#include "occview.h"

#include "SectionTypes.h"
#include "SectionPlane.h"

// Qt 头文件
#include <QApplication>
#include <QFile>
#include <QFileInfo>
#include <QMouseEvent>
#include <QPaintEvent>
#include <QPlatformSurfaceEvent>
#include <QResizeEvent>
#include <QWheelEvent>

// Open CASCADE 头文件
#include <AIS_DisplayMode.hxx>
#include <AIS_Shape.hxx>
#include <AIS_Triangulation.hxx>
#include <Aspect_DisplayConnection.hxx>
#include <Aspect_Window.hxx>
#include <BRep_Builder.hxx>
#include <BRepBuilderAPI_MakeFace.hxx>
#include <BRepBuilderAPI_MakeEdge.hxx>
#include <BRepBuilderAPI_MakeVertex.hxx>
#include <Graphic3d_ZLayerId.hxx>
#include <Prs3d_PointAspect.hxx>
#include <BRepBuilderAPI_MakePolygon.hxx>
#include <Geom_BSplineCurve.hxx>
#include <Graphic3d_Camera.hxx>
#include <OpenGl_GraphicDriver.hxx>
#include <Poly_Triangulation.hxx>
#include <Poly_Triangle.hxx>
#include <Precision.hxx>
#include <Prs3d_Drawer.hxx>
#include <Quantity_Color.hxx>
#include <RWStl.hxx>
#include <Standard_Failure.hxx>
#include <TopoDS_Compound.hxx>
#include <TopoDS_Edge.hxx>
#include <TopoDS_Face.hxx>
#include <TopoDS_Wire.hxx>
#include <gp_Vec.hxx>

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <exception>
#include <limits>
#include <unordered_set>

#ifdef _WIN32
#include <WNT_Window.hxx>
#endif

namespace
{
// 仅使用相机矩阵投影世界坐标点，避免调用
// 视图侧的像素转换路径；该路径可能在原生视图
// 调整大小或重绘时访问 OpenGL 映射。
bool projectToWidgetPixels(const Handle(V3d_View)& view,
                           const gp_Pnt& point,
                           int widgetWidth,
                           int widgetHeight,
                           double* pixelX,
                           double* pixelY)
{
    if (pixelX == nullptr || pixelY == nullptr || view.IsNull()
        || widgetWidth <= 0 || widgetHeight <= 0
        || !std::isfinite(point.X()) || !std::isfinite(point.Y())
        || !std::isfinite(point.Z())) {
        return false;
    }

    try {
        const Handle(Graphic3d_Camera)& camera = view->Camera();
        if (camera.IsNull()) {
            return false;
        }
        const gp_Pnt ndc = camera->Project(point);
        if (!std::isfinite(ndc.X()) || !std::isfinite(ndc.Y())
            || !std::isfinite(ndc.Z())) {
            return false;
        }

        // Camera::Project() 返回归一化设备坐标，
        // 原点位于左下角；Qt 鼠标事件的原点位于左上角。
        *pixelX = (ndc.X() + 1.0) * 0.5 * static_cast<double>(widgetWidth);
        const double bottomY =
            (ndc.Y() + 1.0) * 0.5 * static_cast<double>(widgetHeight);
        *pixelY = static_cast<double>(widgetHeight - 1) - bottomY;
        return std::isfinite(*pixelX) && std::isfinite(*pixelY);
    } catch (const Standard_Failure&) {
        return false;
    } catch (const std::exception&) {
        return false;
    } catch (...) {
        return false;
    }
}

// 将点列转换为 OCCT 线框。返回 false 表示点数不足或拓扑构造失败。
bool addPolyline(BRep_Builder* builder,
                 TopoDS_Compound* compound,
                 const std::vector<gp_Pnt>& points,
                 bool closed)
{
    if (builder == nullptr || compound == nullptr || points.size() < 2U) {
        return false;
    }

    try {
        BRepBuilderAPI_MakePolygon polygon;
        for (const gp_Pnt& point : points) {
            if (!std::isfinite(point.X()) || !std::isfinite(point.Y())
                || !std::isfinite(point.Z())) {
                return false;
            }
            polygon.Add(point);
        }
        if (closed) {
            polygon.Close();
        }
        if (!polygon.IsDone()) {
            return false;
        }

        builder->Add(*compound, polygon.Wire());
        return true;
    } catch (const Standard_Failure&) {
        return false;
    } catch (const std::exception&) {
        return false;
    } catch (...) {
        return false;
    }
}

// 优先使用精确 B 样条；没有曲线对象时使用拟合采样点作为显示回退。
bool addFittedContour(BRep_Builder* builder,
                      TopoDS_Compound* compound,
                      const SectionContour& contour)
{
    if (builder == nullptr || compound == nullptr) {
        return false;
    }
    bool added = false;
    if (!contour.fittedSegments.empty()) {
        for (const FittedCurveSegment& segment : contour.fittedSegments) {
            if (!segment.curve.IsNull()) {
                try {
                    BRepBuilderAPI_MakeEdge edgeMaker(segment.curve);
                    if (edgeMaker.IsDone()) {
                        builder->Add(*compound, edgeMaker.Edge());
                        added = true;
                        continue;
                    }
                } catch (const Standard_Failure&) {
                    // 使用采样线段作为安全的显示回退。
                }
            }
            if (addPolyline(builder, compound, segment.fittedPoints, false)) {
                added = true;
            }
        }
        return added;
    }
    if (!contour.curve.IsNull()) {
        try {
            BRepBuilderAPI_MakeEdge edgeMaker(contour.curve);
            if (edgeMaker.IsDone()) {
                builder->Add(*compound, edgeMaker.Edge());
                return true;
            }
        } catch (const Standard_Failure&) {
            // 回退到采样点显示。
        }
    }
    return addPolyline(
        builder, compound, contour.fittedPoints, contour.closed);
}
} // 命名空间

// ============================================================================
// 控件初始化和 Qt 生命周期
// ============================================================================

OccView::OccView(QWidget* parent)
    : QWidget(parent)
{

    // OccView 使用原生窗口承载 Open CASCADE 的 OpenGL 视图。
    setAttribute(Qt::WA_NativeWindow);
    setAttribute(Qt::WA_PaintOnScreen);
    setAttribute(Qt::WA_NoSystemBackground);
    setAttribute(Qt::WA_OpaquePaintEvent);

    setMouseTracking(true);
    setFocusPolicy(Qt::StrongFocus);
}

OccView::~OccView()
{
    releaseOcct();
}

bool OccView::event(QEvent* event)
{
    if (event->type() == QEvent::PlatformSurface
        && static_cast<QPlatformSurfaceEvent*>(event)->surfaceEventType()
            == QPlatformSurfaceEvent::SurfaceAboutToBeDestroyed) {
        // 父窗口可能在 Qt 删除子控件之前销毁子窗口句柄。
        // 应在窗口句柄仍有效时释放图形上下文。
        releaseOcct();
    }
    return QWidget::event(event);
}

