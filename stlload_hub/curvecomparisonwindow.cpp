#include "curvecomparisonwindow.h"

#include "occview.h"
#include <Geom_BSplineCurve.hxx>
#include <GeomAPI_ProjectPointOnCurve.hxx>
#include <Standard_Failure.hxx>

#include <algorithm>
#include <cmath>

#include <QHBoxLayout>
#include <QLabel>
#include <QPushButton>
#include <QShowEvent>
#include <QStatusBar>
#include <QTimer>
#include <QVBoxLayout>

CurveComparisonWindow::CurveComparisonWindow(QWidget* parent)
    : QMainWindow(parent)
{
    setWindowTitle(QStringLiteral("原始折线与 B 样条曲线"));
    resize(960, 720);

    auto* centralWidget = new QWidget(this);
    auto* layout = new QVBoxLayout(centralWidget);
    layout->setContentsMargins(0, 0, 0, 0);

    auto* controls = new QHBoxLayout();
    m_showOriginalPolylineButton = new QPushButton(
        QStringLiteral("显示修整后原始折线"), centralWidget);
    m_showTargetPolylineButton = new QPushButton(
        QStringLiteral("隐藏目标折线"), centralWidget);
    m_showFittedButton = new QPushButton(
        QStringLiteral("显示目标拟合曲线"), centralWidget);
    m_statusLabel = new QLabel(
        QStringLiteral("当前显示目标折线，可显示其 B 样条拟合曲线进行对比。"),
        centralWidget);

    m_showBeforeBumpButton = new QPushButton(QStringLiteral("显示未修整原始折线"), centralWidget);
    m_showBeforeBumpButton->setObjectName(QStringLiteral("showBeforeBumpButton"));
    m_showOriginalPolylineButton->setObjectName(QStringLiteral("showAfterBumpButton"));
    controls->addWidget(m_showBeforeBumpButton);
    controls->addWidget(m_showOriginalPolylineButton);
    connect(m_showBeforeBumpButton, &QPushButton::clicked, this, [this]() {
        m_beforeBumpVisible = !m_beforeBumpVisible;
        m_showBeforeBumpButton->setText(m_beforeBumpVisible
            ? QStringLiteral("隐藏未修整原始折线") : QStringLiteral("显示未修整原始折线"));
        refreshComparisonView();
    });
    controls->addWidget(m_showTargetPolylineButton);
    controls->addWidget(m_showFittedButton);
    m_inspectErrorButton = new QPushButton(QStringLiteral("点击查看误差"), centralWidget);
    m_inspectErrorButton->setObjectName(QStringLiteral("inspectErrorButton"));
    m_inspectErrorButton->setCheckable(true);
    controls->addWidget(m_inspectErrorButton);
    controls->addStretch();
    statusBar()->addWidget(m_statusLabel, 1);
    layout->addLayout(controls);
    auto* legend = new QLabel(QStringLiteral(
        "<span style='color:#ff00ff'>━ 未修整原始折线</span>　"
        "<span style='color:#009c9c'>━ 修整后原始折线</span>　"
        "<span style='color:#d97700'>━ 目标折线</span>　"
        "<span style='color:#32a832'>━ 拟合曲线</span>"), centralWidget);
    legend->setContentsMargins(6, 2, 6, 2);
    layout->addWidget(legend);
    m_pointErrorLabel = new QLabel(
        QStringLiteral("可拖动旋转视角；开启“点击查看误差”后选择数据点。"), centralWidget);
    m_pointErrorLabel->setObjectName(QStringLiteral("pointErrorLabel"));
    m_pointErrorLabel->setWordWrap(false);
    m_pointErrorLabel->setSizePolicy(QSizePolicy::Ignored, QSizePolicy::Fixed);
    m_pointErrorLabel->setContentsMargins(6, 2, 6, 2);
    m_pointErrorLabel->setTextInteractionFlags(Qt::TextSelectableByMouse);
    layout->addWidget(m_pointErrorLabel);

    m_view = new OccView(centralWidget);
    layout->addWidget(m_view, 1);
    setCentralWidget(centralWidget);
    connect(m_view, &OccView::contourPointPicked,
            this, &CurveComparisonWindow::showPointError);
    connect(m_view, &OccView::contourPointPickFailed, this,
            [this](const QString& message) { m_pointErrorLabel->setText(message); });

    connect(m_showOriginalPolylineButton, &QPushButton::clicked,
            this, &CurveComparisonWindow::toggleOriginalPolyline);
    connect(m_showTargetPolylineButton, &QPushButton::clicked,
            this, &CurveComparisonWindow::toggleTargetPolyline);
    connect(m_showFittedButton, &QPushButton::clicked,
            this, &CurveComparisonWindow::toggleFittedCurves);
    connect(m_inspectErrorButton, &QPushButton::toggled,
            this, &CurveComparisonWindow::updateErrorInspection);
}

