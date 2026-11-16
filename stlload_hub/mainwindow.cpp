#include "mainwindow.h"

#include "./ui_mainwindow.h"
#include "occview.h"
#include "Stlsection.h"
#include "SectionWorkflow.h"
#include "SurfaceFit.h"
#include "SurfaceJoin.h"
#include <QInputDialog>
#include <GeomAPI_ProjectPointOnSurf.hxx>
#include <BRepBuilderAPI_MakeFace.hxx>
#include <BRepCheck_Analyzer.hxx>
#include <BRep_Builder.hxx>
#include <TopoDS_Compound.hxx>
#include <STEPControl_Writer.hxx>
#include <Interface_Static.hxx>
#include <QComboBox>
#include <QDoubleSpinBox>
#include <QLabel>
#include <QSaveFile>
#include <QDateTime>
#include <QProgressDialog>
#include <QApplication>
#include <QJsonDocument>
#include <QJsonObject>
#include <QJsonArray>
#include <sstream>
#include "curvecomparisonwindow.h"

#include <Geom_BSplineCurve.hxx>
#include <Standard_Failure.hxx>
#include <gp_Vec.hxx>

#include <QAction>
#include <QDebug>
#include <QDir>
#include <QFileDialog>
#include <QFileInfo>
#include <QHBoxLayout>
#include <QMenuBar>
#include <QMenu>
#include <QMessageBox>
#include <QPushButton>
#include <QStatusBar>
#include <QVBoxLayout>

#include <algorithm>
#include <cmath>
#include <exception>
#include <limits>

namespace
{
gp_Dir radialDirectionForPlane(const gp_Dir& axisDirection,
                               const gp_Dir& preferredNormal)
{
    const gp_Vec axis(axisDirection);
    gp_Vec normal(preferredNormal);
    normal = normal - axis.Multiplied(normal.Dot(axis));

    if (normal.Magnitude() <= 1.0e-12) {
        gp_Vec reference(1.0, 0.0, 0.0);
        if (std::abs(axis.Dot(reference)) > 0.9) {
            reference = gp_Vec(0.0, 1.0, 0.0);
        }
        normal = axis.Crossed(reference);
    }
    normal.Normalize();

    gp_Vec radial = normal.Crossed(axis);
    radial.Normalize();
    return gp_Dir(radial);
}
} // 命名空间

// ============================================================================
// 窗口初始化和控件连接
// ============================================================================

