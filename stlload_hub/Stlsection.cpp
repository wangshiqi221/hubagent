#include "Stlsection.h"
#include "SmoothSplineFit.h"
#include "MicroBumpCleaning.h"

#include <QDir>
#include <QDebug>
#include <QFile>
#include <QFileInfo>
#include <QSaveFile>
#include <QTextStream>
#include <QStringList>

#include <Geom_BSplineCurve.hxx>
#include <Poly_Triangle.hxx>
#include <RWStl.hxx>
#include <Standard_Failure.hxx>
#include <gp_Ax1.hxx>
#include <gp_Mat.hxx>
#include <gp_Vec.hxx>

#include <algorithm>
#include <cmath>
#include <exception>
#include <limits>
#include <set>
#include <unordered_map>
#include <vector>

namespace
{
// ============================================================================
// 常量和截面拓扑辅助类型
// ============================================================================

// 判断浮点分母是否已经接近零，避免线性插值时除以极小值。
constexpr double kZeroLength = 1.0e-12;
constexpr double kEndpointStitchLengthFraction = 0.01;
constexpr double kMaximumEndpointStitchAngleDegrees = 5.0;
constexpr double kMaximumClosureAngleDegrees = 10.0;
constexpr int kMaximumClosureSegmentCount = 10000;
constexpr double kRadiansToDegrees = 57.2957795130823208768;

// 默认截面平面配置，直接修改这些常量即可改变默认方向。
// 默认位置使用模型包围盒中心；自定义位置仅保留给调试或特殊模型。
constexpr bool kUseCustomPlanePosition = false;
constexpr double kCustomPlanePointX = 0.00;
constexpr double kCustomPlanePointY = 0.00;
constexpr double kCustomPlanePointZ = 0.00;
constexpr double kDefaultPlaneRotationXDegrees = 0.0;
constexpr double kDefaultPlaneRotationYDegrees = 0.0;
constexpr double kDefaultPlaneRotationZDegrees = 0.0;
constexpr double kDegreesToRadians = 3.14159265358979323846 / 180.0;

// 空间哈希网格的离散坐标，用来把几何距离接近的点归到同一批候选点中。
struct GridKey
{
    long long x = 0;
    long long y = 0;
    long long z = 0;