void CurveComparisonWindow::setContours(
    const std::vector<SectionContour>& contours,
    const std::vector<SectionContour>& originalContours,
    const std::vector<SectionContour>& beforeBumpContours)
{
    m_contours = contours;
    m_inspectErrorButton->setChecked(false);
    updateErrorInspection();
    m_originalContours = originalContours;
    m_beforeBumpContours = beforeBumpContours;
    m_beforeBumpVisible = false;
    m_showBeforeBumpButton->setText(QStringLiteral("显示未修整原始折线"));
    m_showBeforeBumpButton->setEnabled(!m_beforeBumpContours.empty());
    m_originalPolylineVisible = false;
    m_targetPolylineVisible = true;
    m_fittedCurvesVisible = false;

    m_showOriginalPolylineButton->setText(QStringLiteral("显示修整后原始折线"));
    m_showOriginalPolylineButton->setEnabled(!m_originalContours.empty());
    m_showTargetPolylineButton->setText(QStringLiteral("隐藏目标折线"));
    m_showTargetPolylineButton->setEnabled(!m_contours.empty());
    m_showFittedButton->setText(QStringLiteral("显示目标拟合曲线"));
    m_showFittedButton->setEnabled(!m_contours.empty());
    m_statusLabel->setText(
        QStringLiteral("当前显示目标折线，可显示其 B 样条拟合曲线进行对比。"));
    if (m_contours.size() == 1 && m_contours.front().compliant) {
        const SectionContour& fitted = m_contours.front();
        m_statusLabel->setText(QStringLiteral("C² 平滑三次曲线，最大最短距离误差 %1 mm < %2 mm，控制点 %3 个")
            .arg(fitted.maximumDeviation, 0, 'g', 5)
            .arg(fitted.fittingTolerance, 0, 'g', 5)
            .arg(fitted.curve.IsNull() ? 0 : fitted.curve->NbPoles()));
    }

    if (isVisible() && m_view != nullptr) {
        QTimer::singleShot(0, this, [this]() {
            if (isVisible() && m_view != nullptr && m_view->isVisible()) {
                refreshComparisonView();
            }
        });
    }
}

void CurveComparisonWindow::toggleOriginalPolyline()
{
    if (m_view == nullptr || m_originalContours.empty()) {
        return;
    }

    m_originalPolylineVisible = !m_originalPolylineVisible;
    m_showOriginalPolylineButton->setText(
        m_originalPolylineVisible
            ? QStringLiteral("隐藏修整后原始折线")
            : QStringLiteral("显示修整后原始折线"));
    refreshComparisonView();
}

void CurveComparisonWindow::toggleTargetPolyline()
{
    if (m_view == nullptr || m_contours.empty()) {
        return;
    }
    m_targetPolylineVisible = !m_targetPolylineVisible;
    m_showTargetPolylineButton->setText(
        m_targetPolylineVisible
            ? QStringLiteral("隐藏目标折线")
            : QStringLiteral("显示目标折线"));
    refreshComparisonView();
}

void CurveComparisonWindow::toggleFittedCurves()
{
    if (m_view == nullptr || m_contours.empty()) {
        return;
    }

    m_fittedCurvesVisible = !m_fittedCurvesVisible;
    m_showFittedButton->setText(
        m_fittedCurvesVisible
            ? QStringLiteral("隐藏目标拟合曲线")
            : QStringLiteral("显示目标拟合曲线"));
    refreshComparisonView();
}