MainWindow::MainWindow(QWidget* parent)
    : QMainWindow(parent)
    , ui(new Ui::MainWindow)
{
    ui->setupUi(this);

    auto* viewLayout = new QVBoxLayout(ui->centralwidget);
    viewLayout->setContentsMargins(0, 0, 0, 0);

    auto* controlsLayout = new QHBoxLayout();
    controlsLayout->setContentsMargins(8, 8, 8, 4);
    viewLayout->setSpacing(0);
    viewLayout->addLayout(controlsLayout, 0);
    controlsLayout->setSizeConstraint(QLayout::SetMinimumSize);

    m_triangleMeshButton = new QPushButton(QStringLiteral("显示三角网格"), ui->centralwidget);
    m_triangleMeshButton->setCheckable(true);
    m_triangleMeshButton->setEnabled(false);
    controlsLayout->addWidget(m_triangleMeshButton);

    m_sectionButton = new QPushButton(QStringLiteral("截取数据点"), ui->centralwidget);
    m_sectionButton->setEnabled(false);
    controlsLayout->addWidget(m_sectionButton);

    m_pickPlaneButton = new QPushButton(QStringLiteral("选择截取平面"), ui->centralwidget);
    m_pickPlaneButton->setEnabled(false);
    controlsLayout->addWidget(m_pickPlaneButton);
    m_cancelPlaneButton = new QPushButton(QStringLiteral("取消选点"), ui->centralwidget);
    m_cancelPlaneButton->setEnabled(false);
    controlsLayout->addWidget(m_cancelPlaneButton);

    m_cleanButton = new QPushButton(QStringLiteral("清洗数据点"), ui->centralwidget);
    m_cleanButton->setEnabled(false);
    controlsLayout->addWidget(m_cleanButton);

    m_pickTargetButton = new QPushButton(
        QStringLiteral("选择目标折线"), ui->centralwidget);
    m_pickTargetButton->setEnabled(false);
    controlsLayout->addWidget(m_pickTargetButton);
    m_cancelTargetButton = new QPushButton(
        QStringLiteral("取消目标选点"), ui->centralwidget);
    m_cancelTargetButton->setEnabled(false);
    controlsLayout->addWidget(m_cancelTargetButton);
    m_switchTargetPathButton = new QPushButton(
        QStringLiteral("切换另一侧"), ui->centralwidget);
    m_switchTargetPathButton->setEnabled(false);
    controlsLayout->addWidget(m_switchTargetPathButton);

    m_fitButton = new QPushButton(QStringLiteral("拟合数据点"), ui->centralwidget);
    m_fitButton->setEnabled(false);
    controlsLayout->addWidget(m_fitButton);

    controlsLayout->addStretch();
    auto* surfaceControls = new QHBoxLayout();
    surfaceControls->setContentsMargins(8, 0, 8, 4);
    viewLayout->addLayout(surfaceControls, 0);

    m_view = new OccView(ui->centralwidget);
    viewLayout->addWidget(m_view, 1);

    m_surfaceSlot = new QComboBox(this);
    m_surfaceSlot->addItems({QStringLiteral("面 1 · 红"), QStringLiteral("面 2 · 绿"),
                            QStringLiteral("面 3 · 蓝"), QStringLiteral("面 4 · 紫")});
    surfaceControls->addWidget(m_surfaceSlot);
    m_surfaceSelect = new QPushButton(QStringLiteral("手动选择面片"), this);
    m_surfaceSelect->setCheckable(true);
    m_surfaceSelect->setToolTip(QStringLiteral("点击添加邻近面片；Shift 点击删除；拖动旋转，滚轮缩放。逐个选择四个主体面，避开圆角。"));
    surfaceControls->addWidget(m_surfaceSelect);
    surfaceControls->addWidget(new QLabel(QStringLiteral("选区半径"), this));
    m_surfaceRadius = new QDoubleSpinBox(this);
    m_surfaceRadius->setRange(0.01, 1000); m_surfaceRadius->setDecimals(2);
    m_surfaceRadius->setValue(5); m_surfaceRadius->setSuffix(QStringLiteral(" mm"));
    surfaceControls->addWidget(m_surfaceRadius);
    auto* clearSurface = new QPushButton(QStringLiteral("清空当前面"), this);
    surfaceControls->addWidget(clearSurface);
    m_surfaceFitButton = new QPushButton(QStringLiteral("拟合当前曲面"), this);
    surfaceControls->addWidget(m_surfaceFitButton);
    m_surfaceIntersectionButton = new QPushButton(QStringLiteral("曲面求交"),this);
    m_surfaceIntersectionButton->setToolTip(QStringLiteral("四个面全部拟合后显示交线、裁剪封口，生成可用于布尔相减的实体；可选倒圆角。"));
    surfaceControls->addWidget(m_surfaceIntersectionButton);
    connect(m_surfaceIntersectionButton,&QPushButton::clicked,this,&MainWindow::intersectSurfacePatches);
    m_surfaceStatus = new QLabel(this);
    statusBar()->addPermanentWidget(m_surfaceStatus);
    surfaceControls->addStretch();
    connect(m_surfaceSelect, &QPushButton::toggled, this, [this](bool enabled) {
        if (enabled) {
            cancelPlanePointPicking(); cancelTargetContourPicking();
            statusBar()->showMessage(QStringLiteral("选择四个主体面：点击添加，Shift 点击删除，拖动旋转。选区之间不重复分配面片。"));
        }
        m_view->setSurfaceSelection(enabled ? m_surfaceSlot->currentIndex() : -1, m_surfaceRadius->value());
    });
    auto selectionChanged = [this]() {
        if (m_surfaceSelect->isChecked())
            m_view->setSurfaceSelection(m_surfaceSlot->currentIndex(), m_surfaceRadius->value());
    };
    connect(m_surfaceSlot, &QComboBox::currentIndexChanged, this, selectionChanged);
    connect(m_surfaceRadius, &QDoubleSpinBox::valueChanged, this, selectionChanged);
    connect(clearSurface, &QPushButton::clicked, this, [this]() { m_view->clearSurfacePatch(m_surfaceSlot->currentIndex()); });
    connect(m_surfaceSlot, &QComboBox::currentIndexChanged, this, &MainWindow::updateSurfaceControls);
    connect(m_view, &OccView::surfacePatchEdited, this, &MainWindow::invalidateSurfacePatch);
    connect(m_view, &OccView::surfaceSelectionChanged, this, &MainWindow::updateSurfaceControls);
    connect(m_view, &OccView::surfaceSelectionMessage, this,
            [this](const QString& message) { statusBar()->showMessage(message, 6000); });
    connect(m_surfaceFitButton, &QPushButton::clicked, this, &MainWindow::fitSurfacePatches);
    updateSurfaceControls();

    auto* fileMenu = menuBar()->addMenu("File");
    auto* openAction = new QAction("Open STL", this);
    fileMenu->addAction(openAction);
    auto* exportTargetAction = fileMenu->addAction(QStringLiteral("导出目标轮廓折线…"));
    exportTargetAction->setObjectName(QStringLiteral("exportTargetContourPolylineAction"));
    exportTargetAction->setEnabled(false);
    connect(fileMenu, &QMenu::aboutToShow, this, [this, exportTargetAction]() {
        exportTargetAction->setEnabled(m_currentResult.hasTargetSelection
            && !m_currentResult.contours.empty() && !m_view->isPlanePointPicking()
            && !m_view->isContourPointPicking());
    });
    connect(exportTargetAction, &QAction::triggered,
            this, &MainWindow::exportTargetContourPolyline);

    connect(openAction, &QAction::triggered, this, &MainWindow::openStlFile);
    connect(m_sectionButton, &QPushButton::clicked,
            this, &MainWindow::sectionCurrentData);
    connect(m_pickPlaneButton, &QPushButton::clicked,
            this, &MainWindow::beginPlanePointPicking);
    connect(m_cancelPlaneButton, &QPushButton::clicked,
            this, &MainWindow::cancelPlanePointPicking);
    connect(m_view, &OccView::planePointPicked,
            this, &MainWindow::handlePlanePointPicked);
    connect(m_view, &OccView::planePointPickFailed, this,
            [this](const QString& message) { statusBar()->showMessage(message, 4000); });
    connect(m_pickTargetButton, &QPushButton::clicked,
            this, &MainWindow::beginTargetContourPicking);
    connect(m_cancelTargetButton, &QPushButton::clicked,
            this, &MainWindow::cancelTargetContourPicking);
    connect(m_switchTargetPathButton, &QPushButton::clicked,
            this, &MainWindow::toggleTargetContourPath);
    connect(m_view, &OccView::contourPointPicked,
            this, &MainWindow::handleContourPointPicked);
    connect(m_view, &OccView::contourPointPickFailed, this,
            [this](const QString& message) { statusBar()->showMessage(message, 4000); });
    connect(m_cleanButton, &QPushButton::clicked,
            this, &MainWindow::cleanCurrentData);
    connect(m_fitButton, &QPushButton::clicked,
            this, &MainWindow::fitCurrentData);
    connect(m_triangleMeshButton, &QPushButton::toggled,
            this, [this](bool checked) {
                m_view->setTriangleMeshVisible(checked);
                m_triangleMeshButton->setText(
                    checked
                        ? QStringLiteral("隐藏三角网格")
                        : QStringLiteral("显示三角网格"));
            });

    setWindowTitle("Qt + OpenCascade STL Viewer");
}

MainWindow::~MainWindow()
{
    delete ui;
}

// ============================================================================
// 文件输入
// ============================================================================