    bool operator==(const GridKey& other) const
    {
        return x == other.x && y == other.y && z == other.z;
    }
};

// 为 GridKey 提供哈希值，支持 unordered_map 按网格快速查找点。
struct GridKeyHash
{
    std::size_t operator()(const GridKey& key) const
    {
        std::size_t hash = std::hash<long long>{}(key.x);
        hash ^= std::hash<long long>{}(key.y) + 0x9e3779b9U
            + (hash << 6U) + (hash >> 2U);
        hash ^= std::hash<long long>{}(key.z) + 0x9e3779b9U
            + (hash << 6U) + (hash >> 2U);
        return hash;
    }
};

// 截面线图中的节点：保存唯一点坐标，以及和该点相连的边编号。
struct PointNode
{
    gp_Pnt point;
    std::vector<int> edgeIds;
};

// 截面线图中的边：连接两个已经合并过的点节点。
struct GraphEdge
{
    int first = -1;
    int second = -1;
};

// ============================================================================
// 点和尺寸辅助函数
// ============================================================================

// 检查点坐标是否为有限数，过滤 NaN/Inf 这类无法参与几何计算的数据。
bool isFinitePoint(const gp_Pnt& point)
{
    return std::isfinite(point.X())
        && std::isfinite(point.Y())
        && std::isfinite(point.Z());
}

// 根据一组点计算包围盒；没有有效点时返回 invalid 包围盒。
SectionBoundingBox boundingBoxForPoints(const std::vector<gp_Pnt>& points)
{
    SectionBoundingBox boundingBox;
    bool hasPoint = false;
    double minX = 0.0;
    double minY = 0.0;
    double minZ = 0.0;
    double maxX = 0.0;
    double maxY = 0.0;
    double maxZ = 0.0;

    for (const gp_Pnt& point : points) {
        if (!isFinitePoint(point)) {
            continue;
        }

        if (!hasPoint) {
            minX = maxX = point.X();
            minY = maxY = point.Y();
            minZ = maxZ = point.Z();
            hasPoint = true;
            continue;
        }

        minX = std::min(minX, point.X());
        minY = std::min(minY, point.Y());
        minZ = std::min(minZ, point.Z());
        maxX = std::max(maxX, point.X());
        maxY = std::max(maxY, point.Y());
        maxZ = std::max(maxZ, point.Z());
    }

    if (!hasPoint) {
        return boundingBox;
    }

    boundingBox.minimum = gp_Pnt(minX, minY, minZ);
    boundingBox.maximum = gp_Pnt(maxX, maxY, maxZ);
    boundingBox.center = gp_Pnt(
        (minX + maxX) * 0.5,
        (minY + maxY) * 0.5,
        (minZ + maxZ) * 0.5);
    // 对角线同时作为后续自适应容差的模型尺度基准。
    boundingBox.diagonal = boundingBox.minimum.Distance(boundingBox.maximum);
    boundingBox.valid = true;
    return boundingBox;
}

SectionBoundingBox boundingBoxForMesh(
    const Handle(Poly_Triangulation)& mesh)
{
    SectionBoundingBox boundingBox;
    if (mesh.IsNull() || mesh->NbNodes() <= 0) {
        return boundingBox;
    }

    std::vector<gp_Pnt> points;
    points.reserve(static_cast<std::size_t>(mesh->NbNodes()));
    for (int index = 1; index <= mesh->NbNodes(); ++index) {
        points.push_back(mesh->Node(index));
    }
    return boundingBoxForPoints(points);
}

// 用点云包围盒对角线作为尺度基准，让容差随模型尺寸自适应。
double pointScale(const std::vector<gp_Pnt>& points)
{
    const SectionBoundingBox boundingBox = boundingBoxForPoints(points);
    return boundingBox.valid ? std::max(boundingBox.diagonal, 1.0) : 1.0;
}

// ============================================================================
// 点清洗辅助函数
// ============================================================================

// 根据输入点和用户选项计算清洗、拟合阶段使用的容差。
SectionTolerances processingTolerances(
    const std::vector<gp_Pnt>& points,
    const SectionProcessingOptions& options)
{
    const double scale = pointScale(points);
    const double duplicateTolerance =
        options.duplicateTolerance > 0.0
            ? options.duplicateTolerance
            : std::max(1.0e-9, scale * 1.0e-7);
    const double fittingTolerance =
        options.fittingTolerance > 0.0
            ? options.fittingTolerance
            // 固定绝对容差，假定模型坐标单位为毫米。
            : 0.1;

    SectionTolerances tolerances;
    tolerances.plane = std::max(1.0e-9, scale * 1.0e-7);
    tolerances.minimumSegment = std::max(1.0e-9, scale * 1.0e-6);
    tolerances.duplicatePoint = duplicateTolerance;
    tolerances.fitting = fittingTolerance;
    return tolerances;
}

// 把点坐标映射到空间哈希单元，便于在容差范围内合并重复点。
GridKey gridKeyFor(const gp_Pnt& point, double cellSize)
{
    return {
        static_cast<long long>(std::floor(point.X() / cellSize)),
        static_cast<long long>(std::floor(point.Y() / cellSize)),
        static_cast<long long>(std::floor(point.Z() / cellSize))
    };
}

bool pointsCoincide(const gp_Pnt& first,
                    const gp_Pnt& second,
                    double tolerance)
{
    if (tolerance <= 0.0) {
        return first.X() == second.X()
            && first.Y() == second.Y()
            && first.Z() == second.Z();
    }
    return first.SquareDistance(second) <= tolerance * tolerance;
}

double angleDegrees(const gp_Vec& first, const gp_Vec& second)
{
    if (first.SquareMagnitude() <= kZeroLength * kZeroLength
        || second.SquareMagnitude() <= kZeroLength * kZeroLength) {
        return 180.0;
    }

    const double cosine = std::clamp(
        first.Dot(second) / (first.Magnitude() * second.Magnitude()),
        -1.0,
        1.0);
    return std::acos(cosine) * kRadiansToDegrees;
}

// 删除点序列中的重复点，并把删除数量累加到 duplicateCount。
void removeRepeatedPoints(std::vector<gp_Pnt>* points,
                           double tolerance,
                           int* duplicateCount)
{
    if (points == nullptr || points->empty()) {
        return;
    }

    std::vector<gp_Pnt> uniquePoints;
    uniquePoints.reserve(points->size());
    for (const gp_Pnt& point : *points) {
        bool duplicate = false;
        for (const gp_Pnt& existing : uniquePoints) {
            if (pointsCoincide(point, existing, tolerance)) {
                duplicate = true;
                break;
            }
        }
        if (duplicate) {
            if (duplicateCount != nullptr) {
                ++(*duplicateCount);
            }
        } else {
            uniquePoints.push_back(point);
        }
    }
    points->swap(uniquePoints);
}

// 清理已经有顺序的点列：过滤无效点、删除重复点，并判断首尾是否闭合。
std::vector<gp_Pnt> cleanOrderedPoints(const std::vector<gp_Pnt>& input,
                                       double tolerance,
                                       int* invalidCount,
                                       int* duplicateCount,
                                       bool* closed)
{
    // 先过滤非法坐标，避免 NaN/Inf 进入距离比较并破坏排序结果。
    std::vector<gp_Pnt> validPoints;
    validPoints.reserve(input.size());
    for (const gp_Pnt& point : input) {
        if (isFinitePoint(point)) {
            validPoints.push_back(point);
        } else if (invalidCount != nullptr) {
            ++(*invalidCount);
        }
    }

    bool isClosed = validPoints.size() >= 3
        && pointsCoincide(validPoints.front(), validPoints.back(), tolerance);
    if (isClosed) {
        validPoints.pop_back();
    }

    std::vector<gp_Pnt> cleaned;
    cleaned.reserve(validPoints.size());
    for (const gp_Pnt& point : validPoints) {
        if (!cleaned.empty()
            && pointsCoincide(point, cleaned.back(), tolerance)) {
            if (duplicateCount != nullptr) {
                ++(*duplicateCount);
            }
            continue;
        }
        cleaned.push_back(point);
    }

    removeRepeatedPoints(&cleaned, tolerance, duplicateCount);
    if (cleaned.size() >= 3
        && pointsCoincide(cleaned.front(), cleaned.back(), tolerance)) {
        cleaned.pop_back();
        isClosed = true;
        if (duplicateCount != nullptr) {
            ++(*duplicateCount);
        }
    }

    if (closed != nullptr) {
        *closed = isClosed;
    }
    return cleaned;
}

// ============================================================================
// B 样条拟合辅助函数
// ============================================================================

// 将清洗后的点列写入结果对象；点数不足的轮廓会被跳过。
void appendCleanedContour(SectionResult* result,
                          std::vector<gp_Pnt> points,
                          bool closed)
{
    if (result == nullptr) {
        return;
    }

    if (closed && points.size() >= 2
        && pointsCoincide(points.front(), points.back(),
                          result->tolerances.duplicatePoint)) {
        points.pop_back();
    }

    if (points.empty()) {
        return;
    }

    SectionContour contour;
    contour.cleanedPoints = std::move(points);
    contour.closed = closed;

    result->cleanedPoints.insert(
        result->cleanedPoints.end(),
        contour.cleanedPoints.begin(),
        contour.cleanedPoints.end());
    result->contours.push_back(std::move(contour));
}

// 清空所有派生数据，并提取参与本轮清洗的有限坐标点。
// rawSegments/rawPoints 属于求交阶段输入，必须完整保留以便重复调整参数。
std::vector<gp_Pnt> initializeCleaning(
    SectionResult* result,
    const SectionProcessingOptions& options)
{
    result->cleanedPoints.clear();
    result->contours.clear();
    result->beforeBumpContours.clear();
    result->targetSelection = TargetContourSelection{};
    result->hasTargetSelection = false;
    result->selectedContour = SectionContour{};
    result->hasSelectedContour = false;
    result->repairedBumpCount = 0;
    result->bumpSupportRejected = result->bumpShapeRejected = 0;
    result->bumpBudgetRejected = result->bumpSafetyRejected = 0;
    result->movedBumpPointCount = 0;
    result->maximumBumpMovement = 0.0;
    result->invalidPointCount = 0;
    result->duplicatePointCount = 0;
    result->skippedContourCount = 0;
    result->fittedContourCount = 0;
    result->error.clear();

    std::vector<gp_Pnt> validPoints;
    validPoints.reserve(result->rawPoints.size());
    for (const SectionPoint& point : result->rawPoints) {
        if (isFinitePoint(point.point)) {
            validPoints.push_back(point.point);
        } else {
            ++result->invalidPointCount;
        }
    }

    const SectionTolerances calculated =
        processingTolerances(validPoints, options);
    result->tolerances.duplicatePoint = calculated.duplicatePoint;
    result->tolerances.fitting = calculated.fitting;
    if (result->tolerances.plane <= 0.0) {
        result->tolerances.plane = calculated.plane;
    }
    if (result->tolerances.minimumSegment <= 0.0) {
        result->tolerances.minimumSegment = calculated.minimumSegment;
    }
    return validPoints;
}

std::vector<std::size_t> walkClosedContour(std::size_t start,
                                           std::size_t end,
                                           std::size_t count,
                                           int direction)
{
    std::vector<std::size_t> indices;
    if (count == 0U || start >= count || end >= count
        || (direction != 1 && direction != -1)) {
        return indices;
    }
    indices.reserve(count);
    std::size_t current = start;
    for (std::size_t step = 0; step < count; ++step) {
        indices.push_back(current);
        if (current == end) {
            return indices;
        }
        current = direction > 0
            ? (current + 1U) % count
            : (current + count - 1U) % count;
    }
    indices.clear();
    return indices;
}

// 把 B 样条曲线的次数、节点、节点重数、控制点和权重导出为 CSV。
void writeBSplineFunctions(QTextStream* stream,
                           const std::vector<SectionContour>& contours)
{
    if (stream == nullptr) return;
    *stream << "record,contour_id,segment_id,index,status,join_after,join_angle_degrees,compliant,degree,periodic,closed,rational,"
               "first_parameter,last_parameter,maximum_deviation,knot,multiplicity,x,y,z,weight,"
               "deviation_is_upper_bound,fitting_tolerance\n";
    const auto number = [](double v) { return QString::number(v, 'g', 17); };
    for (int contourIndex = 0; contourIndex < static_cast<int>(contours.size()); ++contourIndex) {
        const SectionContour& contour = contours[contourIndex];
        if (contour.curve.IsNull()) continue;
        const Geom_BSplineCurve& curve = *contour.curve;
        const auto row = [&](const QString& type) {
            QStringList fields;
            for (int i = 0; i < 23; ++i) fields.push_back(QString());
            fields[0] = type;
            fields[1] = QString::number(contourIndex);
            fields[2] = QStringLiteral("0");
            return fields;
        };
        QStringList metadata = row(QStringLiteral("curve"));
        metadata[4] = QStringLiteral("compliant_cubic");
        metadata[5] = QStringLiteral("none");
        metadata[7] = contour.compliant ? QStringLiteral("1") : QStringLiteral("0");
        metadata[8] = QString::number(curve.Degree());
        metadata[9] = curve.IsPeriodic() ? QStringLiteral("1") : QStringLiteral("0");
        metadata[10] = contour.closed ? QStringLiteral("1") : QStringLiteral("0");
        metadata[11] = curve.IsRational() ? QStringLiteral("1") : QStringLiteral("0");
        metadata[12] = number(curve.FirstParameter());
        metadata[13] = number(curve.LastParameter());
        metadata[14] = number(contour.maximumDeviation);
        metadata[21] = contour.deviationIsUpperBound ? QStringLiteral("1") : QStringLiteral("0");
        metadata[22] = number(contour.fittingTolerance);
        *stream << metadata.join(',') << '\n';
        for (int i = 1; i <= curve.NbKnots(); ++i) {
            QStringList fields = row(QStringLiteral("knot"));
            fields[3] = QString::number(i);
            fields[15] = number(curve.Knot(i));
            fields[16] = QString::number(curve.Multiplicity(i));
            *stream << fields.join(',') << '\n';
        }
        for (int i = 1; i <= curve.NbPoles(); ++i) {
            QStringList fields = row(QStringLiteral("pole"));
            fields[3] = QString::number(i);
            fields[17] = number(curve.Pole(i).X());
            fields[18] = number(curve.Pole(i).Y());
            fields[19] = number(curve.Pole(i).Z());
            fields[20] = number(curve.Weight(i));
            *stream << fields.join(',') << '\n';
        }
    }
}

// ============================================================================
// STL 平面求交辅助函数
// ============================================================================

// 计算点到平面的有符号距离：法向量方向为正，反方向为负。
double signedDistance(const gp_Pnt& point,
                      const gp_Pnt& planePoint,
                      const gp_Dir& planeNormal)
{
    // gp_Vec(planePoint, point) 构造从平面参考点指向待测点的向量。
    const gp_Vec offset(planePoint, point);
    return offset.X() * planeNormal.X()
        + offset.Y() * planeNormal.Y()
        + offset.Z() * planeNormal.Z();
}

// 根据线段两端到平面的有符号距离，线性插值得到线段与平面的交点。
gp_Pnt interpolate(const gp_Pnt& first,
                   const gp_Pnt& second,
                   double firstDistance,
                   double secondDistance)
{
    // 分母接近零时无法稳定计算交点，直接返回第一个端点。
    const double denominator = firstDistance - secondDistance;
    if (std::abs(denominator) <= kZeroLength) {
        return first;
    }

    double parameter = firstDistance / denominator;
    parameter = std::clamp(parameter, 0.0, 1.0);

    return gp_Pnt(
        first.X() + parameter * (second.X() - first.X()),
        first.Y() + parameter * (second.Y() - first.Y()),
        first.Z() + parameter * (second.Z() - first.Z()));
}

// 判断线段两端是否严格位于平面的两侧。
bool crossesPlane(double firstDistance,
                  double secondDistance,
                  double tolerance)
{
    return (firstDistance < -tolerance && secondDistance > tolerance)
        || (firstDistance > tolerance && secondDistance < -tolerance);
}

bool prepareOutputDirectory(const QString& path,
                            QDir* directory,
                            QString* errorMessage)
{
    if (directory == nullptr) {
        return false;
    }

    *directory = QDir(path);
    if (directory->exists()) {
        return true;
    }
    if (QDir().mkpath(path)) {
        return true;
    }

    if (errorMessage != nullptr) {
        *errorMessage = QStringLiteral("无法创建输出目录：%1").arg(path);
    }
    return false;
}

} // 命名空间

