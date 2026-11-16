#pragma once

#include "SectionTypes.h"

#include <QMainWindow>

#include <vector>

class OccView;
class QPushButton;
class QLabel;

class CurveComparisonWindow : public QMainWindow
{
    Q_OBJECT

public:
    explicit CurveComparisonWindow(QWidget* parent = nullptr);

    // 目标折线、修整后的完整轮廓，以及微小突起修整前的真实轮廓。
    void setContours(const std::vector<SectionContour>& contours,
                     const std::vector<SectionContour>& originalContours = {},
                     const std::vector<SectionContour>& beforeBumpContours = {});

protected:
    void showEvent(QShowEvent* event) override;

private:
    void toggleOriginalPolyline();
    void toggleTargetPolyline();
    void toggleFittedCurves();
    void refreshComparisonView();
    void updateErrorInspection();
    void showPointError(int contourIndex, int pointIndex, const gp_Pnt& point);

    OccView* m_view = nullptr;
    std::vector<SectionContour> m_contours;
    std::vector<SectionContour> m_originalContours;
    std::vector<SectionContour> m_beforeBumpContours;
    QPushButton* m_showBeforeBumpButton = nullptr;
    bool m_beforeBumpVisible = false;
    QPushButton* m_showOriginalPolylineButton = nullptr;
    QPushButton* m_showTargetPolylineButton = nullptr;
    QPushButton* m_showFittedButton = nullptr;
    QPushButton* m_inspectErrorButton = nullptr;
    QLabel* m_statusLabel = nullptr;
    QLabel* m_pointErrorLabel = nullptr;
    bool m_targetPolylineVisible = true;
    bool m_originalPolylineVisible = false;
    bool m_fittedCurvesVisible = false;
};
