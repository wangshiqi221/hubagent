#include "SmoothSplineFit.h"

#include <Geom_BSplineCurve.hxx>
#include <GeomAPI_ProjectPointOnCurve.hxx>
#include <NCollection_Array1.hxx>
#include <Standard_Failure.hxx>
#include <gp_Vec.hxx>

#include <QDebug>
#include <algorithm>
#include <array>
#include <cmath>
#include <limits>
#include <vector>

namespace {
constexpr int degree = 3;

struct Reference {
    std::vector<gp_Pnt> points;
    std::vector<double> parameters;
    double length = 0.0;
    double roundoff = 0.0;

    // 对弧长邻域内的分段线性位置作连续最小二乘直线回归。
    // 两点高斯精确积分二次被积函数；点密度不会改变参考方向。
    gp_Vec direction(double u, double window) const
    {
        const double a = std::max(0.0, u - window / length);
        const double z = std::min(1.0, u + window / length);
        const double center = (a + z) * 0.5;
        const gp_Pnt origin = value(center);
        gp_Vec covariance;
        auto it = std::upper_bound(parameters.begin(), parameters.end(), a);
        std::size_t i = std::max<std::size_t>(1, it - parameters.begin());
        for (; i < parameters.size() && parameters[i - 1] < z; ++i) {
            const double left = std::max(a, parameters[i - 1]);
            const double right = std::min(z, parameters[i]);
            const double half = (right - left) * 0.5;
            const double middle = (left + right) * 0.5;
            for (double sign : {-1.0, 1.0}) {
                const double s = middle + sign * half / std::sqrt(3.0);
                covariance += gp_Vec(origin, value(s)) * ((s - center) * half);
            }
        }
        // 折返或极小窗口无法可靠估计方向时，不施加方向惩罚。
        if (covariance.Magnitude() <= roundoff * (z - a) * (z - a))
            return gp_Vec();
        return covariance.Normalized();
    }

    gp_Pnt value(double u) const
    {
        auto found = std::upper_bound(parameters.begin(), parameters.end(), u);
        const std::size_t i = std::clamp<std::size_t>(
            static_cast<std::size_t>(found - parameters.begin()), 1, points.size() - 1);
        const double t = std::clamp((u - parameters[i - 1])
            / (parameters[i] - parameters[i - 1]), 0.0, 1.0);
        return points[i - 1].Translated(gp_Vec(points[i - 1], points[i]) * t);
    }
};

bool prepareReference(const SectionContour& source, Reference* ref)
{
    double coordinateScale = 1.0;
    for (const gp_Pnt& p : source.cleanedPoints) {
        if (!std::isfinite(p.X()) || !std::isfinite(p.Y()) || !std::isfinite(p.Z()))
            return false;
        coordinateScale = std::max({coordinateScale, std::abs(p.X()),
                                   std::abs(p.Y()), std::abs(p.Z())});
        // 仅移除完全重合的连续点；验证参考数据
        // 不允许简化，也不允许按点数截断。
        if (ref->points.empty() || p.Distance(ref->points.back()) > 0.0) {
            if (!ref->points.empty()) ref->length += p.Distance(ref->points.back());
            ref->points.push_back(p);
            ref->parameters.push_back(ref->length);
        }
    }
    if (ref->points.size() < 2 || !std::isfinite(ref->length) || ref->length <= 1.e-12)
        return false;
    for (double& u : ref->parameters) u /= ref->length;
    for (std::size_t i = 1; i < ref->parameters.size(); ++i)
        if (ref->parameters[i] <= ref->parameters[i - 1]) return false;
    ref->parameters.back() = 1.0;
    // 预留数值安全余量；坐标量级极端时直接判定失败。
    ref->roundoff = 512.0 * std::numeric_limits<double>::epsilon()
        * std::max(coordinateScale, ref->length);
    return true;
}

std::vector<double> expandedKnots(const std::vector<double>& knots)
{
    std::vector<double> result(4, 0.0);
    result.insert(result.end(), knots.begin() + 1, knots.end() - 1);
    result.insert(result.end(), 4, 1.0);
    return result;
}

// Cox-de Boor 基函数及其解析导数；积分采样点
// 严格位于节点区间内部，因此此处无需约定端点取值。
double basis(const std::vector<double>& t, int i, int p, double u, int derivative)
{
    if (derivative > p) return 0.0;
    if (p == 0) return t[i] <= u && u < t[i + 1] ? 1.0 : 0.0;
    const double left = t[i + p] - t[i];
    const double right = t[i + p + 1] - t[i + 1];
    if (derivative > 0) {
        return (left > 0.0 ? p / left * basis(t, i, p - 1, u, derivative - 1) : 0.0)
            - (right > 0.0 ? p / right * basis(t, i + 1, p - 1, u, derivative - 1) : 0.0);
    }
    return (left > 0.0 ? (u - t[i]) / left * basis(t, i, p - 1, u, 0) : 0.0)
        + (right > 0.0 ? (t[i + p + 1] - u) / right * basis(t, i + 1, p - 1, u, 0) : 0.0);
}

// 三次曲线的正规矩阵带宽为三；端点控制点被精确消元，
// 无需使用大惩罚权重，也无需事后移动控制点。
struct System {
    std::vector<std::array<double, 4>> matrix;
    std::vector<gp_Vec> rhs;