// ============================================================================
// ============================================================================

// 读取 STL 文件，保存三角网格、文件路径和模型包围盒。
bool Stlsection::loadStl(const QString& filePath, QString* errorMessage)
{
    if (errorMessage != nullptr)
    {
        errorMessage->clear();
    }

    m_mesh.Nullify();
    m_filePath.clear();
    // 清空上一次加载的模型状态，避免失败后继续使用旧数据。
    m_boundingBox = SectionBoundingBox{};

    const QFileInfo fileInfo(filePath);
    if (!fileInfo.exists() || !fileInfo.isFile() || !fileInfo.isReadable())
    {
        if (errorMessage != nullptr)
        {
            *errorMessage = QStringLiteral("无法读取 STL 文件：%1").arg(filePath);
        }
        return false;
    }

    try
    {
        // OCCT 接收 UTF-8 路径；QFile::encodeName 在 Windows 上使用本地编码。
        const QByteArray pathBytes = filePath.toUtf8();
        Handle(Poly_Triangulation) mesh =
            RWStl::ReadFile(pathBytes.constData());

        if (mesh.IsNull() || mesh->NbNodes() <= 0 || mesh->NbTriangles() <= 0)
        {
            if (errorMessage != nullptr) {
                *errorMessage = QStringLiteral("STL 文件没有有效的顶点或三角形：%1")
                                    .arg(filePath);
            }
            return false;
        }

        const SectionBoundingBox boundingBox = boundingBoxForMesh(mesh);
        if (!boundingBox.valid) {
            if (errorMessage != nullptr) {
                *errorMessage = QStringLiteral("STL 文件没有有效的顶点：%1")
                                    .arg(filePath);
            }
            return false;
        }

        m_mesh = mesh;
        m_filePath = filePath;
        m_boundingBox = boundingBox;
        return true;
    }
    catch (const Standard_Failure& failure)
    {
        if (errorMessage != nullptr)
        {
            *errorMessage = QStringLiteral("Open CASCADE 读取 STL 失败：%1")
                                .arg(QString::fromLatin1(failure.what()));
        }
        return false;
    }
}

// 从文本文件读取按 x、y、z 排列的点，兼容逗号、分号和空白分隔符。
// ============================================================================
// STL 模型信息和默认截面平面
// ============================================================================

// 判断当前是否已经成功加载了可用的三角网格。
bool Stlsection::isLoaded() const
{
    return !m_mesh.IsNull() && m_boundingBox.valid;
}

const QString& Stlsection::filePath() const
{
    return m_filePath;
}

const SectionBoundingBox& Stlsection::boundingBox() const
{
    return m_boundingBox;
}

Handle(Poly_Triangulation) Stlsection::mesh() const
{
    return m_mesh;
}


// 返回模型包围盒中心，作为默认截面平面上的参考点。
gp_Pnt Stlsection::defaultPlanePoint() const
{
    if (kUseCustomPlanePosition) {
        return gp_Pnt(kCustomPlanePointX,
                      kCustomPlanePointY,
                      kCustomPlanePointZ);
    }
    return m_boundingBox.center;
}

// 选择包围盒最长轴作为默认平面法向量，便于得到模型的代表性截面。
gp_Dir Stlsection::defaultPlaneNormal() const
{
    const double dx = m_boundingBox.maximum.X() - m_boundingBox.minimum.X();
    const double dy = m_boundingBox.maximum.Y() - m_boundingBox.minimum.Y();
    const double dz = m_boundingBox.maximum.Z() - m_boundingBox.minimum.Z();

    gp_Vec normal;
    if (dx >= dy && dx >= dz) {
        normal = gp_Vec(1.0, 0.0, 0.0);
    } else if (dy >= dx && dy >= dz) {
        normal = gp_Vec(0.0, 1.0, 0.0);
    } else {
        normal = gp_Vec(0.0, 0.0, 1.0);
    }

    const gp_Pnt rotationOrigin(0.0, 0.0, 0.0);
    normal.Rotate(
        gp_Ax1(rotationOrigin, gp_Dir(1.0, 0.0, 0.0)),
        kDefaultPlaneRotationXDegrees * kDegreesToRadians);
    normal.Rotate(
        gp_Ax1(rotationOrigin, gp_Dir(0.0, 1.0, 0.0)),
        kDefaultPlaneRotationYDegrees * kDegreesToRadians);
    normal.Rotate(
        gp_Ax1(rotationOrigin, gp_Dir(0.0, 0.0, 1.0)),
        kDefaultPlaneRotationZDegrees * kDegreesToRadians);
    return gp_Dir(normal);
}

