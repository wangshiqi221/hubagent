#pragma once

#include <gp_Dir.hxx>
#include <gp_Pnt.hxx>

#include <QString>

class SectionPlane
{
public:
    // 清除当前选择，但保留对象可供下一次拾取复用。
    void reset();

    // 配置轮毂轴及允许拾取的外圆半径。调用后需要重新选择外圆点。
    void setAxis(const gp_Pnt& axisPoint, const gp_Dir& axisDirection,
                 double outerRadius, double outerRadiusTolerance);

    // 用外圆点定义半平面：轴线是边界，径向方向由轴线指向该点。
    bool addOuterPoint(const gp_Pnt& point, double duplicateTolerance,
                       QString* errorMessage = nullptr);

    int pointCount() const;
    bool isDefined() const;
    gp_Pnt point() const;
    gp_Dir normal() const;
    gp_Dir radialDirection() const;
    gp_Pnt axisPoint() const;
    gp_Dir axisDirection() const;
    gp_Pnt pickedPoint() const;

private:
    // 当前交互只需要一个外圆点；计数仍单独保存，便于界面显示选择进度。
    int m_pointCount = 0;
    gp_Pnt m_planePoint;
    gp_Dir m_planeNormal = gp_Dir(0.0, 0.0, 1.0);
    gp_Dir m_radialDirection = gp_Dir(1.0, 0.0, 0.0);
    gp_Pnt m_axisPoint;
    gp_Dir m_axisDirection = gp_Dir(0.0, 0.0, 1.0);
    gp_Pnt m_pickedPoint;
    double m_outerRadius = 0.0;
    double m_outerRadiusTolerance = 0.0;
    bool m_isDefined = false;
};