    explicit System(int n) : matrix(n), rhs(n) {}

    void addRow(int first, const std::array<double, 4>& values,
                double weight, gp_Vec target, const gp_Vec& lastPole)
    {
        const int poleCount = static_cast<int>(matrix.size()) + 2;
        for (int j = 0; j < 4; ++j)
            if (first + j == poleCount - 1) target -= lastPole * values[j];
        // 第一个控制点是归一化坐标系的原点。
        for (int j = 0; j < 4; ++j) {
            const int row = first + j - 1;
            if (row < 0 || row >= static_cast<int>(matrix.size())) continue;
            rhs[row] += target * (weight * values[j]);
            for (int k = 0; k <= j; ++k) {
                const int col = first + k - 1;
                if (col >= 0 && col < static_cast<int>(matrix.size()))
                    matrix[row][row - col] += weight * values[j] * values[k];
            }
        }
    }

    bool solve()
    {
        const int n = static_cast<int>(matrix.size());
        for (int i = 0; i < n; ++i) {
            for (int j = std::max(0, i - 3); j <= i; ++j) {
                double v = matrix[i][i - j];
                for (int k = std::max(0, i - 3); k < j; ++k)
                    v -= matrix[i][i - k] * matrix[j][j - k];
                if (i == j) {
                    if (!std::isfinite(v) || v <= 0.0) return false;
                    matrix[i][0] = std::sqrt(v);
                } else matrix[i][i - j] = v / matrix[j][0];
            }
            for (int j = std::max(0, i - 3); j < i; ++j)
                rhs[i] -= rhs[j] * matrix[i][i - j];
            rhs[i] /= matrix[i][0];
        }
        for (int i = n - 1; i >= 0; --i) {
            for (int j = i + 1; j < std::min(n, i + 4); ++j)
                rhs[i] -= rhs[j] * matrix[j][j - i];
            rhs[i] /= matrix[i][0];
            if (!std::isfinite(rhs[i].X()) || !std::isfinite(rhs[i].Y())
                || !std::isfinite(rhs[i].Z())) return false;
        }
        return true;
    }
};

std::vector<double> partition(const Reference& ref, const std::vector<double>& knots)
{
    std::vector<double> result = ref.parameters;
    result.insert(result.end(), knots.begin(), knots.end());
    std::sort(result.begin(), result.end());
    result.erase(std::unique(result.begin(), result.end()), result.end());
    return result;
}

Handle(Geom_BSplineCurve) solveCurve(const Reference& ref,
    const std::vector<double>& knots, const std::vector<double>& intervals, double lambda,
    double directionWindow, double directionWeight, double tolerance)
{
    const auto t = expandedKnots(knots);
    const int poleCount = static_cast<int>(t.size()) - 4;
    System system(poleCount - 2);
    const gp_Vec lastPole = gp_Vec(ref.points.front(), ref.points.back()) / ref.length;
    const auto row = [&](double u, int derivative) {
        // 极短参考区间内的高斯采样点可能因舍入
        // 变为 1；应确保基函数求值点位于夹持节点域内部。
        u = std::clamp(u, std::nextafter(0.0, 1.0), std::nextafter(1.0, 0.0));
        const int span = static_cast<int>(std::upper_bound(t.begin(), t.end(), u) - t.begin()) - 1;
        std::array<double, 4> values{};
        for (int j = 0; j < 4; ++j) values[j] = basis(t, span - 3 + j, 3, u, derivative);
        return std::make_pair(span - 3, values);
    };
    // 四点高斯求积可精确积分三次曲线相对于
    // 各线性参考段的残差平方；该拟合采用弧长权重，
    // 覆盖线段内部，且不受顶点密度影响。
    constexpr std::array<double, 4> abscissae{
        -0.8611363115940526, -0.3399810435848563, 0.3399810435848563, 0.8611363115940526};
    constexpr std::array<double, 4> weights{
        0.3478548451374538, 0.6521451548625461, 0.6521451548625461, 0.3478548451374538};
    for (std::size_t i = 1; i < intervals.size(); ++i) {
        const double half = (intervals[i] - intervals[i - 1]) * 0.5;
        const double middle = (intervals[i] + intervals[i - 1]) * 0.5;
        for (int j = 0; j < 4; ++j) {
            const double u = middle + half * abscissae[j];
            const auto r = row(u, 0);
            const gp_Vec target = gp_Vec(ref.points.front(), ref.value(u)) / ref.length;
            system.addRow(r.first, r.second, half * weights[j], target, lastPole);
            if (directionWindow > 0.0 && directionWeight > 0.0) {
                const gp_Vec tangent = ref.direction(u, directionWindow);
                if (tangent.SquareMagnitude() > 0.0) {
                    const auto derivative = row(u, 1);
                    // C/length 对归一化弧长求导，参考速度为 1。
                    // 长度比平方使方向项与位置项的尺度保持一致。
                    // 严格位置容差下减弱软约束，避免邻域平滑趋势与
                    // 真实急弯冲突后，单靠加节点仍无法通过距离验收。
                    const double scale = std::min({directionWindow, 10.0 * tolerance, ref.length}) / ref.length;
                    system.addRow(derivative.first, derivative.second,
                        half * weights[j] * directionWeight * scale * scale,
                        tangent, lastPole);
                }
            }
        }
    }
    // 对 |C''|^2 求积分：两点高斯求积在每个三次区间内均精确。
    for (std::size_t i = 1; i < knots.size(); ++i) {
        const double half = (knots[i] - knots[i - 1]) * 0.5;
        const double middle = (knots[i] + knots[i - 1]) * 0.5;
        for (double sign : {-1.0, 1.0}) {
            const auto r = row(middle + sign * half / std::sqrt(3.0), 2);
            system.addRow(r.first, r.second, lambda * half, gp_Vec(), lastPole);
        }
    }
    if (!system.solve()) return {};
    NCollection_Array1<gp_Pnt> poles(1, poleCount);
    poles(1) = ref.points.front();
    poles(poleCount) = ref.points.back();
    for (int i = 1; i < poleCount - 1; ++i)
        poles(i + 1) = ref.points.front().Translated(system.rhs[i - 1] * ref.length);
    NCollection_Array1<double> uniqueKnots(1, static_cast<int>(knots.size()));
    NCollection_Array1<int> multiplicities(1, static_cast<int>(knots.size()));
    for (int i = 0; i < static_cast<int>(knots.size()); ++i) {
        uniqueKnots(i + 1) = knots[i];
        multiplicities(i + 1) = i == 0 || i + 1 == static_cast<int>(knots.size()) ? 4 : 1;
    }
    return new Geom_BSplineCurve(poles, uniqueKnots, multiplicities, degree, false);
}

struct Validation {
    double maximumDistance = 0.0;
    bool invalid = false;
    std::vector<std::pair<double, double>> failures; // 最短距离、数据点弦长参数
};

// 每个数据点到完整曲线的最短距离；同时比较内部投影和两个端点。
Validation validate(const Handle(Geom_BSplineCurve)& curve, const Reference& ref,
                    double tolerance)
{
    Validation result;
    const double first = curve->FirstParameter(), last = curve->LastParameter();
    const gp_Pnt start = curve->Value(first), end = curve->Value(last);
    for (std::size_t i = 0; i < ref.points.size(); ++i) {
        const gp_Pnt& point = ref.points[i];
        GeomAPI_ProjectPointOnCurve projection(point, curve, first, last);
        double distance = std::min(point.Distance(start), point.Distance(end));
        if (projection.NbPoints() > 0)
            distance = std::min(distance, projection.LowerDistance());
        if (!std::isfinite(distance)) {
            result.invalid = true;
            return result;
        }
        result.maximumDistance = std::max(result.maximumDistance, distance);
        if (distance >= tolerance)
            result.failures.emplace_back(distance, ref.parameters[i]);
    }
    return result;
}

void displaySamples(const Handle(Geom_BSplineCurve)& curve, double a, double z,
                    double tolerance, int depth, std::vector<gp_Pnt>* samples)
{
    const auto ca = curve->EvalD1(a), cz = curve->EvalD1(z);
    const gp_Vec edge(ca.Point, cz.Point);
    const double chordError = std::max((ca.D1 * (z - a) - edge).Magnitude(),
                                      (cz.D1 * (z - a) - edge).Magnitude()) / 3.0;
    if (chordError < tolerance || depth >= 12 || samples->size() >= 100000) {
        samples->push_back(cz.Point);
        return;
    }
    const double middle = (a + z) * 0.5;
    displaySamples(curve, a, middle, tolerance, depth + 1, samples);
    displaySamples(curve, middle, z, tolerance, depth + 1, samples);
}
} // 命名空间