// 通过 STL 节点的协方差主成分估算轮毂轴向；轴线位置固定在包围盒中心。
bool Stlsection::estimateHubAxis(gp_Pnt* axisPoint,
                                         gp_Dir* axisDirection,
                                         double* outerRadius,
                                         QString* errorMessage) const
{
    if (axisPoint == nullptr || axisDirection == nullptr || outerRadius == nullptr
        || m_mesh.IsNull() || m_mesh->NbNodes() < 3) {
        if (errorMessage != nullptr) {
            *errorMessage = QStringLiteral("模型点数不足，无法估算轮毂中心轴。");
        }
        return false;
    }

    const int count = m_mesh->NbNodes();
    double mean[3] = {0.0, 0.0, 0.0};
    for (int index = 1; index <= count; ++index) {
        const gp_Pnt point = m_mesh->Node(index);
        mean[0] += point.X();
        mean[1] += point.Y();
        mean[2] += point.Z();
    }
    for (double& value : mean) {
        value /= static_cast<double>(count);
    }

    double covariance[3][3] = {};
    for (int index = 1; index <= count; ++index) {
        const gp_Pnt point = m_mesh->Node(index);
        const double delta[3] = {
            point.X() - mean[0], point.Y() - mean[1], point.Z() - mean[2]
        };
        for (int row = 0; row < 3; ++row) {
            for (int column = 0; column < 3; ++column) {
                covariance[row][column] += delta[row] * delta[column];
            }
        }
    }

    double eigenvectors[3][3] = {
        {1.0, 0.0, 0.0}, {0.0, 1.0, 0.0}, {0.0, 0.0, 1.0}
    };
    for (int iteration = 0; iteration < 32; ++iteration) {
        int p = 0;
        int q = 1;
        double largest = std::abs(covariance[p][q]);
        for (int row = 0; row < 3; ++row) {
            for (int column = row + 1; column < 3; ++column) {
                if (std::abs(covariance[row][column]) > largest) {
                    p = row;
                    q = column;
                    largest = std::abs(covariance[row][column]);
                }
            }
        }
        if (largest <= std::numeric_limits<double>::epsilon()
                           * std::max(covariance[0][0] + covariance[1][1]
                                          + covariance[2][2], 1.0)) {
            break;
        }

        const double angle = 0.5 * std::atan2(
            2.0 * covariance[p][q],
            covariance[q][q] - covariance[p][p]);
        const double cosine = std::cos(angle);
        const double sine = std::sin(angle);
        const double app = covariance[p][p];
        const double aqq = covariance[q][q];
        const double apq = covariance[p][q];
        covariance[p][p] = cosine * cosine * app
            - 2.0 * sine * cosine * apq + sine * sine * aqq;
        covariance[q][q] = sine * sine * app
            + 2.0 * sine * cosine * apq + cosine * cosine * aqq;
        covariance[p][q] = covariance[q][p] = 0.0;
        for (int index = 0; index < 3; ++index) {
            if (index == p || index == q) {
                continue;
            }
            const double aip = covariance[index][p];
            const double aiq = covariance[index][q];
            covariance[index][p] = covariance[p][index] =
                cosine * aip - sine * aiq;
            covariance[index][q] = covariance[q][index] =
                sine * aip + cosine * aiq;
        }
        for (int row = 0; row < 3; ++row) {
            const double vip = eigenvectors[row][p];
            const double viq = eigenvectors[row][q];
            eigenvectors[row][p] = cosine * vip - sine * viq;
            eigenvectors[row][q] = sine * vip + cosine * viq;
        }
    }

    int smallestIndex = 0;
    int largestIndex = 0;
    for (int index = 1; index < 3; ++index) {
        if (covariance[index][index] < covariance[smallestIndex][smallestIndex]) {
            smallestIndex = index;
        }
        if (covariance[index][index] > covariance[largestIndex][largestIndex]) {
            largestIndex = index;
        }
    }
    const double smallestVariance = covariance[smallestIndex][smallestIndex];
    const double largestVariance = covariance[largestIndex][largestIndex];
    if (largestVariance <= 0.0) {
        if (errorMessage != nullptr) {
            *errorMessage = QStringLiteral(
                "模型没有有效的空间尺寸，无法估算轮毂中心轴。");
        }
        return false;
    }

    const gp_Vec axis(eigenvectors[0][smallestIndex],
                     eigenvectors[1][smallestIndex],
                     eigenvectors[2][smallestIndex]);
    const gp_Pnt center = m_boundingBox.center;
    double maximumRadius = 0.0;
    for (int index = 1; index <= count; ++index) {
        const gp_Vec offset(center, m_mesh->Node(index));
        const double axialDistance = offset.Dot(axis);
        const gp_Vec radial = offset - axis.Multiplied(axialDistance);
        maximumRadius = std::max(maximumRadius, radial.Magnitude());
    }

    *axisPoint = center;
    *axisDirection = gp_Dir(axis);
    *outerRadius = maximumRadius;
    if (errorMessage != nullptr) {
        if (maximumRadius <= 0.0) {
            *errorMessage = QStringLiteral("模型半径无效，无法估算轮毂中心轴。");
        } else if (smallestVariance / largestVariance > 0.2) {
            *errorMessage = QStringLiteral(
                "模型轴向特征不明显，当前中心轴按最短主轴估算；请检查视图中的青色轴线。");
        } else {
            errorMessage->clear();
        }
    }
    return maximumRadius > 0.0;
}

// 根据模型包围盒尺寸计算截面求交阶段使用的自适应容差。
SectionTolerances Stlsection::calculateTolerances() const
{
    const double diagonal = std::max(m_boundingBox.diagonal, 1.0);
    const double planeTolerance = std::max(1.0e-7, diagonal * 1.0e-7);
    const double minimumSegment = std::max(1.0e-6, diagonal * 1.0e-6);

    return {
        planeTolerance,
        minimumSegment,
        std::max(1.0e-9, diagonal * 1.0e-7),
        0.1
    };
}

// ============================================================================
// STL 三角网格截面
// ============================================================================

// 计算 STL 三角网格与半平面的截交线段。半平面以轴线为边界，
// 只保留从轴线沿 radialDirection 延伸的一侧。
SectionResult Stlsection::section(const gp_Pnt& axisPoint,
                                          const gp_Dir& axisDirection,
                                          const gp_Dir& radialDirection) const
{
    SectionResult result;
    result.tolerances = calculateTolerances();
    result.axisPoint = axisPoint;
    result.axisDirection = axisDirection;
    result.radialDirection = radialDirection;
    result.hasSectionFrame = true;

    if (!isLoaded()) {
        result.error = QStringLiteral("请先加载有效的 STL 文件。");
        return result;
    }

    if (!isFinitePoint(axisPoint)
        || !std::isfinite(axisDirection.X())
        || !std::isfinite(axisDirection.Y())
        || !std::isfinite(axisDirection.Z())
        || !std::isfinite(radialDirection.X())
        || !std::isfinite(radialDirection.Y())
        || !std::isfinite(radialDirection.Z())) {
        result.error = QStringLiteral("截面轴点或方向包含无效数值。");
        return result;
    }

    const gp_Vec axisVector(axisDirection);
    gp_Vec radialVector(radialDirection);
    radialVector = radialVector
        - axisVector.Multiplied(radialVector.Dot(axisVector));
    if (radialVector.Magnitude() <= 1.0e-12) {
        result.error = QStringLiteral("截面径向方向不能与主轴方向平行。");
        return result;
    }
    radialVector.Normalize();

    const gp_Dir effectiveRadialDirection(radialVector);
    const gp_Dir planeNormal(axisVector.Crossed(radialVector));
    const double planeTolerance = result.tolerances.plane;
    // 遍历三角形，收集每个三角形与平面的有效交线段。
    for (int triangleIndex = 1;
         triangleIndex <= m_mesh->NbTriangles();
         ++triangleIndex)
    {
        int firstIndex = 0;
        int secondIndex = 0;
        int thirdIndex = 0;
        m_mesh->Triangle(triangleIndex).Get(firstIndex, secondIndex, thirdIndex);

        if (firstIndex < 1 || firstIndex > m_mesh->NbNodes()
            || secondIndex < 1 || secondIndex > m_mesh->NbNodes()
            || thirdIndex < 1 || thirdIndex > m_mesh->NbNodes()) {
            ++result.invalidPointCount;
            continue;
        }

        const gp_Pnt points[] =
            {
            m_mesh->Node(firstIndex),
            m_mesh->Node(secondIndex),
            m_mesh->Node(thirdIndex)
        };

        // 计算三角形三个顶点到平面的有符号距离。
        const double distances[] =
            {
            signedDistance(points[0], axisPoint, planeNormal),
            signedDistance(points[1], axisPoint, planeNormal),
            signedDistance(points[2], axisPoint, planeNormal)
        };

        // 情况一：三角形整体落在平面容差带内，不生成退化交线。
        const bool allOnPlane =
            std::abs(distances[0]) <= planeTolerance
            && std::abs(distances[1]) <= planeTolerance
            && std::abs(distances[2]) <= planeTolerance;
        if (allOnPlane)
        {
            continue;
        }

        // 情况二：顶点落在平面容差带内，直接作为交点候选。
        std::vector<gp_Pnt> intersections;
        intersections.reserve(3);
        for (int vertex = 0; vertex < 3; ++vertex)
        {
            if (std::abs(distances[vertex]) <= planeTolerance) {
                intersections.push_back(points[vertex]);
            }
        }


        // 情况三：检查三条边是否跨越平面，并对跨越边进行插值。
        constexpr int edgeStart[] = {0, 1, 2};
        constexpr int edgeEnd[] = {1, 2, 0};
        for (int edge = 0; edge < 3; ++edge) {
            const int start = edgeStart[edge];
            const int end = edgeEnd[edge];
            const double startDistance = distances[start];
            const double endDistance = distances[end];

            // 只有边的两个端点严格分处平面两侧时才计算交点。
            if (crossesPlane(startDistance, endDistance, planeTolerance)) {
                intersections.push_back(interpolate(points[start], points[end],
                                                    startDistance, endDistance));
            }
        }

        removeRepeatedPoints(&intersections, planeTolerance, nullptr);
        if (intersections.size() >= 2
            && isFinitePoint(intersections[0])
            && isFinitePoint(intersections[1]))
        {
            gp_Pnt first = intersections[0];
            gp_Pnt second = intersections[1];
            const double firstRadialDistance = signedDistance(
                first, axisPoint, effectiveRadialDirection);
            const double secondRadialDistance = signedDistance(
                second, axisPoint, effectiveRadialDirection);

            // 两端都在轴线背面时，该交线段不属于目标半平面。
            if (firstRadialDistance < -planeTolerance
                && secondRadialDistance < -planeTolerance) {
                continue;
            }

            // 交线段跨过轴线时，将背面端点裁剪到轴线边界上。
            if (firstRadialDistance < -planeTolerance) {
                first = interpolate(first, second,
                                    firstRadialDistance,
                                    secondRadialDistance);
            } else if (secondRadialDistance < -planeTolerance) {
                second = interpolate(first, second,
                                     firstRadialDistance,
                                     secondRadialDistance);
            }

            if (first.Distance(second) <= result.tolerances.minimumSegment) {
                continue;
            }

            const int segmentIndex =
                static_cast<int>(result.rawSegments.size());
            result.rawSegments.push_back({
                first,
                second,
                triangleIndex
            });
            result.rawPoints.push_back({
                first,
                triangleIndex,
                segmentIndex,
                0
            });
            result.rawPoints.push_back({
                second,
                triangleIndex,
                segmentIndex,
                1
            });
        }
    }

    // 原始交线段已收集完成，清洗和拟合由后续独立阶段负责。
    result.success = true;
    return result;
}

