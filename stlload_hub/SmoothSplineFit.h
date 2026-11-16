#pragma once

#include "SectionTypes.h"

// 拟合一条内部节点为单重节点的夹持三次曲线（C2 连续）。
// maximumDeviation 为全部目标数据点到完整拟合曲线的最短距离的最大值。
// 坐标和容差使用同一单位，界面默认单位为毫米。
bool fitSmoothCubicContour(const SectionContour& source,
                           double tolerance,
                           int maximumPoles,
                           int maximumIterations,
                           SectionContour* output,
                           QString* error,
                           double directionWindow = 1.0,
                           double directionWeight = 1.0);