void MainWindow::openStlFile()
{
    const QString filePath = QFileDialog::getOpenFileName(
        this,
        "Open STL File",
        QString(),
        "STL Files (*.stl);;All Files (*.*)"
        );

    if (filePath.isEmpty()) {
        return;
    }

    if (!m_view->loadStl(filePath)) {
        QMessageBox::critical(
            this,
            "Load Failed",
            m_view->lastError()
            );
        return;
    }

    m_surfaceSelect->setChecked(false);
    for (auto& face:m_surfaceFaces) face.Nullify();
    for (auto& samples:m_surfaceSamples) samples.clear();
    updateSurfaceControls();
    m_triangleMeshButton->setEnabled(true);
    m_triangleMeshButton->setChecked(false);
    m_triangleMeshButton->setText(QStringLiteral("显示三角网格"));

    m_pipeline = std::make_unique<Stlsection>();
    QString pipelineError;
    if (!m_pipeline->loadStl(filePath, &pipelineError)) {
        qWarning().noquote() << "STL section pipeline load failed:" << pipelineError;
        updateSurfaceControls();
        return;
    }

    const SectionBoundingBox& box = m_pipeline->boundingBox();
    qDebug().noquote()
        << "STL loaded:" << m_pipeline->filePath()
        << "nodes =" << m_pipeline->mesh()->NbNodes()
        << "triangles =" << m_pipeline->mesh()->NbTriangles();
    qDebug().noquote()
        << "Bounding box min =" << box.minimum.X() << box.minimum.Y() << box.minimum.Z()
        << "max =" << box.maximum.X() << box.maximum.Y() << box.maximum.Z()
        << "center =" << box.center.X() << box.center.Y() << box.center.Z()
        << "diagonal =" << box.diagonal;

    m_sectionPlane.reset();
    QString axisError;
    m_hubAxisValid = m_pipeline->estimateHubAxis(
        &m_hubAxisPoint, &m_hubAxisDirection, &m_hubOuterRadius, &axisError);
    if (m_hubAxisValid) {
        m_planePoint = m_hubAxisPoint;
        m_planeRadialDirection = radialDirectionForPlane(
            m_hubAxisDirection, m_pipeline->defaultPlaneNormal());
        m_planeNormal = gp_Dir(
            gp_Vec(m_hubAxisDirection).Crossed(
                gp_Vec(m_planeRadialDirection)));
        m_view->displaySectionPlane(
            m_planePoint, m_hubAxisDirection,
            m_planeRadialDirection, box.diagonal);
        m_view->displayHubAxis(
            m_hubAxisPoint, m_hubAxisDirection, box.diagonal);
        m_pickPlaneButton->setToolTip(
            QStringLiteral("选择最外围外圆上的一点，截面将从中心轴向该点方向延伸。"));
    } else {
        m_view->clearSectionPlane();
        m_pickPlaneButton->setToolTip(axisError);
        statusBar()->showMessage(axisError, 8000);
    }
    if (m_hubAxisValid && !axisError.isEmpty()) {
        m_pickPlaneButton->setToolTip(axisError);
    }

    m_currentSource = DataSource::StlSection;
    m_currentSourcePath = filePath;
    updateSurfaceControls();
    m_currentResult = SectionResult{};
    m_targetSelection = TargetContourSelection{};
    m_targetPickCount = 0;
    clearResultPresentation();
    m_sectionButton->setEnabled(m_hubAxisValid);
    m_pickPlaneButton->setEnabled(m_hubAxisValid);
    m_cancelPlaneButton->setEnabled(false);
    m_cleanButton->setEnabled(false);
    m_pickTargetButton->setEnabled(false);
    m_cancelTargetButton->setEnabled(false);
    m_switchTargetPathButton->setEnabled(false);
    m_fitButton->setEnabled(false);
    statusBar()->showMessage(QStringLiteral("STL 已加载，请先截取数据点。"));
}

// ============================================================================
// 数据处理流程：截面、清洗、拟合
// ============================================================================

void MainWindow::sectionCurrentData()
{
    if (m_currentSource != DataSource::StlSection || !m_pipeline) {
        return;
    }

    if (m_view->isPlanePointPicking()
        || m_view->isContourPointPicking()) {
        statusBar()->showMessage(QStringLiteral("请先完成选点或取消选点。"));
        return;
    }

    try {
        m_view->displaySectionPlane(
            m_planePoint,
            m_hubAxisDirection,
            m_planeRadialDirection,
            m_pipeline->boundingBox().diagonal);

        const SectionRequest request{m_planePoint, m_hubAxisDirection, m_planeRadialDirection};
        m_currentResult = SectionWorkflow::compute(*m_pipeline, request);
    } catch (const Standard_Failure& failure) {
        m_currentResult = SectionResult{};
        m_currentResult.error = QStringLiteral("截取数据点时 Open CASCADE 发生错误：%1")
            .arg(QString::fromLatin1(failure.what()));
        QMessageBox::warning(this,
                             QStringLiteral("Section Failed"),
                             m_currentResult.error);
        return;
    } catch (const std::exception& exception) {
        m_currentResult = SectionResult{};
        m_currentResult.error = QStringLiteral("截取数据点时发生错误：%1")
            .arg(QString::fromLocal8Bit(exception.what()));
        QMessageBox::warning(this,
                             QStringLiteral("Section Failed"),
                             m_currentResult.error);
        return;
    }
    if (!m_currentResult.success) {
        QMessageBox::warning(this,
                             QStringLiteral("Section Failed"),
                             m_currentResult.error);
        return;
    }

    clearResultPresentation();
    m_targetSelection = TargetContourSelection{};
    m_targetPickCount = 0;
    m_cleanButton->setEnabled(!m_currentResult.rawPoints.empty());
    m_pickTargetButton->setEnabled(false);
    m_cancelTargetButton->setEnabled(false);
    m_switchTargetPathButton->setEnabled(false);
    m_fitButton->setEnabled(false);

    const SectionTolerances& tolerances = m_currentResult.tolerances;
    qDebug().noquote()
        << "Section plane point ="
        << m_planePoint.X() << m_planePoint.Y() << m_planePoint.Z()
        << "normal ="
        << m_planeNormal.X() << m_planeNormal.Y() << m_planeNormal.Z();
    qDebug().noquote()
        << "Section tolerances: plane =" << tolerances.plane
        << "minimumSegment =" << tolerances.minimumSegment
        << "duplicatePoint =" << tolerances.duplicatePoint
        << "fitting =" << tolerances.fitting;
    qDebug().noquote()
        << "Section points: segments =" << m_currentResult.rawSegments.size()
        << "points =" << m_currentResult.rawPoints.size();

    const QString outputDirectory = sectionOutputDirectory();
    QString error;
    if (!m_pipeline->saveSectionPoints(
            m_currentResult, outputDirectory, &error)) {
        QMessageBox::warning(this,
                             QStringLiteral("Export Failed"),
                             error);
        return;
    }

    updateResultStatus(outputDirectory);
}