// ============================================================================
// 截面点清洗和轮廓重建
// ============================================================================

// 清洗截面结果：过滤无效点、合并相近节点，并把线段重建为有序轮廓。
bool Stlsection::cleanSectionResult(
    SectionResult* result,
    const SectionProcessingOptions& options)
{
    if (result == nullptr) {
        return false;
    }
    if (!result->success) {
        return false;
    }

    if (options.enableMicroBumpCleaning
        && (!std::isfinite(options.bumpDetectionTolerance) || options.bumpDetectionTolerance <= 0.
            || !std::isfinite(options.maximumBumpWidth) || options.maximumBumpWidth <= 0.
            || !std::isfinite(options.maximumBumpDisplacement) || options.maximumBumpDisplacement <= 0.
            || !std::isfinite(options.bumpReferenceLength) || options.bumpReferenceLength <= 0.
            || !std::isfinite(options.bumpSupportSearchLength) || options.bumpSupportSearchLength < options.bumpReferenceLength
            || !std::isfinite(options.bumpSupportResidual) || options.bumpSupportResidual <= 0.
            || !std::isfinite(options.bumpTangentAngleDegrees) || options.bumpTangentAngleDegrees <= 0.
            || options.bumpTangentAngleDegrees >= 90.)) {
        result->error = QStringLiteral("微小突起清洗参数必须是有限正数。");
        return false;
    }
    // 1. 过滤无效原始点，并根据输入数据尺寸确定点处理容差。
    const std::vector<gp_Pnt> allPoints = initializeCleaning(result, options);

    if (result->rawSegments.empty()) {
        bool closed = false;
        std::vector<gp_Pnt> cleaned = cleanOrderedPoints(
            allPoints,
            result->tolerances.duplicatePoint,
            nullptr,
            &result->duplicatePointCount,
            &closed);
        repairMicroBumps(cleaned, closed, options, result);
        appendCleanedContour(result, std::move(cleaned), closed);
        return true;
    }

    // 2. 用小容差合并数值上重合的端点，并建立无向线段图。
    std::vector<PointNode> nodes;
    std::vector<GraphEdge> edges;
    std::unordered_map<GridKey, std::vector<int>, GridKeyHash> buckets;
    std::set<std::pair<int, int>> edgeKeys;
    const double pointTolerance = std::max(
        result->tolerances.duplicatePoint, kZeroLength);
    const double squaredPointTolerance = pointTolerance * pointTolerance;

    const auto findOrCreateNode = [&](const gp_Pnt& point) {
        const GridKey key = gridKeyFor(point, pointTolerance);
        for (long long dx = -1; dx <= 1; ++dx) {
            for (long long dy = -1; dy <= 1; ++dy) {
                for (long long dz = -1; dz <= 1; ++dz) {
                    const GridKey neighbourKey{
                        key.x + dx,
                        key.y + dy,
                        key.z + dz
                    };
                    const auto bucket = buckets.find(neighbourKey);
                    if (bucket == buckets.end()) {
                        continue;
                    }
                    for (const int nodeId : bucket->second) {
                        const gp_Pnt& existing =
                            nodes[static_cast<std::size_t>(nodeId)].point;
                        if (existing.SquareDistance(point)
                            <= squaredPointTolerance) {
                            ++result->duplicatePointCount;
                            return nodeId;
                        }
                    }
                }
            }
        }

        const int nodeId = static_cast<int>(nodes.size());
        nodes.push_back({point, {}});
        buckets[key].push_back(nodeId);
        return nodeId;
    };

    for (const SectionSegment& segment : result->rawSegments) {
        if (!isFinitePoint(segment.first) || !isFinitePoint(segment.second)) {
            continue;
        }
        if (segment.first.Distance(segment.second) <= pointTolerance) {
            ++result->duplicatePointCount;
            continue;
        }
        const int firstNode = findOrCreateNode(segment.first);
        const int secondNode = findOrCreateNode(segment.second);
        if (firstNode == secondNode) {
            ++result->duplicatePointCount;
            continue;
        }

        const std::pair<int, int> edgeKey(
            std::min(firstNode, secondNode),
            std::max(firstNode, secondNode));
        if (!edgeKeys.insert(edgeKey).second) {
            ++result->duplicatePointCount;
            continue;
        }

        const int edgeId = static_cast<int>(edges.size());
        edges.push_back({firstNode, secondNode});
        nodes[static_cast<std::size_t>(firstNode)].edgeIds.push_back(edgeId);
        nodes[static_cast<std::size_t>(secondNode)].edgeIds.push_back(edgeId);
    }

    if (edges.empty()) {
        result->error = QStringLiteral("清洗后没有可用的截面线段。");
        return false;
    }

    const auto otherNode = [&](int edgeId, int nodeId) {
        const GraphEdge& edge = edges[static_cast<std::size_t>(edgeId)];
        return edge.first == nodeId ? edge.second : edge.first;
    };

    const auto addGraphEdge = [&](int firstNode, int secondNode) {
        const std::pair<int, int> edgeKey(
            std::min(firstNode, secondNode),
            std::max(firstNode, secondNode));
        if (firstNode == secondNode || edgeKeys.count(edgeKey) != 0U) {
            return false;
        }

        const int edgeId = static_cast<int>(edges.size());
        edges.push_back({firstNode, secondNode});
        edgeKeys.insert(edgeKey);
        nodes[static_cast<std::size_t>(firstNode)].edgeIds.push_back(edgeId);
        nodes[static_cast<std::size_t>(secondNode)].edgeIds.push_back(edgeId);
        return true;
    };

    const auto buildComponentIds = [&]() {
        std::vector<int> componentIds(nodes.size(), -1);
        int componentCount = 0;
        for (int startNode = 0;
             startNode < static_cast<int>(nodes.size());
             ++startNode) {
            if (nodes[static_cast<std::size_t>(startNode)].edgeIds.empty()
                || componentIds[static_cast<std::size_t>(startNode)] >= 0) {
                continue;
            }

            std::vector<int> pending{startNode};
            componentIds[static_cast<std::size_t>(startNode)] = componentCount;
            while (!pending.empty()) {
                const int nodeId = pending.back();
                pending.pop_back();
                for (const int edgeId :
                     nodes[static_cast<std::size_t>(nodeId)].edgeIds) {
                    const int neighbour = otherNode(edgeId, nodeId);
                    if (componentIds[static_cast<std::size_t>(neighbour)] >= 0) {
                        continue;
                    }
                    componentIds[static_cast<std::size_t>(neighbour)] =
                        componentCount;
                    pending.push_back(neighbour);
                }
            }
            ++componentCount;
        }
        return std::pair<std::vector<int>, int>(
            std::move(componentIds), componentCount);
    };

    for (const PointNode& node : nodes) {
        if (node.edgeIds.size() > 2U) {
            result->error = QStringLiteral(
                "截面线段存在分支节点，无法重建为单一轮廓。");
            return false;
        }
    }

    std::vector<double> segmentLengths;
    segmentLengths.reserve(edges.size());
    for (const GraphEdge& edge : edges) {
        segmentLengths.push_back(
            nodes[static_cast<std::size_t>(edge.first)].point.Distance(
                nodes[static_cast<std::size_t>(edge.second)].point));
    }
    const auto middle = segmentLengths.begin()
        + static_cast<std::ptrdiff_t>(segmentLengths.size() / 2U);
    std::nth_element(segmentLengths.begin(), middle, segmentLengths.end());
    const double typicalSegmentLength = std::max(*middle, pointTolerance);
    const double endpointStitchTolerance = std::max(
        pointTolerance,
        typicalSegmentLength * kEndpointStitchLengthFraction);

    // 3. 只连接不同子轮廓的端点，避免较大的拼接容差吞掉真实短线段。
    while (true) {
        auto components = buildComponentIds();
        const std::vector<int>& componentIds = components.first;
        if (components.second == 1) {
            break;
        }

        int bestFirst = -1;
        int bestSecond = -1;
        double bestDistance = std::numeric_limits<double>::max();
        for (int firstNode = 0;
             firstNode < static_cast<int>(nodes.size());
             ++firstNode) {
            if (nodes[static_cast<std::size_t>(firstNode)].edgeIds.size()
                != 1U) {
                continue;
            }
            for (int secondNode = firstNode + 1;
                 secondNode < static_cast<int>(nodes.size());
                 ++secondNode) {
                if (nodes[static_cast<std::size_t>(secondNode)].edgeIds.size()
                        != 1U
                    || componentIds[static_cast<std::size_t>(firstNode)]
                        == componentIds[static_cast<std::size_t>(secondNode)]) {
                    continue;
                }

                const double distance =
                    nodes[static_cast<std::size_t>(firstNode)].point.Distance(
                        nodes[static_cast<std::size_t>(secondNode)].point);
                if (distance > endpointStitchTolerance
                    || distance >= bestDistance) {
                    continue;
                }

                const int firstNeighbour = otherNode(
                    nodes[static_cast<std::size_t>(firstNode)].edgeIds.front(),
                    firstNode);
                const int secondNeighbour = otherNode(
                    nodes[static_cast<std::size_t>(secondNode)].edgeIds.front(),
                    secondNode);
                const gp_Vec firstTangent(
                    nodes[static_cast<std::size_t>(firstNeighbour)].point,
                    nodes[static_cast<std::size_t>(firstNode)].point);
                const gp_Vec secondTangent(
                    nodes[static_cast<std::size_t>(secondNode)].point,
                    nodes[static_cast<std::size_t>(secondNeighbour)].point);
                if (angleDegrees(firstTangent, secondTangent)
                    > kMaximumEndpointStitchAngleDegrees) {
                    continue;
                }

                bestFirst = firstNode;
                bestSecond = secondNode;
                bestDistance = distance;
            }
        }

        if (bestFirst < 0) {
            break; // 保留无法安全拼接的独立轮廓。
        }
        if (!addGraphEdge(bestFirst, bestSecond)) {
            result->error = QStringLiteral("无法创建端点补缝线段。");
            return false;
        }
    }

    // 多个连通分量分别输出，开放分量从真实端点开始，不添加跨缺口边。
    const auto remainingComponents = buildComponentIds();
    if (remainingComponents.second > 1) {
        std::vector<int> starts(remainingComponents.second, -1);
        std::vector<int> endpointCounts(remainingComponents.second, 0);
        std::vector<std::size_t> pointCounts(remainingComponents.second, 0);
        for (int node = 0; node < static_cast<int>(nodes.size()); ++node) {
            const int component = remainingComponents.first[node];
            if (component < 0) continue;
            ++pointCounts[component];
            if (starts[component] < 0) starts[component] = node;
            if (nodes[node].edgeIds.size() == 1U) {
                if (endpointCounts[component] == 0) starts[component] = node;
                ++endpointCounts[component];
            }
        }
        std::vector<bool> used(edges.size(), false);
        std::vector<SectionContour> contours;
        for (int component = 0; component < remainingComponents.second; ++component) {
            const bool closed = endpointCounts[component] == 0;
            if (!closed && endpointCounts[component] != 2) {
                result->error = QStringLiteral("独立轮廓的端点数量不合法。");
                return false;
            }
            int current = starts[component];
            std::vector<gp_Pnt> points;
            for (std::size_t step = 0; step < pointCounts[component]; ++step) {
                points.push_back(nodes[current].point);
                int next = -1;
                for (const int edge : nodes[current].edgeIds) {
                    if (!used[edge]) { next = edge; break; }
                }
                if (next < 0) {
                    if (!closed && step + 1 == pointCounts[component]
                        && nodes[current].edgeIds.size() == 1U) break;
                    result->error = QStringLiteral("独立轮廓遍历提前终止。");
                    return false;
                }
                used[next] = true;
                current = otherNode(next, current);
            }
            if (points.size() != pointCounts[component]
                || (closed && current != starts[component])) {
                result->error = QStringLiteral("独立轮廓拓扑校验失败。");
                return false;
            }
            if (options.enableMicroBumpCleaning)
                repairMicroBumps(points, closed, options, result);
            SectionContour contour;
            contour.cleanedPoints = std::move(points);
            contour.closed = closed;
            contours.push_back(std::move(contour));
        }
        if (std::find(used.begin(), used.end(), false) != used.end()) {
            result->error = QStringLiteral("独立轮廓未能完整遍历所有线段。");
            return false;
        }
        // Reuse the single-component closure checks for each independent open chain.
        // Never join unrelated components just to obtain a closed profile.
        for (auto& contour : contours) {
            if (contour.closed) {
                appendCleanedContour(result, std::move(contour.cleanedPoints), true);
                continue;
            }
            SectionResult componentResult;
            componentResult.success = true;
            componentResult.tolerances = result->tolerances;
            for (std::size_t i = 1; i < contour.cleanedPoints.size(); ++i)
                componentResult.rawSegments.push_back({contour.cleanedPoints[i-1], contour.cleanedPoints[i], -1});
            auto componentOptions = options;
            componentOptions.enableMicroBumpCleaning = false; // already cleaned above
            componentOptions.duplicateTolerance = result->tolerances.duplicatePoint;
            if (!cleanSectionResult(&componentResult, componentOptions)) {
                result->error = componentResult.error;
                return false;
            }
            for (auto& repaired : componentResult.contours)
                appendCleanedContour(result, std::move(repaired.cleanedPoints), repaired.closed);
        }
        result->error.clear();
        return true;
    }

    // 4. 连通图应当是一条开链或已经闭合的单环。
    std::vector<int> openEndpoints;
    for (int nodeId = 0; nodeId < static_cast<int>(nodes.size()); ++nodeId) {
        const std::size_t degree =
            nodes[static_cast<std::size_t>(nodeId)].edgeIds.size();
        if (degree == 1U) {
            openEndpoints.push_back(nodeId);
        } else if (degree != 2U) {
            result->error = QStringLiteral(
                "拼接后的截面拓扑不是单一开链或闭环。");
            return false;
        }
    }

    if (!openEndpoints.empty() && openEndpoints.size() != 2U) {
        result->error = QStringLiteral(
            "拼接后的轮廓包含 %1 个端点，无法闭合。")
            .arg(openEndpoints.size());
        return false;
    }

    // 在引入人工闭合采样点前，对真实节点排序。
    if (options.enableMicroBumpCleaning) {
        std::vector<int> order;
        int current = openEndpoints.empty() ? 0 : openEndpoints.front();
        int previousEdge = -1;
        for (std::size_t step = 0; step < nodes.size(); ++step) {
            order.push_back(current);
            int nextEdge = -1;
            for (int edge : nodes[current].edgeIds) {
                if (edge != previousEdge) { nextEdge = edge; break; }
            }
            if (nextEdge < 0) break;
            current = otherNode(nextEdge, current);
            previousEdge = nextEdge;
        }
        std::vector<gp_Pnt> actual;
        for (int node : order) actual.push_back(nodes[node].point);
        repairMicroBumps(actual, openEndpoints.empty(), options, result);
        for (std::size_t i = 0; i < order.size(); ++i) nodes[order[i]].point = actual[i];
    }
    // 闭合不安全时，沿开链保留全部节点和两个真实端点。
    const auto appendOpenContour = [&]() {
        std::vector<bool> used(edges.size(), false);
        std::vector<gp_Pnt> points;
        int current = openEndpoints.front();
        points.push_back(nodes[static_cast<std::size_t>(current)].point);
        for (std::size_t step = 0; step < edges.size(); ++step) {
            int next = -1;
            for (const int edge : nodes[static_cast<std::size_t>(current)].edgeIds) {
                if (!used[static_cast<std::size_t>(edge)]) {
                    next = edge;
                    break;
                }
            }
            if (next < 0) {
                result->error = QStringLiteral("开放轮廓遍历提前终止。");
                return false;
            }
            used[static_cast<std::size_t>(next)] = true;
            current = otherNode(next, current);
            points.push_back(nodes[static_cast<std::size_t>(current)].point);
        }
        if (current != openEndpoints.back() || points.size() != nodes.size()
            || std::find(used.begin(), used.end(), false) != used.end()) {
            result->error = QStringLiteral("开放轮廓未能完整遍历所有线段。");
            return false;
        }
        appendCleanedContour(result, std::move(points), false);
        result->error.clear();
        return true;
    };

    // 5. 对唯一开链的首尾缺口按典型点距均匀采样，使闭合处不产生长边。
    if (openEndpoints.size() == 2U) {
        const int firstEndpoint = openEndpoints[0];
        const int secondEndpoint = openEndpoints[1];
        const int firstNeighbour = otherNode(
            nodes[static_cast<std::size_t>(firstEndpoint)].edgeIds.front(),
            firstEndpoint);
        const int secondNeighbour = otherNode(
            nodes[static_cast<std::size_t>(secondEndpoint)].edgeIds.front(),
            secondEndpoint);
        const gp_Pnt firstPoint =
            nodes[static_cast<std::size_t>(firstEndpoint)].point;
        const gp_Pnt secondPoint =
            nodes[static_cast<std::size_t>(secondEndpoint)].point;
        const gp_Vec closureDirection(firstPoint, secondPoint);
        const gp_Vec firstOutward(
            nodes[static_cast<std::size_t>(firstNeighbour)].point,
            firstPoint);
        const gp_Vec secondInward(
            secondPoint,
            nodes[static_cast<std::size_t>(secondNeighbour)].point);
        if (angleDegrees(firstOutward, closureDirection)
                > kMaximumClosureAngleDegrees
            || angleDegrees(closureDirection, secondInward)
                > kMaximumClosureAngleDegrees) {
            return appendOpenContour();
        }

        const double closureLength = firstPoint.Distance(secondPoint);
        const int closureSegmentCount = std::max(
            1,
            static_cast<int>(std::ceil(
                closureLength / typicalSegmentLength)));
        if (closureSegmentCount > kMaximumClosureSegmentCount) {
            return appendOpenContour();
        }

        int previousNode = firstEndpoint;
        for (int index = 1; index < closureSegmentCount; ++index) {
            const double parameter =
                static_cast<double>(index)
                / static_cast<double>(closureSegmentCount);
            const gp_Pnt interpolated(
                firstPoint.X()
                    + parameter * (secondPoint.X() - firstPoint.X()),
                firstPoint.Y()
                    + parameter * (secondPoint.Y() - firstPoint.Y()),
                firstPoint.Z()
                    + parameter * (secondPoint.Z() - firstPoint.Z()));
            const int interpolatedNode = static_cast<int>(nodes.size());
            nodes.push_back({interpolated, {}});
            if (!addGraphEdge(previousNode, interpolatedNode)) {
                result->error = QStringLiteral("无法创建闭合插值线段。");
                return false;
            }
            previousNode = interpolatedNode;
        }
        if (!addGraphEdge(previousNode, secondEndpoint)) {
            result->error = QStringLiteral("无法连接闭合轮廓的首尾端点。");
            return false;
        }
    }

    // 6. 闭环必须连通、无分支，并满足边数等于节点数。
    const auto finalComponents = buildComponentIds();
    if (finalComponents.second != 1 || edges.size() != nodes.size()) {
        result->error = QStringLiteral("闭合轮廓拓扑校验失败。");
        return false;
    }
    for (const PointNode& node : nodes) {
        if (node.edgeIds.size() != 2U) {
            result->error = QStringLiteral("闭合轮廓仍包含端点或分支点。");
            return false;
        }
    }

    // 7. 从任意节点沿唯一未访问边遍历，得到按连接顺序排列的闭合点列。
    std::vector<bool> edgeUsed(edges.size(), false);
    std::vector<gp_Pnt> orderedPoints;
    orderedPoints.reserve(nodes.size());
    const int startNode = 0;
    int currentNode = startNode;
    for (std::size_t step = 0; step < edges.size(); ++step) {
        orderedPoints.push_back(
            nodes[static_cast<std::size_t>(currentNode)].point);
        int nextEdge = -1;
        for (const int edgeId :
             nodes[static_cast<std::size_t>(currentNode)].edgeIds) {
            if (!edgeUsed[static_cast<std::size_t>(edgeId)]) {
                nextEdge = edgeId;
                break;
            }
        }
        if (nextEdge < 0) {
            result->error = QStringLiteral("闭合轮廓遍历提前终止。");
            return false;
        }
        edgeUsed[static_cast<std::size_t>(nextEdge)] = true;
        currentNode = otherNode(nextEdge, currentNode);
    }

    if (currentNode != startNode || orderedPoints.size() != nodes.size()
        || std::find(edgeUsed.begin(), edgeUsed.end(), false)
            != edgeUsed.end()) {
        result->error = QStringLiteral("闭合轮廓未能完整遍历所有线段。");
        return false;
    }

    appendCleanedContour(result, std::move(orderedPoints), true);
    return true;
}