void OccView::releaseOcct() noexcept
{
    // 先解除 OpenCascade 视图与原生窗口的绑定，再由 Qt 销毁窗口句柄。
    try {
        if (!m_context.IsNull()) {
            m_context->RemoveAll(false);
        }
        if (!m_view.IsNull()) {
            m_view->Remove();
        }
    } catch (...) {
        // 窗口正在销毁时驱动可能已经不可用，此时只需继续释放句柄。
    }

    resetSceneObjects();
    m_context.Nullify();
    m_view.Nullify();
    m_viewer.Nullify();
    m_isInitialized = false;
}

QPaintEngine* OccView::paintEngine() const
{
    return nullptr;
}

void OccView::showEvent(QShowEvent* event)
{
    QWidget::showEvent(event);

    if (!m_isInitialized) {
        initOcct();
    }
}

void OccView::paintEvent(QPaintEvent* event)
{
    Q_UNUSED(event);
    redraw();
}

void OccView::resizeEvent(QResizeEvent* event)
{
    QWidget::resizeEvent(event);

    if (!m_view.IsNull()) {
        m_view->MustBeResized();
        redraw();
    }
    if (m_contourPointPicking) {
        cancelContourPointPicking();
        emit contourPointPickFailed(
            QStringLiteral("视图尺寸已变化，请重新开始目标折线选点。"));
    }
}

// ============================================================================
// Open CASCADE 视图初始化
// ============================================================================

void OccView::initOcct()
{
    if (m_isInitialized) {
        return;
    }
    if (!isVisible() || winId() == 0 || width() <= 0 || height() <= 0) {
        return;
    }

    try {
    // 创建 Open CASCADE 与操作系统之间的显示连接。
    Handle(Aspect_DisplayConnection) displayConnection =
        new Aspect_DisplayConnection();

    // 创建 OpenGL 图形驱动。
    Handle(OpenGl_GraphicDriver) graphicDriver =
        new OpenGl_GraphicDriver(displayConnection);

    m_viewer = new V3d_Viewer(graphicDriver);
    m_viewer->SetDefaultLights();
    m_viewer->SetLightOn();

    m_context = new AIS_InteractiveContext(m_viewer);

    m_view = m_viewer->CreateView();


    // 将 Open CASCADE 视图绑定到 Qt 控件的原生窗口。
#ifdef _WIN32
    Handle(WNT_Window) occtWindow =
        new WNT_Window(reinterpret_cast<Aspect_Handle>(winId()));
#else
#error This example currently contains only the Windows WNT_Window binding.
#endif

    m_view->SetWindow(occtWindow);

    if (!occtWindow->IsMapped()) {
        occtWindow->Map();
    }

    // 设置背景颜色和三维坐标指示器。
    m_view->SetBackgroundColor(Quantity_NOC_GRAY30);
    m_view->TriedronDisplay(Aspect_TOTP_LEFT_LOWER, Quantity_NOC_WHITE, 0.08, V3d_ZBUFFER);

    m_view->MustBeResized();

    m_isInitialized = true;
    redraw();
    } catch (const Standard_Failure& failure) {
        m_lastError = QStringLiteral("Open CASCADE 视图初始化失败：%1")
            .arg(QString::fromLatin1(failure.GetMessageString()));
        resetSceneObjects();
        m_context.Nullify();
        m_view.Nullify();
        m_viewer.Nullify();
        m_isInitialized = false;
    } catch (const std::exception& failure) {
        m_lastError = QStringLiteral("视图初始化失败：%1")
            .arg(QString::fromLocal8Bit(failure.what()));
        resetSceneObjects();
        m_context.Nullify();
        m_view.Nullify();
        m_viewer.Nullify();
        m_isInitialized = false;
    } catch (...) {
        m_lastError = QStringLiteral("视图初始化失败。");
        resetSceneObjects();
        m_context.Nullify();
        m_view.Nullify();
        m_viewer.Nullify();
        m_isInitialized = false;
    }
}

void OccView::resetSceneObjects()
{
    m_surfacePatch = -1;
    m_surfaceOwners.clear();
    for (auto& overlay : m_surfaceOverlays) overlay.Nullify();
    m_surfaceFit.Nullify();
    m_surfaceIntersections.Nullify();
    for (auto& fit : m_surfacePatchFits) fit.Nullify();
    m_contourErrorInspection = false;
    m_contourErrorSegment.Nullify();
    m_sectionPlane.Nullify();
    m_hubAxisShape.Nullify();
    m_rawContourShape.Nullify();
    m_fittedContourShape.Nullify();
    m_triangleMeshShape.Nullify();
    m_planePickMarkers.Nullify();
    m_currentTriangulation.Nullify();

    m_planePointPicking = false;
    m_contourPointPicking = false;
    m_pickPlane = nullptr;
    m_planePreviewPoints.clear();
    m_pickContours.clear();
    m_pickContourScreenPoints.clear();
    unsetCursor();
}

// ============================================================================
// STL 模型和三角网格显示
// ============================================================================

bool OccView::loadStl(const QString& filePath)
{
    m_lastError.clear();

    if (!m_isInitialized) {
        initOcct();
    }
    if (m_context.IsNull() || m_view.IsNull()) {
        if (m_lastError.isEmpty()) {
            m_lastError = QStringLiteral("Open CASCADE 视图尚未初始化。");
        }
        return false;
    }

    const QFileInfo fileInfo(filePath);
    if (!fileInfo.exists() || !fileInfo.isFile() || !fileInfo.isReadable()) {
        m_lastError = QStringLiteral("Cannot read STL file:\n%1").arg(filePath);
        return false;
    }

    try {
        const QByteArray pathBytes = QFile::encodeName(filePath);

        // Poly_Triangulation 保存 STL 的三角网格几何数据。
        Handle(Poly_Triangulation) triangulation =
            RWStl::ReadFile(pathBytes.constData());

        if (triangulation.IsNull()
            || triangulation->NbNodes() == 0
            || triangulation->NbTriangles() == 0) {
            m_lastError = QStringLiteral("The STL file contains no triangles:\n%1")
                              .arg(filePath);
            return false;
        }

        m_context->RemoveAll(false);
        resetSceneObjects();
        m_currentTriangulation = triangulation;

        // 使用 AIS_Triangulation 显示 STL 的三角面片。
        Handle(AIS_Triangulation) aisMesh =
            new AIS_Triangulation(triangulation);

        aisMesh->SetColor(Quantity_NOC_STEELBLUE);
        // OCCT 8.0.1 中的 AIS_Triangulation 仅在
        // 默认显示模式（模式 0）下计算显示表示。

        m_context->Display(aisMesh, false);
        if (m_triangleMeshVisible) {
            displayTriangleMesh();
        }

        m_view->FitAll(0.01, false);
        m_view->ZFitAll();
        redraw();

        return true;
    } catch (const Standard_Failure&) {
        m_lastError = QStringLiteral("Open CASCADE failed to read the STL file:\n%1")
                          .arg(filePath);
        return false;
    } catch (const std::exception& failure) {
        m_lastError = QStringLiteral("读取 STL 时发生错误：%1")
                          .arg(QString::fromLocal8Bit(failure.what()));
        return false;
    } catch (...) {
        m_lastError = QStringLiteral("读取 STL 时发生未知错误。");
        return false;
    }
}