void MainWindow::cleanCurrentData()
{
    if (m_currentSource == DataSource::None
        || m_currentResult.rawPoints.empty()) {
        return;
    }

    if (!Stlsection::cleanSectionResult(&m_currentResult, m_processingOptions)) {
        QMessageBox::warning(this,
                             QStringLiteral("Cleaning Failed"),
                             m_currentResult.error);
        return;
    }

    clearResultPresentation();
    m_targetSelection = TargetContourSelection{};
    m_targetPickCount = 0;
    m_pickTargetButton->setEnabled(!m_currentResult.contours.empty());
    m_cancelTargetButton->setEnabled(false);
    m_switchTargetPathButton->setEnabled(false);
    m_fitButton->setEnabled(false);
    if (!m_currentResult.contours.empty()) {
        m_view->displayContourComparison(
            m_currentResult.beforeBumpContours,
            m_currentResult.contours, true, true, false);
    }
    qDebug().noquote()
        << "Cleaned data: rawPoints =" << m_currentResult.rawPoints.size()
        << "cleanedPoints =" << m_currentResult.cleanedPoints.size()
        << "invalidPoints =" << m_currentResult.invalidPointCount
        << "duplicatePoints =" << m_currentResult.duplicatePointCount
        << "repairedBumps =" << m_currentResult.repairedBumpCount
        << "movedBumpPoints =" << m_currentResult.movedBumpPointCount
        << "maximumBumpMovement =" << m_currentResult.maximumBumpMovement
        << "skippedContours =" << m_currentResult.skippedContourCount
        << "contours =" << m_currentResult.contours.size();
    qDebug().noquote() << "Bump candidate rejections: support ="
        << m_currentResult.bumpSupportRejected
        << "shape =" << m_currentResult.bumpShapeRejected
        << "movementBudget =" << m_currentResult.bumpBudgetRejected
        << "safety =" << m_currentResult.bumpSafetyRejected;
    for (int contourIndex = 0;
         contourIndex < static_cast<int>(m_currentResult.contours.size());
         ++contourIndex) {
        const SectionContour& contour =
            m_currentResult.contours[static_cast<std::size_t>(contourIndex)];
        qDebug().noquote()
            << "Cleaned contour" << contourIndex
            << "points =" << contour.cleanedPoints.size()
            << "closed =" << contour.closed;
    }
    updateResultStatus();
}

void MainWindow::beginPlanePointPicking()
{
    m_surfaceSelect->setChecked(false);
    if (m_currentSource != DataSource::StlSection || !m_pipeline) {
        return;
    }
    if (!m_hubAxisValid) {
        statusBar()->showMessage(
            QStringLiteral("无法识别轮毂中心轴，当前模型不能按外圆点定义截面。"), 6000);
        return;
    }
    // 截面平面改变后，之前的清洗轮廓和目标点不再有效。
    m_view->clearFittedContours();
    m_view->cancelContourPointPicking();
    m_currentResult = SectionResult{};
    m_targetSelection = TargetContourSelection{};
    m_targetPickCount = 0;
    m_cleanButton->setEnabled(false);
    m_pickTargetButton->setEnabled(false);
    m_cancelTargetButton->setEnabled(false);
    m_switchTargetPathButton->setEnabled(false);
    m_fitButton->setEnabled(false);
    m_sectionPlane.setAxis(
        m_hubAxisPoint, m_hubAxisDirection, m_hubOuterRadius,
        std::max(m_hubOuterRadius * 0.025,
                 m_pipeline->boundingBox().diagonal * 0.002));
    m_view->setPlanePointPicking(
        true, &m_sectionPlane,
        std::max(m_pipeline->boundingBox().diagonal * 1.0e-7, 1.0e-7));
    m_pickTargetButton->setEnabled(false);
    m_cancelTargetButton->setEnabled(false);
    m_switchTargetPathButton->setEnabled(false);
    setPlanePickingControls(true);
    statusBar()->showMessage(
        QStringLiteral("请捕获最外围外圆上的一个 STL 原始顶点；青色直线为轮毂中心轴。"));
}

void MainWindow::cancelPlanePointPicking()
{
    m_view->cancelPlanePointPicking();
    m_sectionPlane.reset();
    setPlanePickingControls(false);
    m_pickTargetButton->setEnabled(!m_currentResult.contours.empty());
    statusBar()->showMessage(QStringLiteral("已取消平面选点。"));
}

void MainWindow::handlePlanePointPicked(const gp_Pnt& point)
{
    // SectionPlane 已在 OccView 发出信号前保存了拾取点，此处只消费完成状态。
    Q_UNUSED(point);
    const int count = m_sectionPlane.pointCount();
    if (count < 1) {
        statusBar()->showMessage(
            QStringLiteral("请捕获最外围外圆上的一个 STL 原始顶点。"));
        return;
    }
    if (!m_sectionPlane.isDefined()) {
        return;
    }

    m_planePoint = m_sectionPlane.point();
    m_planeNormal = m_sectionPlane.normal();
    m_planeRadialDirection = m_sectionPlane.radialDirection();
    m_view->cancelPlanePointPicking();
    m_view->displaySectionPlane(
        m_planePoint, m_hubAxisDirection, m_planeRadialDirection,
        m_pipeline->boundingBox().diagonal);
    setPlanePickingControls(false);
    m_pickTargetButton->setEnabled(!m_currentResult.contours.empty());
    statusBar()->showMessage(
        QStringLiteral("截取半平面以轮毂中心轴为边界，并向所选外圆点方向延伸。"));
}

