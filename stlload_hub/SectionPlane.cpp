#include "SectionPlane.h"

#include <gp_Vec.hxx>

#include <algorithm>
#include <cmath>

namespace
{
constexpr double kMinimumVectorLength = 1.0e-12;
constexpr double kMinimumPointTolerance = 1.0e-9;
} // 命名空间

void SectionPlane::reset()
{
    m_pointCount = 0;
    m_isDefined = false;
    m_outerRadius = 0.0;
    m_outerRadiusTolerance = 0.0;
}

void SectionPlane::setAxis(const gp_Pnt& axisPoint,
                           const gp_Dir& axisDirection,
                           double outerRadius,
                           double outerRadiusTolerance)
{
    m_axisPoint = axisPoint;
    m_axisDirection = axisDirection;
    m_outerRadius = std::max(outerRadius, 0.0);
    m_outerRadiusTolerance = std::max(outerRadiusTolerance, 0.0);
    m_pointCount = 0;
    m_isDefined = false;
}

bool SectionPlane::addOuterPoint(const gp_Pnt& point,
                                 double duplicateTolerance,
                                 QString* errorMessage)
{
    // 将拾取点分解为沿轴向和垂直轴向的分量，后者就是候选径向。
    const gp_Vec fromAxis(m_axisPoint, point);
    const gp_Vec axisVector(m_axisDirection);
    const gp_Vec axialOffset = axisVector.Multiplied(fromAxis.Dot(axisVector));
    const double radius = (fromAxis - axialOffset).Magnitude();
    if (std::abs(radius - m_outerRadius) > m_outerRadiusTolerance) {
        if (errorMessage != nullptr) {
            *errorMessage = QStringLiteral(
                "请选择轮毂模型最外围一圈的外圆边缘，当前点不在外圆附近。");
        }
        return false;
    }

    if (point.Distance(m_pickedPoint)
            <= std::max(duplicateTolerance, kMinimumPointTolerance)
        && m_pointCount > 0) {
        if (errorMessage != nullptr) {
            *errorMessage = QStringLiteral("该外圆点已选中，请选择外圆上的其他位置。");
        }
        return false;
    }

    const gp_Vec radialVector = fromAxis - axialOffset;
    if (radialVector.Magnitude() <= kMinimumVectorLength) {
        if (errorMessage != nullptr) {
            *errorMessage = QStringLiteral("所选点无法确定有效的截面平面，请重选外圆点。");
        }
        return false;
    }

    // 法向量按“轴向 x 径向”确定，保证截面坐标系方向稳定。
    const gp_Vec normalVector = axisVector.Crossed(radialVector);
    if (normalVector.Magnitude() <= kMinimumVectorLength) {
        if (errorMessage != nullptr) {
            *errorMessage = QStringLiteral("所选点无法确定有效的截面平面，请重选外圆点。");
        }
        return false;
    }

    m_pickedPoint = point;
    m_planePoint = m_axisPoint;
    m_planeNormal = gp_Dir(normalVector);
    m_radialDirection = gp_Dir(radialVector);
    m_pointCount = 1;
    m_isDefined = true;
    if (errorMessage != nullptr) {
        errorMessage->clear();
    }
    return true;
}

int SectionPlane::pointCount() const
{
    return m_pointCount;
}

bool SectionPlane::isDefined() const
{
    return m_isDefined;
}

gp_Pnt SectionPlane::point() const
{
    return m_planePoint;
}

gp_Dir SectionPlane::normal() const
{
    return m_planeNormal;
}

gp_Dir SectionPlane::radialDirection() const
{
    return m_radialDirection;
}

gp_Pnt SectionPlane::axisPoint() const
{
    return m_axisPoint;
}

gp_Dir SectionPlane::axisDirection() const
{
    return m_axisDirection;
}

gp_Pnt SectionPlane::pickedPoint() const
{
    return m_pickedPoint;
}