bool fitSmoothCubicContour(const SectionContour& source, double tolerance,
    int maximumPoles, int maximumIterations, SectionContour* output, QString* error,
    double directionWindow, double directionWeight)
{
    if (output) *output = SectionContour{};
    if (error) error->clear();
    const auto fail = [&](const QString& reason) {
        if (error) *error = reason;
        return false;
    };
    if (!output || source.closed || !std::isfinite(tolerance) || tolerance <= 0.0
        || maximumPoles < 4 || maximumPoles > 2048
        || maximumIterations < 1 || maximumIterations > 64
        || !std::isfinite(directionWindow) || directionWindow < 0.0
        || !std::isfinite(directionWeight) || directionWeight < 0.0)
        return fail(QStringLiteral("平滑拟合参数无效：需要开放折线、正容差、4～2048 个控制点上限、1～64 次迭代，以及有限非负的方向邻域和权重。"));
    Reference ref;
    if (!prepareReference(source, &ref) || ref.roundoff >= tolerance * 0.01)
        return fail(QStringLiteral("目标折线无效或坐标尺度过大，无法可靠验证指定误差。"));
    const double target = tolerance;
    const double normalizedTolerance = tolerance / ref.length;
    const double initialLambda = normalizedTolerance * normalizedTolerance * 1.e-3;
    const int initialSpans = std::min(8, maximumPoles - 3);
    std::vector<double> knots;
    for (int i = 0; i <= initialSpans; ++i)
        knots.push_back(static_cast<double>(i) / initialSpans);
    double bestMaximumDistance = std::numeric_limits<double>::infinity();
    try {
        for (int iteration = 0; iteration < maximumIterations; ++iteration) {
            const auto intervals = partition(ref, knots);
            Handle(Geom_BSplineCurve) bestCurve;
            Validation bestValidation;
            bestValidation.maximumDistance = std::numeric_limits<double>::infinity();
            // 从最强平滑开始，仅在需要时减弱平滑强度。
            // 保持正惩罚项，确保采样不足的区间仍具有适定解。
            for (int smoothingAttempt = 0; smoothingAttempt < 5; ++smoothingAttempt) {
                const double lambda = initialLambda * std::pow(0.1, smoothingAttempt);
                // 邻域参考是软约束：真实急弯与平滑趋势冲突时，
                // 同步减弱方向权重，绝不通过放宽距离验收来迁就它。
                const double localDirectionWeight = directionWeight * std::pow(0.1, smoothingAttempt);
                auto curve = solveCurve(ref, knots, intervals, lambda,
                    directionWindow, localDirectionWeight, tolerance);
                if (curve.IsNull()) continue;
                auto validation = validate(curve, ref, target);
                if (validation.invalid)
                    return fail(QStringLiteral("最短距离计算得到无效数值，拟合失败。"));
                if (validation.failures.empty() || validation.maximumDistance < bestValidation.maximumDistance) {
                    bestCurve = curve;
                    bestValidation = std::move(validation);
                }
                if (bestValidation.failures.empty()) break;
            }
            if (bestCurve.IsNull())
                return fail(QStringLiteral("平滑拟合求解失败，无法获得可靠的三次曲线。"));
            bestMaximumDistance = bestValidation.maximumDistance;
            qDebug() << "Smooth cubic fit: iteration" << iteration + 1
                     << "poles" << bestCurve->NbPoles() << "maximum point-to-curve distance" << bestMaximumDistance;
            if (bestValidation.failures.empty() && bestMaximumDistance < tolerance) {
                output->cleanedPoints = source.cleanedPoints;
                output->curve = bestCurve;
                output->closed = false;
                output->compliant = true;
                output->maximumDeviation = bestMaximumDistance;
                output->deviationIsUpperBound = false;
                output->fittingTolerance = tolerance;
                output->fittedPoints.push_back(bestCurve->EvalD0(0.0));
                for (std::size_t i = 1; i < knots.size(); ++i)
                    displaySamples(bestCurve, knots[i - 1], knots[i],
                                   std::min(0.01, tolerance * 0.1), 0, &output->fittedPoints);
                FittedCurveSegment segment;
                segment.curve = bestCurve;
                segment.sourcePoints = source.cleanedPoints;
                segment.fittedPoints = output->fittedPoints;
                segment.degree = degree;
                segment.status = CurveSegmentStatus::CompliantCubic;
                segment.maximumDeviation = bestMaximumDistance;
                output->fittedSegments.push_back(std::move(segment));
                return true;
            }
            const int available = maximumPoles - static_cast<int>(knots.size()) - 2;
            if (available <= 0 || iteration + 1 == maximumIterations) break;
            std::sort(bestValidation.failures.begin(), bestValidation.failures.end(),
                      [](const auto& a, const auto& b) { return a.first > b.first; });
            std::vector<double> additions;
            for (const auto& failure : bestValidation.failures) {
                const auto end = std::upper_bound(knots.begin(), knots.end(), failure.second);
                if (end == knots.begin() || end == knots.end()) continue;
                const double middle = (*(end - 1) + *end) * 0.5;
                if (middle <= *(end - 1) + 1.e-12 || middle >= *end - 1.e-12) continue;
                if (std::find(additions.begin(), additions.end(), middle) == additions.end())
                    additions.push_back(middle);
                if (static_cast<int>(additions.size()) >= std::min(32, available)) break;
            }
            if (additions.empty()) break;
            knots.insert(knots.end(), additions.begin(), additions.end());
            std::sort(knots.begin(), knots.end());
        }
    } catch (const Standard_Failure& e) {
        return fail(QStringLiteral("平滑拟合发生几何错误：%1").arg(QString::fromLatin1(e.GetMessageString())));
    }
    return fail(QStringLiteral("达到控制点或迭代上限，未满足 %1 mm 验收要求（最后的最大最短距离误差 %2 mm）。")
        .arg(tolerance, 0, 'g', 6).arg(bestMaximumDistance, 0, 'g', 6));
}