void MainWindow::beginTargetContourPicking()
{
    m_surfaceSelect->setChecked(false);
    if (m_currentSource != DataSource::StlSection
        || !m_pipeline
        || m_view == nullptr
        || m_currentResult.contours.empty()) {
        return;
    }
    if (m_view->isPlanePointPicking()) {
        statusBar()->showMessage(QStringLiteral("请先完成截面平面选点。"));
        return;
    }

    try {
    m_targetSelection = TargetContourSelection{};
    m_targetPickCount = 0;
    m_currentResult.targetSelection = TargetContourSelection{};
    m_currentResult.hasTargetSelection = false;
    m_currentResult.hasSelectedContour = false;
    m_currentResult.selectedContour = SectionContour{};
    if (m_curveComparisonWindow != nullptr) {
        m_curveComparisonWindow->hide();
    }
    // 开始拾取时不重复销毁并重建 OCCT 轮廓对象。
    // 清洗阶段已经显示了原始轮廓，拾取只需要切换输入状态即可；
    // 这样也避免在 AIS 对象仍处于重绘周期时进行二次 Remove/Display。
    m_view->setContourPointPicking(
        true,
        m_currentResult.contours,
        std::max(m_pipeline->boundingBox().diagonal * 1.0e-6, 1.0e-7));
    if (!m_view->isContourPointPicking()) {
        setTargetPickingControls(false);
        statusBar()->showMessage(
            QStringLiteral("目标轮廓投影初始化失败，请重试。"), 8000);
        return;
    }
    setTargetPickingControls(true);
    statusBar()->showMessage(
        QStringLiteral("请在同一条闭合截面轮廓上选择第一个目标点。"));
    } catch (const Standard_Failure& failure) {
        m_view->cancelContourPointPicking();
        setTargetPickingControls(false);
        statusBar()->showMessage(
            QStringLiteral("目标折线拾取初始化失败：%1")
                .arg(QString::fromLocal8Bit(failure.GetMessageString())),
            8000);
    } catch (const std::exception& failure) {
        m_view->cancelContourPointPicking();
        setTargetPickingControls(false);
        statusBar()->showMessage(
            QStringLiteral("目标折线拾取初始化失败：%1")
                .arg(QString::fromLocal8Bit(failure.what())),
            8000);
    } catch (...) {
        m_view->cancelContourPointPicking();
        setTargetPickingControls(false);
        statusBar()->showMessage(
            QStringLiteral("目标折线拾取初始化失败，请重试。"), 8000);
    }
}

void MainWindow::cancelTargetContourPicking()
{
    m_view->cancelContourPointPicking();
    m_targetSelection = TargetContourSelection{};
    m_targetPickCount = 0;
    m_currentResult.targetSelection = TargetContourSelection{};
    m_currentResult.hasTargetSelection = false;
    m_currentResult.hasSelectedContour = false;
    m_currentResult.selectedContour = SectionContour{};
    if (m_curveComparisonWindow != nullptr) {
        m_curveComparisonWindow->hide();
    }
    m_view->displayContourComparison(
        m_currentResult.contours,
        true,
        false);
    setTargetPickingControls(false);
    statusBar()->showMessage(QStringLiteral("已取消目标折线选点。"));
}

void MainWindow::handleContourPointPicked(int contourIndex,
                                          int pointIndex,
                                          const gp_Pnt& point)
{
    // 视图在发出信号前已验证这些索引，但槽函数仍需防御性检查，
    // 因为排队的界面事件也可能调用该槽函数。
    if (contourIndex < 0
        || contourIndex >= static_cast<int>(m_currentResult.contours.size())
        || pointIndex < 0
        || pointIndex >= static_cast<int>(m_currentResult.contours[
            static_cast<std::size_t>(contourIndex)].cleanedPoints.size())) {
        statusBar()->showMessage(
            QStringLiteral("目标点索引无效，请重新选择。"), 5000);
        return;
    }
    if (m_targetPickCount == 0) {
        m_targetSelection.first = {
            contourIndex,
            pointIndex,
            point
        };
        m_targetPickCount = 1;
        statusBar()->showMessage(
            QStringLiteral("已选择第一个点，请在同一条轮廓上选择第二个点。"));
        return;
    }

    if (contourIndex != m_targetSelection.first.contourIndex) {
        statusBar()->showMessage(
            QStringLiteral("第二个点必须位于第一个点所在的同一条闭合轮廓上。"));
        return;
    }
    if (pointIndex == m_targetSelection.first.pointIndex) {
        statusBar()->showMessage(
            QStringLiteral("两个目标点不能是同一个点，请重新选择。"));
        return;
    }

    m_targetSelection.second = {
        contourIndex,
        pointIndex,
        point
    };
    m_targetSelection.valid = true;
    m_currentResult.targetSelection = m_targetSelection;
    m_currentResult.hasTargetSelection = true;
    m_targetPickCount = 2;
    // 停止捕获但保留两个标记，便于用户确认选择结果。
    m_view->setContourPointPicking(false, {}, 0.0);
    setTargetPickingControls(false);
    statusBar()->showMessage(
        QStringLiteral("目标折线已选择，可切换另一侧或直接拟合。"));
}

void MainWindow::toggleTargetContourPath()
{
    if (!m_currentResult.hasTargetSelection) {
        return;
    }
    m_targetSelection.alternatePath = !m_targetSelection.alternatePath;
    m_currentResult.targetSelection = m_targetSelection;
    m_currentResult.hasSelectedContour = false;
    m_currentResult.selectedContour = SectionContour{};
    if (m_curveComparisonWindow != nullptr) {
        m_curveComparisonWindow->hide();
    }
    m_fitButton->setEnabled(true);
    statusBar()->showMessage(
        m_targetSelection.alternatePath
            ? QStringLiteral("已切换到另一侧目标折线。")
            : QStringLiteral("已切换回较短侧目标折线。"));
}

void MainWindow::displayTargetSelectionPreview()
{
    SectionContour targetContour;
    QString error;
    if (!Stlsection::selectTargetContour(
            m_currentResult,
            m_currentResult.targetSelection,
            &targetContour,
            &error)) {
        m_currentResult.error = error;
        m_currentResult.hasTargetSelection = false;
        m_fitButton->setEnabled(false);
        statusBar()->showMessage(error, 5000);
        return;
    }
    // 复用比较视图的拟合图层显示目标折线，使其与完整轮廓颜色区分。
    targetContour.fittedPoints = targetContour.cleanedPoints;
    // 拾取阶段不触碰 AIS/OpenGL 对象；拟合时统一显示目标曲线。
    Q_UNUSED(targetContour);
    m_fitButton->setEnabled(true);
}