void CurveComparisonWindow::refreshComparisonView()
{
    if (m_view != nullptr) {
        m_view->displayContourComparison(
            m_originalContours,
            m_contours,
            m_originalPolylineVisible,
            m_targetPolylineVisible,
            m_fittedCurvesVisible,
            m_beforeBumpContours,
            m_beforeBumpVisible);
        updateErrorInspection();
    }
}

void CurveComparisonWindow::updateErrorInspection()
{
    const bool available = m_targetPolylineVisible && !m_contours.empty();
    m_inspectErrorButton->setEnabled(available);
    if (!available && m_inspectErrorButton->isChecked()) {
        m_inspectErrorButton->setChecked(false);
        return;
    }
    const bool enabled = available && m_inspectErrorButton->isChecked();
    m_inspectErrorButton->setText(enabled
        ? QStringLiteral("退出误差查看") : QStringLiteral("点击查看误差"));
    m_view->setContourErrorInspection(enabled, m_contours);
    m_pointErrorLabel->setToolTip(QString());
    m_pointErrorLabel->setText(enabled
        ? QStringLiteral("点击目标折线的数据点，查看最短距离误差（mm）；品红线段连接数据点与曲线最近点。")
        : QStringLiteral("可拖动旋转视角；开启“点击查看误差”后选择数据点。"));
}

void CurveComparisonWindow::showPointError(
    int contourIndex, int pointIndex, const gp_Pnt& point)
{
    if (!m_inspectErrorButton->isChecked() || !m_targetPolylineVisible) return;
    if (contourIndex < 0 || contourIndex >= static_cast<int>(m_contours.size())) return;
    const auto& contour = m_contours[static_cast<std::size_t>(contourIndex)];
    m_view->clearContourErrorSegment();
    if (!contour.compliant || contour.curve.IsNull()) {
        m_pointErrorLabel->setText(QStringLiteral("当前目标折线尚无成功的拟合曲线，请先拟合。"));
        return;
    }
    try {
        const auto& curve = contour.curve;
        GeomAPI_ProjectPointOnCurve projection(
            point, curve, curve->FirstParameter(), curve->LastParameter());
        // 即使没有内部投影点，端点也可能是最近点。
        gp_Pnt nearest = curve->Value(curve->FirstParameter());
        double distance = point.Distance(nearest);
        const gp_Pnt end = curve->Value(curve->LastParameter());
        if (point.Distance(end) < distance) { nearest = end; distance = point.Distance(end); }
        if (projection.NbPoints() > 0 && projection.LowerDistance() < distance) {
            nearest = projection.NearestPoint();
            distance = projection.LowerDistance();
        }
        if (!std::isfinite(distance)) throw Standard_Failure("Invalid point distance");
        m_view->showContourErrorSegment(point, nearest);
        m_pointErrorLabel->setText(
            QStringLiteral("目标点 %1（轮廓 %2）  最短距离误差：%3 mm（品红线段；重合时显示点）")
                .arg(pointIndex + 1).arg(contourIndex + 1).arg(distance, 0, 'g', 10));
        m_pointErrorLabel->setToolTip(
            QStringLiteral("数据点：(%1, %2, %3)\n曲线最近点：(%4, %5, %6)")
                .arg(point.X(), 0, 'g', 10).arg(point.Y(), 0, 'g', 10).arg(point.Z(), 0, 'g', 10)
                .arg(nearest.X(), 0, 'g', 10).arg(nearest.Y(), 0, 'g', 10).arg(nearest.Z(), 0, 'g', 10));
    } catch (const Standard_Failure&) {
        m_pointErrorLabel->setText(QStringLiteral("此点的最短距离计算失败，请重新选择。"));
    } catch (const std::exception&) {
        m_pointErrorLabel->setText(QStringLiteral("此点的最短距离计算失败，请重新选择。"));
    }
}

void CurveComparisonWindow::showEvent(QShowEvent* event)
{
    QMainWindow::showEvent(event);
    if (m_view != nullptr && !m_contours.empty()) {
        QTimer::singleShot(0, this, [this]() {
            if (isVisible() && m_view != nullptr && m_view->isVisible()) {
                refreshComparisonView();
            }
        });
    }
}