void OccView::clearScene()
{
    if (!m_isInitialized) {
        initOcct();
    }
    try {
        if (!m_context.IsNull()) {
            m_context->RemoveAll(false);
        }
    } catch (...) {
        m_lastError = QStringLiteral("清理视图对象失败。");
    }

    resetSceneObjects();
    redraw();
}

void OccView::setTriangleMeshVisible(bool visible)
{
    m_triangleMeshVisible = visible;

    if (!visible) {
        clearTriangleMesh();
        redraw();
        return;
    }

    displayTriangleMesh();
}

void OccView::displayTriangleMesh()
{
    if (!m_triangleMeshVisible || m_currentTriangulation.IsNull() || m_context.IsNull()) {
        return;
    }

    clearTriangleMesh();

    BRep_Builder builder;
    TopoDS_Compound compound;
    builder.MakeCompound(compound);

    std::unordered_set<std::uint64_t> edgeKeys;
    const auto addEdge = [&](int firstNode, int secondNode) {
        if (firstNode == secondNode) {
            return;
        }

        const int low = std::min(firstNode, secondNode);
        const int high = std::max(firstNode, secondNode);
        const std::uint64_t key =
            (static_cast<std::uint64_t>(static_cast<std::uint32_t>(low)) << 32U)
            | static_cast<std::uint32_t>(high);
        if (!edgeKeys.insert(key).second) {
            return;
        }

        BRepBuilderAPI_MakeEdge edgeMaker(
            m_currentTriangulation->Node(firstNode),
            m_currentTriangulation->Node(secondNode));
        if (edgeMaker.IsDone()) {
            builder.Add(compound, edgeMaker.Edge());
        }
    };

    for (int triangleIndex = 1;
         triangleIndex <= m_currentTriangulation->NbTriangles();
         ++triangleIndex) {
        int firstNode = 0;
        int secondNode = 0;
        int thirdNode = 0;
        m_currentTriangulation->Triangle(triangleIndex)
            .Get(firstNode, secondNode, thirdNode);

        addEdge(firstNode, secondNode);
        addEdge(secondNode, thirdNode);
        addEdge(thirdNode, firstNode);
    }

    if (edgeKeys.empty()) {
        return;
    }

    m_triangleMeshShape = new AIS_Shape(compound);
    m_triangleMeshShape->SetColor(Quantity_NOC_WHITE);
    m_triangleMeshShape->SetWidth(1.0);
    m_context->Display(m_triangleMeshShape, AIS_WireFrame, -1, true);
    redraw();
}

void OccView::clearTriangleMesh()
{
    if (!m_context.IsNull() && !m_triangleMeshShape.IsNull()) {
        m_context->Remove(m_triangleMeshShape, false);
    }
    m_triangleMeshShape.Nullify();
}

// ============================================================================
// 截面平面和拟合轮廓显示
// ============================================================================

void OccView::displaySectionPlane(const gp_Pnt& axisPoint,
                                  const gp_Dir& axisDirection,
                                  const gp_Dir& radialDirection,
                                  double modelDiagonal)
{
    if (!m_isInitialized) {
        initOcct();
    }
    if (m_context.IsNull()) {
        return;
    }

    clearSectionPlane();

    const double axialHalfLength = std::max(modelDiagonal * 0.6, 1.0);
    const double radialLength = std::max(modelDiagonal * 0.75, 1.0);
    const gp_Vec axis = gp_Vec(axisDirection).Multiplied(axialHalfLength);
    const gp_Vec radial = gp_Vec(radialDirection).Multiplied(radialLength);

    // p1-p2 是半平面的边界，并与轮毂主轴重合；平面只沿径向正方向延伸。
    const gp_Pnt p1 = axisPoint.Translated(-axis);
    const gp_Pnt p2 = axisPoint.Translated(axis);
    const gp_Pnt p3 = p2.Translated(radial);
    const gp_Pnt p4 = p1.Translated(radial);

    BRepBuilderAPI_MakePolygon polygon;
    polygon.Add(p1);
    polygon.Add(p2);
    polygon.Add(p3);
    polygon.Add(p4);
    polygon.Close();

    if (!polygon.IsDone()) {
        m_lastError = QStringLiteral("Failed to build section plane wire.");
        return;
    }

    BRepBuilderAPI_MakeFace faceBuilder(polygon.Wire());
    if (!faceBuilder.IsDone()) {
        m_lastError = QStringLiteral("Failed to build section plane face.");
        return;
    }

    m_sectionPlane = new AIS_Shape(faceBuilder.Face());
    m_sectionPlane->SetColor(Quantity_NOC_GOLD);
    m_sectionPlane->SetTransparency(0.72);

    m_context->Display(m_sectionPlane, AIS_Shaded, -1, true);
    redraw();
}

void OccView::clearSectionPlane()
{
    if (!m_context.IsNull() && !m_sectionPlane.IsNull()) {
        m_context->Remove(m_sectionPlane, false);
    }
    m_sectionPlane.Nullify();
}

void OccView::displayHubAxis(const gp_Pnt& axisPoint,
                             const gp_Dir& axisDirection,
                             double modelDiagonal)
{
    if (!m_isInitialized) {
        initOcct();
    }
    if (m_context.IsNull()) {
        return;
    }
    if (!m_hubAxisShape.IsNull()) {
        m_context->Remove(m_hubAxisShape, false);
        m_hubAxisShape.Nullify();
    }

    const double halfLength = std::max(modelDiagonal * 0.65, 1.0);
    const gp_Vec offset(axisDirection);
    BRepBuilderAPI_MakeEdge axisEdge(
        axisPoint.Translated(-offset.Multiplied(halfLength)),
        axisPoint.Translated(offset.Multiplied(halfLength)));
    if (!axisEdge.IsDone()) {
        return;
    }

    m_hubAxisShape = new AIS_Shape(axisEdge.Edge());
    m_hubAxisShape->SetColor(Quantity_NOC_CYAN1);
    m_hubAxisShape->SetWidth(2.5);
    m_context->Display(m_hubAxisShape, AIS_WireFrame, -1, true);
}

void OccView::setPlanePointPicking(bool enabled, SectionPlane* plane,
                                   double pointTolerance)
{
    m_contourPointPicking = false;
    m_pickContours.clear();
    m_pickContourScreenPoints.clear();
    m_planePointPicking = enabled && plane != nullptr;
    m_pickPlane = m_planePointPicking ? plane : nullptr;
    m_pickPointTolerance = std::max(pointTolerance, 1.0e-9);
    m_planePreviewPoints.clear();
    displayPlanePickMarkers();
    setCursor(m_planePointPicking ? Qt::CrossCursor : Qt::ArrowCursor);
}

