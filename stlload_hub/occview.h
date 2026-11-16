#ifndef OCCVIEW_H
#define OCCVIEW_H

#include <QPoint>
#include <QString>
#include <QWidget>

#include <AIS_InteractiveContext.hxx>
#include <AIS_Shape.hxx>
#include <V3d_View.hxx>
#include <V3d_Viewer.hxx>
#include <gp_Dir.hxx>
#include <gp_Pnt.hxx>
#include <gp_Lin.hxx>
#include <Poly_Triangulation.hxx>

#include <vector>
#include <array>
#include <TopoDS_Shape.hxx>

class QMouseEvent;
class QPaintEngine;
class QResizeEvent;
class QWheelEvent;
class SectionPlane;
struct SectionContour;

class OccView : public QWidget
{
    Q_OBJECT

public:
    explicit OccView(QWidget* parent = nullptr);
    ~OccView() override;

    // 场景和模型。loadStl 会替换当前场景；clearScene 释放所有显示对象。
    void clearScene();
    bool loadStl(const QString& filePath);
    void setTriangleMeshVisible(bool visible);

    // 截面平面、轮毂轴和交互拾取。
    void displaySectionPlane(const gp_Pnt& axisPoint,
                             const gp_Dir& axisDirection,
                             const gp_Dir& radialDirection,
                             double modelDiagonal);
    void displayHubAxis(const gp_Pnt& axisPoint,
                        const gp_Dir& axisDirection,
                        double modelDiagonal);
    void clearSectionPlane();
    void setPlanePointPicking(bool enabled, SectionPlane* plane,
                              double pointTolerance);
    void cancelPlanePointPicking();
    bool isPlanePointPicking() const;
    void setPlanePreviewPoints(const std::vector<gp_Pnt>& points);
    void setContourPointPicking(bool enabled,
                                const std::vector<SectionContour>& contours,
                                double pointTolerance);
    void cancelContourPointPicking();
    bool isContourPointPicking() const;
    void setContourErrorInspection(bool enabled,
                                   const std::vector<SectionContour>& contours);
    void showContourErrorSegment(const gp_Pnt& point, const gp_Pnt& nearest);
    void clearContourErrorSegment();
    // 轮廓显示。比较视图可分别控制完整轮廓、目标折线和拟合曲线。
    void displayFittedContours(const std::vector<SectionContour>& contours);
    void displayContourComparison(const std::vector<SectionContour>& contours,
                                  bool showFittedContours = true);
    void displayContourComparison(const std::vector<SectionContour>& contours,
                                  bool showRawContours,
                                  bool showFittedContours);
    void displayContourComparison(
        const std::vector<SectionContour>& originalContours,
        const std::vector<SectionContour>& targetContours,
        bool showOriginalContours,
        bool showTargetContours,
        bool showFittedContours,
        const std::vector<SectionContour>& beforeBumpContours = {},
        bool showBeforeBumpContours = false);
    void clearFittedContours();
    QString lastError() const;
    void setSurfaceSelection(int patch, double radius);
    void clearSurfacePatch(int patch);
    std::vector<gp_Pnt> surfacePatchSamples(int patch) const;
    int surfacePatchTriangleCount(int patch) const;
    void displaySurfaceFit(const TopoDS_Shape& shape);
    void displaySurfacePatchFit(int patch, const TopoDS_Shape& shape);
    void clearSurfaceFit();
    void displaySurfaceIntersections(const TopoDS_Shape& shape);

signals:
    void surfaceSelectionChanged();
    void surfacePatchEdited(int patch);
    void surfaceSelectionMessage(const QString& message);
    void planePointPicked(const gp_Pnt& point);
    void planePointPickFailed(const QString& message);
    void contourPointPicked(int contourIndex,
                            int pointIndex,
                            const gp_Pnt& point);
    void contourPointPickFailed(const QString& message);

protected:
    bool event(QEvent* event) override;
    // Qt 绘制、尺寸变化和鼠标交互事件。
    QPaintEngine* paintEngine() const;
    void paintEvent(QPaintEvent* event);
    void resizeEvent(QResizeEvent* event);
    void showEvent(QShowEvent* event);

    void mousePressEvent(QMouseEvent* event);
    void mouseMoveEvent(QMouseEvent* event);
    void mouseReleaseEvent(QMouseEvent* event) override;
    void wheelEvent(QWheelEvent* event);

private:
    void paintSurfacePatch(const QPoint& position, bool remove);
    void refreshSurfaceSelection();
    int m_surfacePatch = -1;
    double m_surfaceRadius = 5;
    std::vector<int> m_surfaceOwners;
    std::array<Handle(AIS_InteractiveObject),4> m_surfaceOverlays;
    Handle(AIS_Shape) m_surfaceFit;
    Handle(AIS_Shape) m_surfaceIntersections;
    std::array<Handle(AIS_Shape),4> m_surfacePatchFits;
    // OCCT 初始化和场景对象管理。
    void initOcct();
    void releaseOcct() noexcept;
    void resetSceneObjects();
    void displayTriangleMesh();
    void clearTriangleMesh();
    void updateFittedCurveDisplayPrecision();
    void redraw();
    bool pickMeshPoint(const QPoint& position, gp_Pnt* point) const;
    bool pickContourPoint(const QPoint& position,
                          int* contourIndex,
                          int* pointIndex,
                          gp_Pnt* point) const;
    void displayPlanePickMarkers();
    void updateContourPickProjection();

private:
    // OCCT 视图生命周期。
    bool m_isInitialized = false;

    QPoint m_lastMousePos;
    QPoint m_mousePressPos;
    bool m_leftButtonPressed = false;
    bool m_mouseDragged = false;

    Handle(V3d_Viewer) m_viewer;
    Handle(V3d_View) m_view;
    Handle(AIS_InteractiveContext) m_context;
    // 当前场景中的可选显示层。
    Handle(AIS_Shape) m_sectionPlane;
    Handle(AIS_Shape) m_hubAxisShape;
    Handle(AIS_Shape) m_rawContourShape;
    Handle(AIS_Shape) m_originalContourShape;
    Handle(AIS_Shape) m_beforeBumpContourShape;
    Handle(AIS_Shape) m_fittedContourShape;
    Handle(AIS_Shape) m_triangleMeshShape;
    Handle(Poly_Triangulation) m_currentTriangulation;
    bool m_triangleMeshVisible = false;
    QString m_lastError;

    // 截面外圆点拾取状态。m_pickPlane 在拾取期间由 MainWindow 持有。
    bool m_planePointPicking = false;
    bool m_contourPointPicking = false;
    SectionPlane* m_pickPlane = nullptr;
    double m_pickPointTolerance = 1.0e-6;
    std::vector<gp_Pnt> m_planePreviewPoints;
    std::vector<SectionContour> m_pickContours;
    std::vector<std::vector<QPoint>> m_pickContourScreenPoints;
    Handle(AIS_Shape) m_planePickMarkers;
    bool m_contourErrorInspection = false;
    Handle(AIS_Shape) m_contourErrorSegment;
};

#endif // 视图头文件保护结束