bool Stlsection::selectTargetContour(
    const SectionResult& result,
    const TargetContourSelection& selection,
    SectionContour* selectedContour,
    QString* errorMessage)
{
    if (selectedContour == nullptr) {
        if (errorMessage != nullptr) {
            *errorMessage = QStringLiteral("目标轮廓输出参数为空。");
        }
        return false;
    }
    *selectedContour = SectionContour{};

    if (!result.hasSectionFrame) {
        if (errorMessage != nullptr) {
            *errorMessage = QStringLiteral("缺少截面局部坐标系，无法生成目标折线。");
        }
        return false;
    }

    if (!selection.valid) {
        if (errorMessage != nullptr) {
            *errorMessage = QStringLiteral("请先在三维视图中选择目标折线的两个点。");
        }
        return false;
    }

    if (selection.first.contourIndex < 0
        || selection.first.contourIndex
               != selection.second.contourIndex
        || selection.first.contourIndex
               >= static_cast<int>(result.contours.size())) {
        if (errorMessage != nullptr) {
            *errorMessage = QStringLiteral("两个目标点必须位于同一条截面轮廓上。");
        }
        return false;
    }

    const SectionContour& target = result.contours[
        static_cast<std::size_t>(selection.first.contourIndex)];
    if (target.cleanedPoints.size() < 2U) {
        if (errorMessage != nullptr) {
            *errorMessage = QStringLiteral("目标点必须位于清洗后的有效轮廓上。");
        }
        return false;
    }

    const std::size_t pointCount = target.cleanedPoints.size();
    if (selection.first.pointIndex < 0
        || selection.second.pointIndex < 0
        || selection.first.pointIndex >= static_cast<int>(pointCount)
        || selection.second.pointIndex >= static_cast<int>(pointCount)
        || selection.first.pointIndex == selection.second.pointIndex) {
        if (errorMessage != nullptr) {
            *errorMessage = QStringLiteral("两个目标点必须是同一轮廓上的不同点。");
        }
        return false;
    }

    const std::size_t firstIndex = static_cast<std::size_t>(
        selection.first.pointIndex);
    const std::size_t secondIndex = static_cast<std::size_t>(
        selection.second.pointIndex);
    std::vector<std::size_t> forward = walkClosedContour(
        firstIndex, secondIndex, pointCount, 1);
    const std::vector<std::size_t> reverse = walkClosedContour(
        firstIndex, secondIndex, pointCount, -1);
    if (!target.closed) {
        forward.clear();
        const int direction = firstIndex < secondIndex ? 1 : -1;
        for (int index = static_cast<int>(firstIndex);;
             index += direction) {
            forward.push_back(static_cast<std::size_t>(index));
            if (index == static_cast<int>(secondIndex)) break;
        }
    }
    if (forward.size() < 2U || reverse.size() < 2U) {
        if (errorMessage != nullptr) {
            *errorMessage = QStringLiteral("两个目标点之间没有有效的连续折线。");
        }
        return false;
    }

    const auto pathLength = [&target](const std::vector<std::size_t>& path) {
        double length = 0.0;
        for (std::size_t index = 1U; index < path.size(); ++index) {
            length += target.cleanedPoints[path[index - 1U]].Distance(
                target.cleanedPoints[path[index]]);
        }
        return length;
    };
    const bool forwardIsShorter = pathLength(forward) <= pathLength(reverse);
    const bool chooseForward = !target.closed || (selection.alternatePath
        ? !forwardIsShorter
        : forwardIsShorter);
    const std::vector<std::size_t>& chosenIndices =
        chooseForward ? forward : reverse;

    std::vector<gp_Pnt> selectedPoints;
    selectedPoints.reserve(chosenIndices.size());
    for (const std::size_t index : chosenIndices) {
        selectedPoints.push_back(target.cleanedPoints[index]);
    }
    if (selectedPoints.size() < 2U) {
        if (errorMessage != nullptr) {
            *errorMessage = QStringLiteral("筛选出的目标折线点数不足。");
        }
        return false;
    }

    // 目标折线统一按径向从内侧到外侧排列，保持原拟合和导出方向。
    const gp_Vec radialVector(result.radialDirection);
    const double firstRadial = gp_Vec(result.axisPoint,
                                      selectedPoints.front()).Dot(radialVector);
    const double lastRadial = gp_Vec(result.axisPoint,
                                     selectedPoints.back()).Dot(radialVector);
    const double pointTolerance = std::max(
        result.tolerances.duplicatePoint, kZeroLength);
    if (firstRadial > lastRadial + pointTolerance) {
        std::reverse(selectedPoints.begin(), selectedPoints.end());
    }

    selectedContour->cleanedPoints = std::move(selectedPoints);
    selectedContour->closed = false;
    return true;
}

