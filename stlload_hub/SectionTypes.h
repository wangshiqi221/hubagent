#pragma once

#include <gp_Dir.hxx>
#include <gp_Pnt.hxx>
#include <Standard_Handle.hxx>

#include <QString>

#include <vector>

class Geom_BSplineCurve;

// STL 三角形与截面平面的交线段。triangleIndex 使用 OCCT 的 1 基索引；
// 无法追溯来源时保持为 -1。
struct SectionSegment
{
    gp_Pnt first;
    gp_Pnt second;
    int triangleIndex = -1;
};

// 原始截面点，同时保留它在 STL 拓扑中的来源信息。
struct SectionPoint
{
    gp_Pnt point;
    // triangleIndex 为 OCCT 三角形索引；其余两个索引均为结果数组中的 0 基索引。
    int triangleIndex = -1;
    int segmentIndex = -1;
    int pointIndex = -1;
};

// STL 模型或点云的轴对齐包围盒。valid 为 false 时其余字段不可使用。
struct SectionBoundingBox
{
    gp_Pnt minimum;
    gp_Pnt maximum;
    gp_Pnt center;
    double diagonal = 0.0;
    bool valid = false;
};

// 截面计算、点清洗和曲线拟合使用的绝对距离容差，单位与模型一致。
struct SectionTolerances
{
    double plane = 1.0e-7;
    double minimumSegment = 1.0e-6;
    double duplicatePoint = 1.0e-6;
    double fitting = 0.1;
};

// 点清洗和 B 样条拟合的可选参数。
struct SectionProcessingOptions
{
    // 去重容差设为 0 时，根据输入数据的尺寸自动计算。
    double duplicateTolerance = 0.0;
    // 以模型单位表示的绝对长度（默认单位为毫米）。
    bool enableMicroBumpCleaning = true;
    double bumpDetectionTolerance = 0.02; // 最小异常偏差，最大法向偏差必须大于0.02，才继续作为毛刺处理。
    double maximumBumpWidth = 5.0;        // 限制一次最多把多长的一段原始轮廓当作毛刺来修复
    double maximumBumpDisplacement = 2.0; //突起点允许移动的位移
    double bumpReferenceLength = 3.0;     // 每一侧的外部轮廓的支撑长度 Lsupport
    double bumpSupportSearchLength = 6.0; // 越过相邻毛刺，向更远处搜索
    double bumpSupportResidual = 0.01;   // 支撑点和端点的最大残差
    double bumpTangentAngleDegrees = 15.0;
    // 拟合误差要求，模型坐标单位须为毫米；0 使用固定默认值 0.1。
    double fittingTolerance = 0.08;
    // 保留该字段以兼容已有调用方，当前清洗阶段不再按点数删除轮廓。
    int minimumContourPoints = 0;
    // 平滑拟合固定使用四阶（三次）曲线；保留字段供已有调用方使用。
    int maximumOrder = 4;
    int maximumFittingPoles = 512;
    int maximumFittingIterations = 32;
    // 沿目标折线的弧长邻域半宽（模型单位）；0 关闭局部方向参考。
    double fittingDirectionWindow = 1.0;
    // 局部单位切线匹配的软约束权重；0 恢复仅位置和平滑拟合。
    double fittingDirectionWeight = 1.0;
};

enum class CurveSegmentStatus {
    CompliantCubic,
    CompliantQuadratic,
    ExactLinearFeature,
    CompliantLinearFallback,
    Failed
};

enum class CurveJoinType { SmoothG1, SmoothC1, SharpC0 };

struct CurveJoin
{
    CurveJoinType type = CurveJoinType::SharpC0;
    double turningAngleDegrees = 0.0;
};

struct FittedCurveSegment
{
    Handle(Geom_BSplineCurve) curve;
    std::vector<gp_Pnt> sourcePoints;
    std::vector<gp_Pnt> fittedPoints;
    CurveSegmentStatus status = CurveSegmentStatus::Failed;
    CurveJoinType joinAfter = CurveJoinType::SharpC0;
    double joinAngleDegrees = 0.0;
    int degree = 0;
    double maximumDeviation = 0.0;
};

// 一条经过清洗并可能已经完成拟合的有序轮廓。
struct SectionContour
{
    // 闭合轮廓不重复保存首点；closed 决定绘制和拟合时是否补上闭合边。
    std::vector<gp_Pnt> cleanedPoints;
    // fittedPoints 是 curve 的显示采样；拟合失败时可保存原始折线作为回退。
    std::vector<gp_Pnt> fittedPoints;
    Handle(Geom_BSplineCurve) curve;
    std::vector<FittedCurveSegment> fittedSegments;
    std::vector<CurveJoin> joins;
    bool compliant = false;
    bool closed = false;
    // 兼容 CSV 字段；当前使用数据点到曲线的最大最短距离，此值为 false。
    bool deviationIsUpperBound = false;
    double fittingTolerance = 0.0;
    double maximumDeviation = 0.0;
};

// 用户在清洗后的轮廓上选择的目标折线端点。
struct TargetPointRef
{
    int contourIndex = -1;
    int pointIndex = -1;
    gp_Pnt point;
};

// 闭合轮廓有两条路径，alternatePath 用于切换另一侧；开放轮廓只有一条路径。
struct TargetContourSelection
{
    TargetPointRef first;
    TargetPointRef second;
    bool alternatePath = false;
    bool valid = false;
};

// 一次 STL 截面处理的完整结果。
struct SectionResult
{
    bool success = false;
    QString error;

    SectionTolerances tolerances;

    // 用于截面轮廓筛选的局部坐标系。局部 X 轴取轮毂轴方向，
    // radial 取截面从内孔指向外圆的方向。
    gp_Pnt axisPoint;
    gp_Dir axisDirection = gp_Dir(0.0, 0.0, 1.0);
    gp_Dir radialDirection = gp_Dir(1.0, 0.0, 0.0);
    bool hasSectionFrame = false;

    // raw* 是求交阶段输出，cleaned* 和 contours 是清洗阶段输出。
    std::vector<SectionSegment> rawSegments;
    std::vector<SectionPoint> rawPoints;
    std::vector<gp_Pnt> cleanedPoints;
    std::vector<SectionContour> contours;
    // 毛刺修复和人工闭合前的有序真实轮廓。
    std::vector<SectionContour> beforeBumpContours;
    // 清洗后的完整轮廓保留在 contours；拟合阶段使用用户选择的路径。
    TargetContourSelection targetSelection;
    bool hasTargetSelection = false;
    SectionContour selectedContour;
    bool hasSelectedContour = false;

    int repairedBumpCount = 0;
    int bumpSupportRejected = 0;
    int bumpShapeRejected = 0;
    int bumpBudgetRejected = 0;
    int bumpSafetyRejected = 0;
    int movedBumpPointCount = 0;
    double maximumBumpMovement = 0.0;
    int invalidPointCount = 0;
    int duplicatePointCount = 0;
    int skippedContourCount = 0;
    int fittedContourCount = 0;

    double maximumPlaneResidual = 0.0;
};