void OccView::cancelPlanePointPicking()
{
    m_planePointPicking = false;
    m_pickPlane = nullptr;
    m_planePreviewPoints.clear();
    if (!m_context.IsNull() && !m_planePickMarkers.IsNull()) {
        m_context->Remove(m_planePickMarkers, false);
    }
    m_planePickMarkers.Nullify();
    unsetCursor();
    redraw();
}

void OccView::setContourPointPicking(
    bool enabled,
    const std::vector<SectionContour>& contours,
    double pointTolerance)
{
    m_planePointPicking = false;
    m_pickPlane = nullptr;
    m_contourPointPicking = enabled && !contours.empty();
    m_pickContours = m_contourPointPicking
        ? contours
        : std::vector<SectionContour>{};
    m_pickContourScreenPoints.clear();
    m_pickPointTolerance = std::max(pointTolerance, 1.0e-9);
    if (m_contourPointPicking) {
        updateContourPickProjection();
        if (!m_contourPointPicking) {
            return;
        }
        m_planePreviewPoints.clear();
        // 复用已显示的清洗轮廓，不额外添加拾取图层。
        setCursor(Qt::CrossCursor);
    } else {
        setCursor(Qt::ArrowCursor);
    }
}

void OccView::updateContourPickProjection()
{
    m_pickContourScreenPoints.clear();
    // 按当前相机更新缓存，保证旋转、平移和缩放后仍准确拾取。
    m_pickContourScreenPoints.resize(m_pickContours.size());
    try {
        for (std::size_t contourIndex = 0;
             contourIndex < m_pickContours.size(); ++contourIndex) {
            const auto& points = m_pickContours[contourIndex].cleanedPoints;
            auto& screenPoints = m_pickContourScreenPoints[contourIndex];
            screenPoints.reserve(points.size());
            for (const gp_Pnt& candidate : points) {
                QPoint screenPoint(-100000, -100000);
                if (std::isfinite(candidate.X())
                    && std::isfinite(candidate.Y())
                    && std::isfinite(candidate.Z())
                    && !m_view.IsNull()) {
                    double screenX = 0.0;
                    double screenY = 0.0;
                    if (projectToWidgetPixels(m_view, candidate,
                                               width(), height(),
                                               &screenX, &screenY)) {
                        screenPoint = QPoint(
                            static_cast<int>(std::lround(screenX)),
                            static_cast<int>(std::lround(screenY)));
                    }
                }
                screenPoints.push_back(screenPoint);
            }
        }
    } catch (const Standard_Failure&) {
        m_pickContourScreenPoints.clear();
        m_contourPointPicking = false;
        m_pickContours.clear();
        setCursor(Qt::ArrowCursor);
        return;
    } catch (const std::exception&) {
        m_pickContourScreenPoints.clear();
        m_contourPointPicking = false;
        m_pickContours.clear();
        setCursor(Qt::ArrowCursor);
        return;
    }
}

void OccView::cancelContourPointPicking()
{
    m_contourPointPicking = false;
    m_pickContours.clear();
    m_pickContourScreenPoints.clear();
    m_planePreviewPoints.clear();
    if (!m_context.IsNull() && !m_planePickMarkers.IsNull()) {
        m_context->Remove(m_planePickMarkers, false);
    }
    m_planePickMarkers.Nullify();
    unsetCursor();
    redraw();
}

void OccView::setContourErrorInspection(
    bool enabled, const std::vector<SectionContour>& contours)
{
    clearContourErrorSegment();
    m_contourErrorInspection = enabled;
    setContourPointPicking(enabled, contours, 1.0e-9);
    redraw();
}

void OccView::clearContourErrorSegment()
{
    if (!m_context.IsNull() && !m_contourErrorSegment.IsNull()) {
        m_context->Remove(m_contourErrorSegment, false);
    }
    m_contourErrorSegment.Nullify();
    redraw();
}

void OccView::showContourErrorSegment(const gp_Pnt& point, const gp_Pnt& nearest)
{
    clearContourErrorSegment();
    if (m_context.IsNull() || !m_contourErrorInspection) return;
    updateFittedCurveDisplayPrecision();
    BRep_Builder builder;
    TopoDS_Compound compound;
    builder.MakeCompound(compound);
    builder.Add(compound, BRepBuilderAPI_MakeVertex(point).Vertex());
    builder.Add(compound, BRepBuilderAPI_MakeVertex(nearest).Vertex());
    // 重合端点不能构造有效边，此时只显示位置标记。
    if (point.Distance(nearest) > Precision::Confusion()) {
        BRepBuilderAPI_MakeEdge edge(point, nearest);
        if (edge.IsDone()) builder.Add(compound, edge.Edge());
    }
    m_contourErrorSegment = new AIS_Shape(compound);
    m_contourErrorSegment->SetColor(Quantity_NOC_MAGENTA1);
    m_contourErrorSegment->SetWidth(3.0);
    m_contourErrorSegment->Attributes()->SetPointAspect(new Prs3d_PointAspect(
        Aspect_TOM_POINT, Quantity_NOC_MAGENTA1, 6.0));
    m_contourErrorSegment->SetZLayer(Graphic3d_ZLayerId_Topmost);
    m_context->Display(m_contourErrorSegment, AIS_WireFrame, -1, false);
    redraw();
}

bool OccView::isContourPointPicking() const
{
    return m_contourPointPicking;
}

bool OccView::isPlanePointPicking() const
{
    return m_planePointPicking;
}

void OccView::setPlanePreviewPoints(const std::vector<gp_Pnt>& points)
{
    m_planePreviewPoints = points;
    displayPlanePickMarkers();
}

void OccView::displayPlanePickMarkers()
{
    if (m_context.IsNull()) {
        return;
    }
    if (!m_planePickMarkers.IsNull()) {
        m_context->Remove(m_planePickMarkers, false);
        m_planePickMarkers.Nullify();
    }
    m_planePickMarkers.Nullify();
    if (m_planePreviewPoints.empty()) {
        redraw();
        return;
    }

    BRep_Builder builder;
    TopoDS_Compound compound;
    builder.MakeCompound(compound);
    const double markerSize = std::max(m_pickPointTolerance * 20.0, 0.5);
    if (m_pickPlane != nullptr) {
        const double axisHalfLength = std::max(m_pickPointTolerance * 1.0e7, 1.0);
        const gp_Vec axisOffset(m_pickPlane->axisDirection());
        BRepBuilderAPI_MakeEdge axisEdge(
            m_pickPlane->axisPoint().Translated(
                -axisOffset.Multiplied(axisHalfLength)),
            m_pickPlane->axisPoint().Translated(
                axisOffset.Multiplied(axisHalfLength)));
        if (axisEdge.IsDone()) {
            builder.Add(compound, axisEdge.Edge());
        }
    }
    for (const gp_Pnt& point : m_planePreviewPoints) {
        BRepBuilderAPI_MakeEdge horizontal(
            point.Translated(gp_Vec(-markerSize, 0.0, 0.0)),
            point.Translated(gp_Vec(markerSize, 0.0, 0.0)));
        BRepBuilderAPI_MakeEdge vertical(
            point.Translated(gp_Vec(0.0, -markerSize, 0.0)),
            point.Translated(gp_Vec(0.0, markerSize, 0.0)));
        BRepBuilderAPI_MakeEdge depth(
            point.Translated(gp_Vec(0.0, 0.0, -markerSize)),
            point.Translated(gp_Vec(0.0, 0.0, markerSize)));
        if (horizontal.IsDone()) builder.Add(compound, horizontal.Edge());
        if (vertical.IsDone()) builder.Add(compound, vertical.Edge());
        if (depth.IsDone()) builder.Add(compound, depth.Edge());
    }
    m_planePickMarkers = new AIS_Shape(compound);
    m_planePickMarkers->SetColor(Quantity_NOC_RED);
    m_planePickMarkers->SetWidth(3.0);
    m_context->Display(m_planePickMarkers, AIS_WireFrame, -1, false);
    redraw();
}