// ============================================================================
// B 样条拟合和统一处理入口
// ============================================================================

// 对清洗后的用户目标折线拟合 B 样条，并记录成功拟合的轮廓数量。
bool Stlsection::fitSectionResult(
    SectionResult* result,
    const SectionProcessingOptions& options)
{
    if (result == nullptr || !result->success) {
        return false;
    }

    result->fittedContourCount = 0;
    result->selectedContour = SectionContour{};

    if (!result->hasTargetSelection) {
        result->hasSelectedContour = false;
        result->error = QStringLiteral("请先在三维视图中选择目标折线的两个点。");
        return false;
    }

    // 只把用户选择的开放目标折线送入拟合器。
    SectionContour selectedContour;
    QString selectionError;
    if (!selectTargetContour(*result,
                             result->targetSelection,
                             &selectedContour,
                             &selectionError)) {
        result->hasSelectedContour = false;
        result->error = selectionError;
        return false;
    }

    const double tolerance = options.fittingTolerance == 0.0
        ? 0.1 : options.fittingTolerance;
    result->selectedContour = selectedContour;
    result->selectedContour.fittedPoints = selectedContour.cleanedPoints;
    result->hasSelectedContour = true;
    if (options.maximumOrder != 4) {
        result->error = QStringLiteral("平滑拟合仅支持四阶（三次）B 样条，不再降阶回退。");
        return false;
    }
    if (!std::isfinite(tolerance) || tolerance <= 0.0) {
        result->error = QStringLiteral("拟合容差必须是有限的正数，单位为毫米。");
        return false;
    }
    result->tolerances.fitting = tolerance;
    SectionContour fitted;
    if (!fitSmoothCubicContour(selectedContour, tolerance,
            options.maximumFittingPoles, options.maximumFittingIterations,
            &fitted, &result->error,
            options.fittingDirectionWindow, options.fittingDirectionWeight)) {
        ++result->skippedContourCount;
        return false;
    }
    result->selectedContour = std::move(fitted);
    result->fittedContourCount = 1;

    result->error.clear();
    return true;
}