void MainWindow::exportTargetContourPolyline()
{
    if (!m_currentResult.hasTargetSelection) {
        statusBar()->showMessage(QStringLiteral("请先选择目标轮廓折线的两个点。"), 5000);
        return;
    }
    const QString suggestedPath = QDir(QFileInfo(m_currentSourcePath).absolutePath())
        .filePath(QFileInfo(m_currentSourcePath).completeBaseName()
                  + QStringLiteral("_target_contour.csv"));
    QString filePath = QFileDialog::getSaveFileName(
        this, QStringLiteral("导出目标轮廓折线"), suggestedPath,
        QStringLiteral("CSV 文件 (*.csv)"));
    if (filePath.isEmpty()) return;
    if (QFileInfo(filePath).suffix().isEmpty()) filePath += QStringLiteral(".csv");
    QString error;
    if (!Stlsection::saveTargetContourPolyline(m_currentResult, filePath, &error)) {
        QMessageBox::warning(this, QStringLiteral("导出失败"), error);
        return;
    }
    statusBar()->showMessage(QStringLiteral("目标轮廓折线已导出到 %1").arg(filePath), 8000);
}

void MainWindow::fitCurrentData()
{
    if (m_currentResult.contours.empty()) {
        QMessageBox::warning(this,
                             QStringLiteral("Fitting Failed"),
                             QStringLiteral("请先清洗数据点。"));
        return;
    }

    if (!m_currentResult.hasTargetSelection) {
        QMessageBox::warning(
            this,
            QStringLiteral("Fitting Failed"),
            QStringLiteral("请先选择目标折线的两个点。"));
        return;
    }

    bool fitSucceeded = false;
    m_view->clearFittedContours();
    if (m_curveComparisonWindow != nullptr) m_curveComparisonWindow->hide();
    try {
        fitSucceeded = Stlsection::fitSectionResult(&m_currentResult, m_processingOptions);
    } catch (const Standard_Failure& failure) {
        m_currentResult.error = QStringLiteral("拟合过程中 Open CASCADE 发生错误：%1")
            .arg(QString::fromLatin1(failure.GetMessageString()));
        m_currentResult.hasSelectedContour = false;
    } catch (const std::exception& failure) {
        m_currentResult.error = QStringLiteral("拟合过程中发生错误：%1")
            .arg(QString::fromLocal8Bit(failure.what()));
        m_currentResult.hasSelectedContour = false;
    } catch (...) {
        m_currentResult.error = QStringLiteral("拟合过程中发生未知错误。");
        m_currentResult.hasSelectedContour = false;
    }
    if (!fitSucceeded) {
        QMessageBox::warning(this,
                             QStringLiteral("Fitting Failed"),
                             m_currentResult.error);
        return;
    }

    if (!m_currentResult.hasSelectedContour) {
        QMessageBox::warning(this,
                             QStringLiteral("Fitting Failed"),
                             QStringLiteral("没有可显示的目标折线。"));
        return;
    }
    m_view->displayFittedContours(
        std::vector<SectionContour>{m_currentResult.selectedContour});
    if (m_curveComparisonWindow == nullptr) {
        m_curveComparisonWindow = new CurveComparisonWindow(this);
    }
    m_curveComparisonWindow->setContours(
        std::vector<SectionContour>{m_currentResult.selectedContour},
        m_currentResult.contours,
        m_currentResult.beforeBumpContours);
    m_curveComparisonWindow->show();
    m_curveComparisonWindow->raise();
    m_curveComparisonWindow->activateWindow();
    qDebug().noquote()
        << "Fitted data: fittedContours ="
        << m_currentResult.fittedContourCount
        << "repairedBumps =" << m_currentResult.repairedBumpCount
        << "movedBumpPoints =" << m_currentResult.movedBumpPointCount
        << "maximumBumpMovement =" << m_currentResult.maximumBumpMovement
        << "skippedContours =" << m_currentResult.skippedContourCount;
    const SectionContour& fitted = m_currentResult.selectedContour;
    for (int segmentIndex = 0;
         segmentIndex < static_cast<int>(fitted.fittedSegments.size());
         ++segmentIndex) {
        const FittedCurveSegment& segment = fitted.fittedSegments[
            static_cast<std::size_t>(segmentIndex)];
        qDebug().noquote()
            << "Fitted segment" << segmentIndex
            << "degree =" << segment.degree
            << "poles =" << (segment.curve.IsNull() ? 0 : segment.curve->NbPoles())
            << "maximumDeviation =" << segment.maximumDeviation
            << "status =" << static_cast<int>(segment.status);
    }

    const QString outputDirectory = sectionOutputDirectory();
    QString error;
    // 只有通过连续性和最大最短距离误差验收的三次曲线才允许导出。
    bool saved = true;
    if (m_currentSource == DataSource::StlSection
        && m_pipeline
        && m_currentResult.fittedContourCount > 0) {
        saved = m_pipeline->saveFittedBSplines(
            m_currentResult, outputDirectory, &error);
    }
    if (!saved) {
        QMessageBox::warning(this, QStringLiteral("Export Failed"), error);
        return;
    }

    updateResultStatus(outputDirectory);
}

// ============================================================================
// 状态栏反馈
// ============================================================================

void MainWindow::clearResultPresentation()
{
    m_view->clearFittedContours();
    m_view->cancelContourPointPicking();
    if (m_curveComparisonWindow != nullptr) {
        m_curveComparisonWindow->hide();
    }
}

void MainWindow::setPlanePickingControls(bool picking)
{
    const bool canDefinePlane =
        m_currentSource == DataSource::StlSection && m_hubAxisValid;
    m_pickPlaneButton->setEnabled(canDefinePlane && !picking);
    m_cancelPlaneButton->setEnabled(picking);
    m_sectionButton->setEnabled(canDefinePlane && !picking);
    if (picking) {
        m_pickTargetButton->setEnabled(false);
        m_cancelTargetButton->setEnabled(false);
        m_switchTargetPathButton->setEnabled(false);
    } else {
        m_pickTargetButton->setEnabled(!m_currentResult.contours.empty());
    }
}