bool OccView::pickMeshPoint(const QPoint& position, gp_Pnt* point) const
{
    if (point == nullptr || !m_isInitialized || m_view.IsNull()
        || m_currentTriangulation.IsNull()) {
        return false;
    }

    // 使用已经渲染的视图比较屏幕坐标。
    // 原先通过 ConvertWithProj() 构造射线时，可能在视图更新期间
    // 解引用无效的 OpenGL 投影状态，
    // 导致拾取时偶发访问冲突。
    constexpr double pickTolerancePixels = 18.0;
    const double squaredTolerance = pickTolerancePixels * pickTolerancePixels;
    double bestDistanceSquared = squaredTolerance;
    bool found = false;

    for (int nodeIndex = 1;
         nodeIndex <= m_currentTriangulation->NbNodes(); ++nodeIndex) {
        const gp_Pnt candidate = m_currentTriangulation->Node(nodeIndex);
        if (!std::isfinite(candidate.X()) || !std::isfinite(candidate.Y())
            || !std::isfinite(candidate.Z())) {
            continue;
        }
        double screenX = 0.0;
        double screenY = 0.0;
        if (!projectToWidgetPixels(m_view, candidate, width(), height(),
                                   &screenX, &screenY)) {
            continue;
        }
        const double dx = screenX - static_cast<double>(position.x());
        const double dy = screenY - static_cast<double>(position.y());
        const double distanceSquared = dx * dx + dy * dy;
        if (distanceSquared < bestDistanceSquared) {
            bestDistanceSquared = distanceSquared;
            *point = candidate;
            found = true;
        }
    }

    return found;
}

bool OccView::pickContourPoint(const QPoint& position,
                               int* contourIndex,
                               int* pointIndex,
                               gp_Pnt* point) const
{
    if (contourIndex == nullptr || pointIndex == nullptr || point == nullptr
        || !m_isInitialized || m_pickContours.empty()
        || m_pickContourScreenPoints.size() != m_pickContours.size()) {
        return false;
    }

    constexpr double pickTolerancePixels = 18.0;
    const double squaredTolerance = pickTolerancePixels * pickTolerancePixels;
    double bestDistanceSquared = squaredTolerance;
    bool found = false;

    for (int currentContour = 0;
         currentContour < static_cast<int>(m_pickContours.size());
         ++currentContour) {
        const SectionContour& contour = m_pickContours[
            static_cast<std::size_t>(currentContour)];
        const std::vector<QPoint>& screenPoints = m_pickContourScreenPoints[
            static_cast<std::size_t>(currentContour)];
        if (screenPoints.size() != contour.cleanedPoints.size()) {
            return false;
        }
        for (int currentPoint = 0;
             currentPoint < static_cast<int>(contour.cleanedPoints.size());
             ++currentPoint) {
            const gp_Pnt candidate = contour.cleanedPoints[
                static_cast<std::size_t>(currentPoint)];
            if (!std::isfinite(candidate.X()) || !std::isfinite(candidate.Y())
                || !std::isfinite(candidate.Z())) {
                continue;
            }
            const QPoint screenPoint = screenPoints[
                static_cast<std::size_t>(currentPoint)];
            const double dx = static_cast<double>(screenPoint.x() - position.x());
            const double dy = static_cast<double>(screenPoint.y() - position.y());
            const double distanceSquared = dx * dx + dy * dy;
            if (distanceSquared < bestDistanceSquared) {
                bestDistanceSquared = distanceSquared;
                *contourIndex = currentContour;
                *pointIndex = currentPoint;
                *point = candidate;
                found = true;
            }
        }
    }
    return found;
}

void OccView::displayFittedContours(const std::vector<SectionContour>& contours)
{
    if (!m_isInitialized) {
        initOcct();
    }
    if (m_context.IsNull()) {
        return;
    }

    clearFittedContours();

    BRep_Builder builder;
    TopoDS_Compound compound;
    builder.MakeCompound(compound);
    bool hasCurve = false;

    for (const SectionContour& contour : contours) {
        if (addFittedContour(&builder, &compound, contour)) {
            hasCurve = true;
        }
    }

    if (!hasCurve) {
        return;
    }

    m_fittedContourShape = new AIS_Shape(compound);
    m_fittedContourShape->SetOwnDeviationAngle(0.017453292519943295);
    m_fittedContourShape->SetOwnDeviationCoefficient(1.e-5);
    m_fittedContourShape->Attributes()->SetDiscretisation(200);
    m_fittedContourShape->SetColor(Quantity_NOC_LIMEGREEN);
    m_fittedContourShape->SetWidth(3.0);
    m_context->Display(m_fittedContourShape, AIS_WireFrame, -1, true);
    if (!m_view.IsNull()) {
        m_view->FitAll(0.01, false);
        m_view->ZFitAll();
    }
    redraw();
}

void OccView::displayContourComparison(
    const std::vector<SectionContour>& contours,
    bool showFittedContours)
{
    displayContourComparison(
        contours,
        contours,
        true,
        true,
        showFittedContours);
}

void OccView::displayContourComparison(
    const std::vector<SectionContour>& contours,
    bool showRawContours,
    bool showFittedContours)
{
    displayContourComparison(
        contours,
        contours,
        showRawContours,
        showRawContours,
        showFittedContours);
}

