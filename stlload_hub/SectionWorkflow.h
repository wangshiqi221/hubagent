#pragma once

#include "Stlsection.h"

// 半平面完全由调用方传入的几何参数定义，不读取界面控件。
struct SectionRequest
{
    gp_Pnt axisPoint;
    gp_Dir axisDirection = gp_Dir(0, 0, 1);
    gp_Dir radialDirection = gp_Dir(1, 0, 0);
};

class SectionWorkflow
{
public:
    static SectionResult compute(const Stlsection& model, const SectionRequest& request);

    // 直接准备已清洗的完整闭合轮廓，不人为构造两个端点。
    // 本入口不拟合周期样条、不修补缺口，也不验证自交。
    static bool prepareClosedContour(const SectionResult& result, int contourIndex,
                                     SectionContour* output, QString* error = nullptr);
};