void MainWindow::setTargetPickingControls(bool picking)
{
    const bool hasContours =
        m_currentSource == DataSource::StlSection
        && !m_currentResult.contours.empty();
    m_pickTargetButton->setEnabled(hasContours && !picking);
    m_cancelTargetButton->setEnabled(picking);
    m_switchTargetPathButton->setEnabled(
        hasContours && m_currentResult.hasTargetSelection && !picking
        && m_targetSelection.first.contourIndex >= 0
        && m_targetSelection.first.contourIndex
            < static_cast<int>(m_currentResult.contours.size())
        && m_currentResult.contours[static_cast<std::size_t>(
            m_targetSelection.first.contourIndex)].closed);
    m_fitButton->setEnabled(
        hasContours && m_currentResult.hasTargetSelection && !picking);
}

QString MainWindow::sectionOutputDirectory() const
{
    if (m_currentSourcePath.isEmpty()) {
        return QString();
    }
    return QDir(QFileInfo(m_currentSourcePath).absolutePath())
        .filePath(QStringLiteral("section_output"));
}

void MainWindow::updateResultStatus(const QString& outputDirectory)
{
    QString message = QStringLiteral("数据处理完成");
    if (m_currentResult.fittedContourCount > 0
        && m_currentResult.selectedContour.compliant) {
        const SectionContour& fitted = m_currentResult.selectedContour;
        message = QStringLiteral("平滑三次拟合达标：最大最短距离误差 %1 mm < %2 mm，控制点 %3 个")
            .arg(fitted.maximumDeviation, 0, 'g', 5)
            .arg(fitted.fittingTolerance, 0, 'g', 5)
            .arg(fitted.curve->NbPoles());
    }
    if (!m_currentResult.contours.empty()) {
        message += QStringLiteral("，修整微小突起 %1 处，移动 %2 个点，最大位移 %3")
            .arg(m_currentResult.repairedBumpCount)
            .arg(m_currentResult.movedBumpPointCount)
            .arg(m_currentResult.maximumBumpMovement, 0, 'g', 5);
    }
    if (!outputDirectory.isEmpty()) {
        message += QStringLiteral("，文件已保存到 %1").arg(outputDirectory);
    }
    statusBar()->showMessage(message);
}


void MainWindow::updateSurfaceControls()
{
    const bool loaded=m_pipeline && m_pipeline->isLoaded();
    m_surfaceSelect->setEnabled(loaded);
    bool allFitted=loaded;
    QStringList counts;
    for (int patch=0;patch<4;++patch) {
        const int count=m_view->surfacePatchTriangleCount(patch);
        counts << QStringLiteral("%1:%2%3").arg(patch+1).arg(count)
            .arg(m_surfaceFaces[patch].IsNull()?QString():QStringLiteral("已拟合"));
        allFitted=allFitted && !m_surfaceFaces[patch].IsNull();
    }
    m_surfaceStatus->setText(QStringLiteral("面片数 [%1]").arg(counts.join(QStringLiteral(" / "))));
    m_surfaceFitButton->setEnabled(loaded && m_view->surfacePatchTriangleCount(m_surfaceSlot->currentIndex())>=3);
    m_surfaceIntersectionButton->setEnabled(allFitted);
}

void MainWindow::invalidateSurfacePatch(int patch)
{
    if(patch<0 || patch>=4) return;
    m_surfaceFaces[patch].Nullify(); m_surfaceSamples[patch].clear();
    m_view->displaySurfacePatchFit(patch,TopoDS_Shape());
    updateSurfaceControls();
}

QString MainWindow::exportSurfaceResult(const TopoDS_Shape& shape, const QString& prefix,
                                      const QString& description, int patch, double deviation,
                                      double radius, bool filletApplied, const QString& warning, double extension)
{
    STEPControl_Writer writer;
    Interface_Static::SetCVal("write.step.unit","MM");
    if(writer.Transfer(shape,STEPControl_AsIs)!=IFSelect_RetDone)
        throw std::runtime_error("STEP transfer failed");
    std::ostringstream stream;
    if(writer.WriteStream(stream)!=IFSelect_RetDone) throw std::runtime_error("STEP serialization failed");
    const QString directory=sectionOutputDirectory();
    if(!QDir().mkpath(directory)) throw std::runtime_error("Cannot create output directory");
    const QString base=QDir(directory).filePath(prefix+QStringLiteral("_%1").arg(
        QDateTime::currentDateTime().toString(QStringLiteral("yyyyMMdd_HHmmss_zzz"))));
    QJsonObject metadata;
    metadata[QStringLiteral("source_stl")]=m_currentSourcePath;
    metadata[QStringLiteral("unit")]=QStringLiteral("mm");
    metadata[QStringLiteral("tolerance_mm")]=m_surfaceFitTolerance;
    metadata[QStringLiteral("extension_strategy")]=QStringLiteral("common window-scale side extension; local capped solid; straight axial end-profile continuation outside the STL bounding box");
    metadata[QStringLiteral("result_type")]=description;
    if(radius>0) metadata[QStringLiteral("requested_fillet_radius_mm")]=radius;
    metadata[QStringLiteral("fillet_applied")]=filletApplied;
    metadata[QStringLiteral("warning")]=warning;
    metadata[QStringLiteral("extension_length_mm")]=extension;
    metadata[QStringLiteral("closed_solid")]=shape.ShapeType()==TopAbs_SOLID;
    if(patch>=0) metadata[QStringLiteral("maximum_sample_deviation_mm")]=deviation;
    metadata[QStringLiteral("validation")]=QStringLiteral("one-way distances at vertices, edge midpoints and triangle centroids; extended regions and fillets are not covered by the fit tolerance");
    QJsonArray patches;
    for(int i=0;i<4;++i) {
        if(patch>=0 && patch!=i) continue;
        QJsonObject item; item[QStringLiteral("patch")]=i+1;
        QJsonArray coordinates;
        for(const auto& point:m_surfaceSamples[i]) coordinates.append(QJsonArray{point.X(),point.Y(),point.Z()});
        item[QStringLiteral("validation_samples_mm")]=coordinates;
        patches.append(item);
    }
    metadata[QStringLiteral("patches")]=patches;
    QSaveFile step(base+QStringLiteral(".step")), report(base+QStringLiteral(".json"));
    const auto data=stream.str(); const auto json=QJsonDocument(metadata).toJson();
    if(!step.open(QIODevice::WriteOnly) || !report.open(QIODevice::WriteOnly)
       || step.write(data.data(),qint64(data.size()))!=qint64(data.size()) || report.write(json)!=json.size())
        throw std::runtime_error("Cannot write output files");
    if(!report.commit()) throw std::runtime_error("Cannot commit report");
    if(!step.commit()) {QFile::remove(base+QStringLiteral(".json"));throw std::runtime_error("Cannot commit STEP");}
    return base+QStringLiteral(".step");
}