void OccView::displayContourComparison(
    const std::vector<SectionContour>& originalContours,
    const std::vector<SectionContour>& targetContours,
    bool showOriginalContours,
    bool showTargetContours,
    bool showFittedContours,
    const std::vector<SectionContour>& beforeBumpContours,
    bool showBeforeBumpContours)
{
    if (!m_isInitialized) {
        initOcct();
    }
    if (m_context.IsNull()) {
        return;
    }

    clearFittedContours();

    BRep_Builder builder;
    TopoDS_Compound rawCompound;
    TopoDS_Compound fittedCompound;
    builder.MakeCompound(rawCompound);
    builder.MakeCompound(fittedCompound);
    bool hasRawContour = false;
    bool hasFittedContour = false;
    bool hasBeforeBumpContour = false;
    bool hasOriginalContour = false;

    if (showBeforeBumpContours) {
        TopoDS_Compound beforeCompound;
        builder.MakeCompound(beforeCompound);
        for (const auto& contour : beforeBumpContours) {
            if (addPolyline(&builder, &beforeCompound, contour.cleanedPoints, contour.closed))
                hasBeforeBumpContour = true;
        }
        if (hasBeforeBumpContour) {
            m_beforeBumpContourShape = new AIS_Shape(beforeCompound);
            m_beforeBumpContourShape->SetColor(Quantity_NOC_MAGENTA1);
            m_beforeBumpContourShape->SetWidth(2.0);
            m_context->Display(m_beforeBumpContourShape, AIS_WireFrame, -1, false);
        }
    }

    if (showOriginalContours) {
        TopoDS_Compound originalCompound;
        builder.MakeCompound(originalCompound);
        bool hasOriginal = false;
        for (const SectionContour& contour : originalContours) {
            if (addPolyline(&builder,
                            &originalCompound,
                            contour.cleanedPoints,
                            contour.closed)) {
                hasOriginal = true;
            }
        }
        if (hasOriginal) {
            hasOriginalContour = true;
            m_originalContourShape = new AIS_Shape(originalCompound);
            m_originalContourShape->SetColor(Quantity_NOC_CYAN1);
            m_originalContourShape->SetWidth(1.0);
            m_context->Display(m_originalContourShape, AIS_WireFrame, -1, false);
        }
    }
    for (const SectionContour& contour : targetContours) {
        if (showTargetContours) {
            if (addPolyline(&builder,
                            &rawCompound,
                            contour.cleanedPoints,
                            contour.closed)) {
                hasRawContour = true;
            }
        }

        if (showFittedContours
            && addFittedContour(&builder, &fittedCompound, contour)) {
            hasFittedContour = true;
        }
    }

    if (hasRawContour) {
        m_rawContourShape = new AIS_Shape(rawCompound);
        m_rawContourShape->SetColor(Quantity_NOC_ORANGE);
        m_rawContourShape->SetWidth(2.0);
        m_context->Display(m_rawContourShape, AIS_WireFrame, -1, false);
    }
    if (hasFittedContour) {
        m_fittedContourShape = new AIS_Shape(fittedCompound);
        m_fittedContourShape->SetOwnDeviationAngle(0.017453292519943295);
        m_fittedContourShape->SetOwnDeviationCoefficient(1.e-5);
        m_fittedContourShape->Attributes()->SetDiscretisation(200);
        m_fittedContourShape->SetColor(Quantity_NOC_LIMEGREEN);
        m_fittedContourShape->SetWidth(3.0);
        m_context->Display(m_fittedContourShape, AIS_WireFrame, -1, false);
    }

    if (!m_view.IsNull() && (hasRawContour || hasFittedContour
        || hasOriginalContour || hasBeforeBumpContour)) {
        m_view->FitAll(0.01, false);
        m_view->ZFitAll();
    }
    redraw();
}

void OccView::clearFittedContours()
{
    clearContourErrorSegment();
    if (!m_context.IsNull() && !m_beforeBumpContourShape.IsNull()) {
        m_context->Remove(m_beforeBumpContourShape, false);
    }
    m_beforeBumpContourShape.Nullify();
    if (!m_context.IsNull() && !m_originalContourShape.IsNull()) {
        m_context->Remove(m_originalContourShape, false);
    }
    m_originalContourShape.Nullify();
    if (!m_context.IsNull() && !m_rawContourShape.IsNull()) {
        m_context->Remove(m_rawContourShape, false);
    }
    if (!m_context.IsNull() && !m_fittedContourShape.IsNull()) {
        m_context->Remove(m_fittedContourShape, false);
    }
    m_rawContourShape.Nullify();
    m_fittedContourShape.Nullify();
}

void OccView::updateFittedCurveDisplayPrecision()
{
    if (m_context.IsNull() || m_view.IsNull() || m_fittedContourShape.IsNull()) return;
    // 将绘制折线与真实曲线之间的弦偏差限制在约四分之一像素。
    // 固定的模型空间偏差在放大后会使精确投影端点看起来脱离曲线。
    const double deflection = m_view->Convert(1) * 0.25;
    if (!std::isfinite(deflection) || deflection <= 0.0) return;
    const auto& drawer = m_fittedContourShape->Attributes();
    if (drawer->TypeOfDeflection() == Aspect_TOD_ABSOLUTE
        && drawer->MaximalChordialDeviation() <= deflection) return;
    drawer->SetTypeOfDeflection(Aspect_TOD_ABSOLUTE);
    drawer->SetMaximalChordialDeviation(deflection);
    m_context->Redisplay(m_fittedContourShape, false);
}

QString OccView::lastError() const
{
    return m_lastError.isEmpty()
        ? QStringLiteral("Failed to load STL file.")
        : m_lastError;
}

void OccView::redraw()
{
    if (!m_view.IsNull()) {
        m_view->Redraw();
    }
}

// ============================================================================
// 鼠标和滚轮交互
// ============================================================================

// 鼠标左键进行旋转，中键进行平移。
void OccView::mousePressEvent(QMouseEvent* event)
{
    if (event == nullptr) {
        return;
    }
    m_lastMousePos = event->position().toPoint();

    if (event->button() == Qt::LeftButton) {
        m_mousePressPos = m_lastMousePos;
        m_leftButtonPressed = true;
        m_mouseDragged = false;
        if (m_isInitialized && !m_view.IsNull()) {
            m_view->StartRotation(m_lastMousePos.x(), m_lastMousePos.y());
        }
    }
}

