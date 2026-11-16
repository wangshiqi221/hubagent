#ifndef MAINWINDOW_H
#define MAINWINDOW_H

#include "SectionPlane.h"
#include "SectionTypes.h"

#include <QMainWindow>

#include <memory>
#include <vector>
#include <array>
#include <TopoDS_Face.hxx>

class OccView;
class CurveComparisonWindow;
class QPushButton;
class QComboBox;
class QDoubleSpinBox;
class QLabel;
class Stlsection;

QT_BEGIN_NAMESPACE
namespace Ui {
class MainWindow;
}
QT_END_NAMESPACE

class MainWindow : public QMainWindow
{
    Q_OBJECT

public:
    explicit MainWindow(QWidget* parent = nullptr);
    ~MainWindow() override;

private:
    enum class DataSource
    {
        None,
        StlSection
    };

    // 文件加载与“截取 -> 清洗 -> 拟合”工作流。
    void fitSurfacePatches();
    void intersectSurfacePatches();
    void invalidateSurfacePatch(int patch);
    QString exportSurfaceResult(const TopoDS_Shape& shape, const QString& prefix,
                                const QString& description, int patch = -1, double deviation = 0,
                                double radius = 0, bool filletApplied = false,
                                const QString& warning = QString(), double extension = 0);
    void updateSurfaceControls();
    void openStlFile();
    void sectionCurrentData();
    void cleanCurrentData();
    void fitCurrentData();
    void exportTargetContourPolyline();
    void beginPlanePointPicking();
    void cancelPlanePointPicking();
    void handlePlanePointPicked(const gp_Pnt& point);
    void beginTargetContourPicking();
    void cancelTargetContourPicking();
    void toggleTargetContourPath();
    void handleContourPointPicked(int contourIndex,
                                  int pointIndex,
                                  const gp_Pnt& point);
    void displayTargetSelectionPreview();
    void setTargetPickingControls(bool picking);

    // 界面状态辅助函数，集中管理各处理阶段共享的显示逻辑。
    void clearResultPresentation();
    void setPlanePickingControls(bool picking);
    QString sectionOutputDirectory() const;
    void updateResultStatus(const QString& outputDirectory = QString());

private:
    // Qt 控件与辅助窗口（所有权由 Qt 父子对象树管理）。
    Ui::MainWindow* ui;
    OccView* m_view = nullptr;
    QPushButton* m_surfaceSelect = nullptr;
    QPushButton* m_surfaceFitButton = nullptr;
    QPushButton* m_surfaceIntersectionButton = nullptr;
    std::array<TopoDS_Face,4> m_surfaceFaces;
    std::array<std::vector<gp_Pnt>,4> m_surfaceSamples;
    QComboBox* m_surfaceSlot = nullptr;
    double m_surfaceFitTolerance = 0.1; // mm; 曲面容差暂不在界面显示。
    QDoubleSpinBox* m_surfaceRadius = nullptr;
    QLabel* m_surfaceStatus = nullptr;
    QPushButton* m_triangleMeshButton = nullptr;
    QPushButton* m_sectionButton = nullptr;
    QPushButton* m_pickPlaneButton = nullptr;
    QPushButton* m_cancelPlaneButton = nullptr;
    QPushButton* m_cleanButton = nullptr;
    QPushButton* m_pickTargetButton = nullptr;
    QPushButton* m_cancelTargetButton = nullptr;
    QPushButton* m_switchTargetPathButton = nullptr;
    QPushButton* m_fitButton = nullptr;
    CurveComparisonWindow* m_curveComparisonWindow = nullptr;
    // 数据处理状态。
    std::unique_ptr<Stlsection> m_pipeline;
    SectionResult m_currentResult;
    QString m_currentSourcePath;
    // 当前截面坐标系与自动识别出的轮毂轴。
    gp_Pnt m_planePoint;
    gp_Dir m_planeNormal = gp_Dir(0.0, 0.0, 1.0);
    gp_Dir m_planeRadialDirection = gp_Dir(1.0, 0.0, 0.0);
    gp_Pnt m_hubAxisPoint;
    gp_Dir m_hubAxisDirection = gp_Dir(0.0, 0.0, 1.0);
    double m_hubOuterRadius = 0.0;
    bool m_hubAxisValid = false;
    SectionPlane m_sectionPlane;
    TargetContourSelection m_targetSelection;
    SectionProcessingOptions m_processingOptions;
    int m_targetPickCount = 0;
    DataSource m_currentSource = DataSource::None;
};
#endif // 主窗口头文件保护结束