void MainWindow::fitSurfacePatches()
{
    const int patch=m_surfaceSlot->currentIndex();
    m_surfaceSelect->setChecked(false);
    QProgressDialog progress(QStringLiteral("正在拟合当前曲面，请稍候…"),QString(),0,0,this);
    progress.setWindowModality(Qt::ApplicationModal); progress.setMinimumDuration(0);
    progress.show(); QApplication::processEvents();
    try {
        const auto samples=m_view->surfacePatchSamples(patch);
        SurfaceFitResult result; QString error;
        if(!fitSurfacePatch(samples,m_surfaceFitTolerance,&result,&error)) {
            progress.close(); QMessageBox::warning(this,QStringLiteral("拟合失败"),error); return;
        }
        BRepBuilderAPI_MakeFace maker(result.surface,1e-6);
        if(!maker.IsDone() || !BRepCheck_Analyzer(maker.Face()).IsValid())
            throw std::runtime_error("Invalid fitted face");
        m_surfaceFaces[patch]=maker.Face(); m_surfaceSamples[patch]=samples;
        m_view->displaySurfacePatchFit(patch,m_surfaceFaces[patch]);
        updateSurfaceControls();
        const QString path=exportSurfaceResult(m_surfaceFaces[patch],QStringLiteral("fitted_surface_%1").arg(patch+1),
            result.planar?QStringLiteral("planar local fitted face"):QStringLiteral("local BSpline fitted face"),patch,result.maximumDeviation);
        progress.close();
        statusBar()->showMessage(QStringLiteral("面 %1 已拟合并显示，最大采样偏差 %2 mm；已保存到 %3")
            .arg(patch+1).arg(result.maximumDeviation,0,'g',5).arg(path));
    } catch(const Standard_Failure& failure) {
        progress.close(); QMessageBox::warning(this,QStringLiteral("拟合/导出失败"),QString::fromUtf8(failure.GetMessageString()));
    } catch(const std::exception& failure) {
        progress.close(); QMessageBox::warning(this,QStringLiteral("拟合/导出失败"),QString::fromUtf8(failure.what()));
    }
}

void MainWindow::intersectSurfacePatches()
{
    if(!m_hubAxisValid || !m_pipeline || !m_pipeline->isLoaded()) {
        QMessageBox::warning(this,QStringLiteral("缺少轴向信息"),QStringLiteral("需要有效的轮毂轴向才能将端盖放到模型两侧。请重新加载并确认轮毂轴。"));return;
    }
    bool accepted=false;
    const double radius=QInputDialog::getDouble(this,QStringLiteral("生成窗口切削实体"),
        QStringLiteral("交接圆角半径（mm，0 表示不倒圆角）："),1.0,0,1000,3,&accepted);
    if(!accepted) return;
    m_surfaceSelect->setChecked(false);
    QProgressDialog progress(QStringLiteral("正在检查交线、裁剪侧面并封闭成实体，请稍候…"),QString(),0,0,this);
    progress.setWindowModality(Qt::ApplicationModal);progress.setMinimumDuration(0);
    progress.show();QApplication::processEvents();
    try {
        WindowSolidOptions options;options.axisOrigin=m_hubAxisPoint;options.axisDirection=m_hubAxisDirection;
        options.filletRadius=radius;options.fitTolerance=m_surfaceFitTolerance;
        options.minimumDepth=std::numeric_limits<double>::max();options.maximumDepth=-options.minimumDepth;
        const auto& box=m_pipeline->boundingBox();
        for(int ix=0;ix<2;++ix) for(int iy=0;iy<2;++iy) for(int iz=0;iz<2;++iz) {
            gp_Pnt point(ix?box.maximum.X():box.minimum.X(),iy?box.maximum.Y():box.minimum.Y(),iz?box.maximum.Z():box.minimum.Z());
            const double depth=gp_Vec(m_hubAxisPoint,point).Dot(gp_Vec(m_hubAxisDirection));
            options.minimumDepth=std::min(options.minimumDepth,depth);options.maximumDepth=std::max(options.maximumDepth,depth);
        }
        const double margin=std::max(box.diagonal*0.02,1.0);
        options.minimumDepth-=margin;options.maximumDepth+=margin;
        WindowSolidResult result;QString error;
        if(!buildWindowCuttingSolid(m_surfaceFaces,m_surfaceSamples,options,&result,&error)) {
            m_view->clearSurfaceFit();m_view->displaySurfaceIntersections(result.intersections);
            progress.close();QMessageBox::warning(this,QStringLiteral("切削实体未生成"),error);return;
        }
        QString validation;
        if(!isValidCuttingSolid(result.solid,&validation)) throw std::runtime_error(validation.toStdString());
        const QString path=exportSurfaceResult(result.solid,QStringLiteral("window_cutting_solid"),
            QStringLiteral("validated closed cutting solid with four side supports and two caps outside model bounds"),
            -1,0,radius,result.filletApplied,result.warning,result.extensionLength);
        m_view->displaySurfaceFit(result.solid);m_view->displaySurfaceIntersections(result.intersections);
        progress.close();
        statusBar()->showMessage(QStringLiteral("窗口切削实体已生成（%1），端盖位于模型两侧；已保存到 %2")
            .arg(result.filletApplied?QStringLiteral("已倒圆角"):QStringLiteral("无圆角")).arg(path));
        if(!result.warning.isEmpty()) QMessageBox::warning(this,QStringLiteral("实体已生成，圆角未完成"),result.warning);
    } catch(const Standard_Failure& failure) {
        progress.close();QMessageBox::warning(this,QStringLiteral("切削实体生成失败"),QString::fromUtf8(failure.GetMessageString()));
    } catch(const std::exception& failure) {
        progress.close();QMessageBox::warning(this,QStringLiteral("切削实体生成失败"),QString::fromUtf8(failure.what()));
    }
}