void OccView::mouseReleaseEvent(QMouseEvent* event)
{
    if (event == nullptr || event->button() != Qt::LeftButton) {
        return;
    }
    const bool isClick = m_leftButtonPressed && !m_mouseDragged
        && (event->position().toPoint() - m_mousePressPos).manhattanLength()
            < QApplication::startDragDistance();
    m_leftButtonPressed = false;
    m_mouseDragged = false;
    if (!isClick) {
        return;
    }
    m_lastMousePos = event->position().toPoint();

    if (m_surfacePatch >= 0) {
        try {
            paintSurfacePatch(m_lastMousePos, event->modifiers().testFlag(Qt::ShiftModifier));
        } catch (const Standard_Failure& failure) {
            emit surfaceSelectionMessage(QStringLiteral("面片选择失败：%1").arg(QString::fromUtf8(failure.GetMessageString())));
        }
        return;
    }
    if (m_planePointPicking && m_pickPlane != nullptr
        && event->button() == Qt::LeftButton) {
        gp_Pnt pickedPoint;
        bool picked = false;
        try {
            picked = pickMeshPoint(m_lastMousePos, &pickedPoint);
        } catch (const Standard_Failure&) {
            picked = false;
        } catch (const std::exception&) {
            picked = false;
        } catch (...) {
            picked = false;
        }
        if (!picked) {
            emit planePointPickFailed(
                QStringLiteral("此位置没有捕获到 STL 原始顶点，请重新选择。"));
            return;
        }
        QString error;
        if (!m_pickPlane->addOuterPoint(
                pickedPoint, m_pickPointTolerance, &error)) {
            emit planePointPickFailed(error);
            return;
        }
        m_planePreviewPoints.clear();
        m_planePreviewPoints.push_back(m_pickPlane->pickedPoint());
        displayPlanePickMarkers();
        emit planePointPicked(pickedPoint);
        return;
    }

    if (m_contourPointPicking && event->button() == Qt::LeftButton) {
        updateContourPickProjection();
        int contourIndex = -1;
        int pointIndex = -1;
        gp_Pnt pickedPoint;
        bool picked = false;
        try {
            picked = pickContourPoint(m_lastMousePos,
                                      &contourIndex,
                                      &pointIndex,
                                      &pickedPoint);
        } catch (const Standard_Failure&) {
            picked = false;
        } catch (const std::exception&) {
            picked = false;
        } catch (...) {
            picked = false;
        }
        if (!picked
            || contourIndex < 0
            || contourIndex >= static_cast<int>(m_pickContours.size())
            || pointIndex < 0
            || pointIndex >= static_cast<int>(m_pickContours[
                static_cast<std::size_t>(contourIndex)].cleanedPoints.size())) {
            emit contourPointPickFailed(
                QStringLiteral("此位置没有捕获到截面轮廓点，请重新选择。"));
            return;
        }
        emit contourPointPicked(contourIndex, pointIndex, pickedPoint);
        return;
    }

}

void OccView::mouseMoveEvent(QMouseEvent* event)
{
    if (!m_isInitialized || m_view.IsNull()) {
        return;
    }

    const QPoint currentPos = event->position().toPoint();
    const QPoint delta = currentPos - m_lastMousePos;

    if ((event->buttons() & Qt::LeftButton) && m_leftButtonPressed) {
        if (!m_mouseDragged
            && (currentPos - m_mousePressPos).manhattanLength()
                >= QApplication::startDragDistance()) {
            m_mouseDragged = true;
        }
        if (m_mouseDragged) {
            m_view->Rotation(currentPos.x(), currentPos.y());
        }
    } else if (event->buttons() & Qt::MiddleButton) {
        m_view->Pan(delta.x(), -delta.y());
    }

    m_lastMousePos = currentPos;
    redraw();
}

// 滚轮控制模型缩放。
void OccView::wheelEvent(QWheelEvent* event)
{
    if (!m_isInitialized || m_view.IsNull()) {
        return;
    }

    const double factor = event->angleDelta().y() > 0 ? 1.1 : 0.9;
    m_view->SetScale(m_view->Scale() * factor);
    updateFittedCurveDisplayPrecision();

    redraw();
}


void OccView::setSurfaceSelection(int patch, double radius)
{
    m_surfacePatch = patch;
    m_surfaceRadius = radius;
    if (patch >= 0) {
        cancelPlanePointPicking();
        cancelContourPointPicking();
        setCursor(Qt::CrossCursor);
    } else unsetCursor();
}

void OccView::displaySurfaceIntersections(const TopoDS_Shape& shape)
{
    if(m_context.IsNull()) return;
    if(!m_surfaceIntersections.IsNull()) m_context->Remove(m_surfaceIntersections,false);
    m_surfaceIntersections.Nullify();
    if(!shape.IsNull()) {
        m_surfaceIntersections=new AIS_Shape(shape);
        m_surfaceIntersections->SetColor(Quantity_NOC_WHITE);
        m_surfaceIntersections->SetWidth(3);
        m_surfaceIntersections->SetZLayer(Graphic3d_ZLayerId_Topmost);
        m_context->Display(m_surfaceIntersections,AIS_WireFrame,-1,false);
    }
    redraw();
}

void OccView::clearSurfaceFit()
{
    displaySurfaceIntersections(TopoDS_Shape());
    if (!m_surfaceFit.IsNull() && !m_context.IsNull()) m_context->Remove(m_surfaceFit,false);
    m_surfaceFit.Nullify();
    if (!m_context.IsNull())
        for (auto& fit:m_surfacePatchFits) if (!fit.IsNull()) m_context->Display(fit,AIS_Shaded,-1,false);
    redraw();
}

void OccView::displaySurfacePatchFit(int patch, const TopoDS_Shape& shape)
{
    if (patch<0 || patch>=4 || m_context.IsNull()) return;
    clearSurfaceFit();
    auto& fit=m_surfacePatchFits[patch];
    if (!fit.IsNull()) m_context->Remove(fit,false);
    fit.Nullify();
    if (!shape.IsNull()) {
        fit=new AIS_Shape(shape);
        const Quantity_NameOfColor colors[4]={Quantity_NOC_RED,Quantity_NOC_GREEN,Quantity_NOC_BLUE1,Quantity_NOC_MAGENTA1};
        fit->SetColor(colors[patch]); fit->SetTransparency(0.15);
        fit->SetZLayer(Graphic3d_ZLayerId_Topmost);
        m_context->Display(fit,AIS_Shaded,-1,false);
    }
    refreshSurfaceSelection();
    redraw();
}

void OccView::displaySurfaceFit(const TopoDS_Shape& shape)
{
    clearSurfaceFit();
    if (shape.IsNull() || m_context.IsNull()) return;
    for (auto& fit:m_surfacePatchFits) if (!fit.IsNull()) m_context->Erase(fit,false);
    m_surfaceFit = new AIS_Shape(shape);
    m_surfaceFit->SetColor(Quantity_NOC_CYAN1);
    m_surfaceFit->SetTransparency(0.25);
    m_surfaceFit->SetZLayer(Graphic3d_ZLayerId_Topmost);
    m_context->Display(m_surfaceFit,AIS_Shaded,-1,false);
    redraw();
}

int OccView::surfacePatchTriangleCount(int patch) const
{
    return int(std::count(m_surfaceOwners.begin(),m_surfaceOwners.end(),patch+1));
}

void OccView::clearSurfacePatch(int patch)
{
    for (auto& owner : m_surfaceOwners) if (owner==patch+1) owner=0;
    clearSurfaceFit();
    refreshSurfaceSelection();
    emit surfacePatchEdited(patch);
    emit surfaceSelectionChanged();
}

std::vector<gp_Pnt> OccView::surfacePatchSamples(int patch) const
{
    std::vector<gp_Pnt> samples;
    if (m_currentTriangulation.IsNull()) return samples;
    std::unordered_set<int> nodes;
    for (size_t i=0;i<m_surfaceOwners.size();++i) {
        if (m_surfaceOwners[i]!=patch+1) continue;
        int a,b,c; m_currentTriangulation->Triangle(int(i)+1).Get(a,b,c);
        const auto A=m_currentTriangulation->Node(a), B=m_currentTriangulation->Node(b), C=m_currentTriangulation->Node(c);
        for (int node : {a,b,c}) if (nodes.insert(node).second) samples.push_back(m_currentTriangulation->Node(node));
        samples.emplace_back((A.XYZ()+B.XYZ()+C.XYZ())/3.0);
        samples.emplace_back((A.XYZ()+B.XYZ())/2.0);
        samples.emplace_back((B.XYZ()+C.XYZ())/2.0);
        samples.emplace_back((C.XYZ()+A.XYZ())/2.0);
    }
    return samples;
}