// 按“先清洗、后拟合”的顺序执行完整截面处理流程。
bool Stlsection::processSectionResult(
    SectionResult* result,
    const SectionProcessingOptions& options)
{
    return cleanSectionResult(result, options)
        && fitSectionResult(result, options);
}

// ============================================================================
// 结果导出
// ============================================================================

// 使用统一精度格式化浮点数，避免导出文本出现不必要的尾数。
QString Stlsection::formatDouble(double value)
{
    return QString::number(value, 'g', 15);
}

// 导出 STL 截面的原始点坐标，不附加索引、拓扑或统计信息。
bool Stlsection::saveSectionPoints(
    const SectionResult& result,
    const QString& outputDirectory,
    QString* errorMessage) const
{
    if (errorMessage != nullptr) {
        errorMessage->clear();
    }

    if (!isLoaded()) {
        if (errorMessage != nullptr) {
            *errorMessage = QStringLiteral("没有可写出的 STL 模型。");
        }
        return false;
    }
    if (!result.success || result.rawPoints.empty()) {
        if (errorMessage != nullptr) {
            *errorMessage = QStringLiteral("没有可保存的截取点。");
        }
        return false;
    }

    QDir directory;
    if (!prepareOutputDirectory(outputDirectory, &directory, errorMessage)) {
        return false;
    }

    const QString stlBaseName =
        QFileInfo(m_filePath).completeBaseName();
    const QString pointsPath = directory.filePath(
        stlBaseName + QStringLiteral("_section_points.csv"));
    QFile pointsFile(pointsPath);
    if (!pointsFile.open(QIODevice::WriteOnly | QIODevice::Text)) {
        if (errorMessage != nullptr) {
            *errorMessage = QStringLiteral("无法写入输出文件：%1").arg(outputDirectory);
        }
        return false;
    }

    QTextStream pointsStream(&pointsFile);
    pointsStream.setEncoding(QStringConverter::Utf8);
    for (const SectionPoint& pointRecord : result.rawPoints) {
        const gp_Pnt& point = pointRecord.point;
        pointsStream << formatDouble(point.X()) << ','
                     << formatDouble(point.Y()) << ','
                     << formatDouble(point.Z()) << '\n';
    }
    return true;
}

bool Stlsection::saveTargetContourPolyline(const SectionResult& result,
                                         const QString& filePath,
                                         QString* errorMessage)
{
    if (errorMessage != nullptr) errorMessage->clear();
    const auto fail = [&](const QString& message) {
        if (errorMessage != nullptr) *errorMessage = message;
        return false;
    };
    if (!result.hasTargetSelection)
        return fail(QStringLiteral("请先选择目标轮廓折线的两个点。"));

    // 从当前选择重新生成，确保切换侧路径后不会导出上一次拟合的折线。
    SectionContour target;
    if (!selectTargetContour(result, result.targetSelection, &target, errorMessage))
        return false;
    for (const gp_Pnt& point : target.cleanedPoints) {
        if (!isFinitePoint(point))
            return fail(QStringLiteral("目标轮廓包含无效坐标，无法导出。"));
    }
    QSaveFile file(filePath);
    if (!file.open(QIODevice::WriteOnly | QIODevice::Text))
        return fail(QStringLiteral("无法写入目标折线文件：%1（%2）")
                        .arg(filePath, file.errorString()));
    QTextStream stream(&file);
    stream.setEncoding(QStringConverter::Utf8);
    for (const gp_Pnt& point : target.cleanedPoints) {
        stream << formatDouble(point.X()) << ','
               << formatDouble(point.Y()) << ','
               << formatDouble(point.Z()) << '\n';
    }
    stream.flush();
    if (stream.status() != QTextStream::Ok || !file.commit())
        return fail(QStringLiteral("保存目标折线文件失败：%1（%2）")
                        .arg(filePath, file.errorString()));
    return true;
}

// 导出 STL 截面拟合得到的 B 样条函数参数。
bool Stlsection::saveFittedBSplines(
    const SectionResult& result,
    const QString& outputDirectory,
    QString* errorMessage) const
{
    if (errorMessage != nullptr) {
        errorMessage->clear();
    }
    if (!isLoaded()) {
        if (errorMessage != nullptr) {
            *errorMessage = QStringLiteral("没有可写出的 STL 模型。");
        }
        return false;
    }
    if (!result.success || result.fittedContourCount <= 0
        || !result.hasSelectedContour
        || (result.selectedContour.curve.IsNull()
            && result.selectedContour.fittedSegments.empty())) {
        if (errorMessage != nullptr) {
            *errorMessage = QStringLiteral("没有可保存的 B 样条拟合函数。");
        }
        return false;
    }

    const SectionContour& fitted = result.selectedContour;
    if (!fitted.compliant
        || !std::isfinite(fitted.maximumDeviation)
        || !std::isfinite(fitted.fittingTolerance) || fitted.fittingTolerance <= 0.0
        || fitted.maximumDeviation >= fitted.fittingTolerance
        || fitted.curve.IsNull() || fitted.curve->Degree() != 3
        || !fitted.curve->IsCN(2)) {
        if (errorMessage != nullptr)
            *errorMessage = QStringLiteral("拟合曲线未通过平滑性或最大最短距离误差验收，不能导出。");
        return false;
    }

    QDir directory;
    if (!prepareOutputDirectory(outputDirectory, &directory, errorMessage)) {
        return false;
    }

    const QString splineFunctionPath = directory.filePath(
        QFileInfo(m_filePath).completeBaseName()
        + QStringLiteral("_bspline_functions.csv"));
    QFile splineFunctionFile(splineFunctionPath);
    if (!splineFunctionFile.open(QIODevice::WriteOnly | QIODevice::Text)) {
        if (errorMessage != nullptr) {
            *errorMessage = QStringLiteral("无法写入输出文件：%1")
                                .arg(splineFunctionPath);
        }
        return false;
    }

    QTextStream splineFunctionStream(&splineFunctionFile);
    splineFunctionStream.setEncoding(QStringConverter::Utf8);
    writeBSplineFunctions(
        &splineFunctionStream,
        std::vector<SectionContour>{result.selectedContour});
    return true;
}