void OccView::refreshSurfaceSelection()
{
    if (m_context.IsNull() || m_currentTriangulation.IsNull()) return;
    const Quantity_NameOfColor colors[4]={Quantity_NOC_RED,Quantity_NOC_GREEN,Quantity_NOC_BLUE1,Quantity_NOC_MAGENTA1};
    for (int patch=0;patch<4;++patch) {
        auto& overlay=m_surfaceOverlays[patch];
        if (!overlay.IsNull()) m_context->Remove(overlay,false);
        overlay.Nullify();
        const int count=surfacePatchTriangleCount(patch);
        if (!count || !m_surfacePatchFits[patch].IsNull()) continue;
        Handle(Poly_Triangulation) mesh=new Poly_Triangulation(count*3,count,false);
        int index=0;
        for (size_t i=0;i<m_surfaceOwners.size();++i) {
            if (m_surfaceOwners[i]!=patch+1) continue;
            int a,b,c; m_currentTriangulation->Triangle(int(i)+1).Get(a,b,c);
            mesh->SetNode(index*3+1,m_currentTriangulation->Node(a));
            mesh->SetNode(index*3+2,m_currentTriangulation->Node(b));
            mesh->SetNode(index*3+3,m_currentTriangulation->Node(c));
            mesh->SetTriangle(index+1,Poly_Triangle(index*3+1,index*3+2,index*3+3));
            ++index;
        }
        Handle(AIS_Triangulation) triangles=new AIS_Triangulation(mesh);
        // AIS_Triangulation uses mode 0 only; mode 1 silently creates no geometry.
        // Explicit vertex colors avoid relying on AIS_Shape's color behavior.
        const unsigned int rgba[4]={0xff0000ffu,0xff00ff00u,0xffff6000u,0xffff00ffu};
        Handle(NCollection_HArray1<int>) vertexColors=new NCollection_HArray1<int>(1,count*3);
        vertexColors->Init(static_cast<int>(rgba[patch]));
        triangles->SetColors(vertexColors);
        overlay=triangles;
        overlay->SetColor(colors[patch]);
        overlay->SetZLayer(Graphic3d_ZLayerId_Top);
        m_context->Display(overlay,0,-1,false);
    }
    redraw();
}

void OccView::paintSurfacePatch(const QPoint& position, bool remove)
{
    if (m_view.IsNull() || m_currentTriangulation.IsNull()) return;
    if (width()<=0 || height()<=0) return;
    // Mouse positions are Qt logical pixels. Unproject using the same widget
    // dimensions as projectToWidgetPixels, independent of native-window DPI.
    const double nx=2.0*position.x()/width()-1.0;
    const double ny=2.0*(height()-1-position.y())/height()-1.0;
    const auto camera=m_view->Camera();
    const gp_Pnt origin=camera->UnProject(gp_Pnt(nx,ny,-1));
    const gp_Pnt farPoint=camera->UnProject(gp_Pnt(nx,ny,1));
    gp_Vec direction(origin,farPoint);
    if (direction.SquareMagnitude()<1e-20) return;
    direction.Normalize();
    int seed=-1; double nearest=std::numeric_limits<double>::max(); gp_Pnt hit;
    const int n=m_currentTriangulation->NbTriangles();
    std::vector<gp_Pnt> centers; std::vector<gp_Vec> normals;
    std::vector<std::vector<int>> incident(m_currentTriangulation->NbNodes()+1);
    centers.reserve(n); normals.reserve(n);
    for (int i=1;i<=n;++i) {
        int a,b,c; m_currentTriangulation->Triangle(i).Get(a,b,c);
        const auto A=m_currentTriangulation->Node(a), B=m_currentTriangulation->Node(b), C=m_currentTriangulation->Node(c);
        gp_Vec e1(A,B),e2(A,C),normal=e1.Crossed(e2);
        centers.emplace_back((A.XYZ()+B.XYZ()+C.XYZ())/3.0); normals.push_back(normal);
        for (int node : {a,b,c}) incident[node].push_back(i-1);
        // Two-sided ray intersection; nearest positive hit prevents picking the back wall.
        gp_Vec h=direction.Crossed(e2); double determinant=e1.Dot(h);
        if (std::abs(determinant)<1e-12) continue;
        gp_Vec offset(A,origin); double u=offset.Dot(h)/determinant;
        if (u<0 || u>1) continue;
        gp_Vec q=offset.Crossed(e1); double v=direction.Dot(q)/determinant;
        if (v<0 || u+v>1) continue;
        double t=e2.Dot(q)/determinant;
        if (t>=0 && t<nearest) { nearest=t;seed=i-1;hit=origin.Translated(direction.Multiplied(t)); }
    }
    if (seed<0) {
        emit surfaceSelectionMessage(QStringLiteral("未命中 STL 面片，请点击有实体的孔壁，或旋转模型后重试。"));
        return;
    }
    if (m_surfaceOwners.size()!=size_t(n)) m_surfaceOwners.assign(n,0);
    std::vector<int> queue{seed}; std::vector<bool> visited(n,false); visited[seed]=true;
    gp_Vec reference=normals[seed];
    if (reference.SquareMagnitude()<1e-20) return;
    reference.Normalize();
    for (size_t index=0;index<queue.size();++index) {
        const int i=queue[index]; auto normal=normals[i];
        if (normal.SquareMagnitude()<1e-20) continue;
        normal.Normalize();
        if (i!=seed && (centers[i].Distance(hit)>m_surfaceRadius || normal.Dot(reference)<0.5)) continue;
        if (remove) { if (m_surfaceOwners[i]==m_surfacePatch+1) m_surfaceOwners[i]=0; }
        else if (m_surfaceOwners[i]==0 || m_surfaceOwners[i]==m_surfacePatch+1) m_surfaceOwners[i]=m_surfacePatch+1;
        else continue;
        int a,b,c; m_currentTriangulation->Triangle(i+1).Get(a,b,c);
        for (int node : {a,b,c}) for (int next : incident[node]) if (!visited[next]) {visited[next]=true;queue.push_back(next);}
    }
    clearSurfaceFit();
    refreshSurfaceSelection();
    emit surfacePatchEdited(m_surfacePatch);
    emit surfaceSelectionChanged();
    emit surfaceSelectionMessage(QStringLiteral("面 %1 当前选中 %2 个三角面片（Shift 点击删除）。")
        .arg(m_surfacePatch+1).arg(surfacePatchTriangleCount(m_surfacePatch)));
}
