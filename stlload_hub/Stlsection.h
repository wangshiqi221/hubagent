#pragma once

#include "SectionTypes.h"

#include <QString>

#include <Poly_Triangulation.hxx>

#include <vector>

class Stlsection
{
public:
    // 读取 STL 并缓存三角网格和包围盒。失败时对象恢复为空状态。
    bool loadStl(const QString& filePath, QString* errorMessage = nullptr);

    // 当前 STL 模型的基本信息。
    bool isLoaded() const;
    const QString& filePath() const;
    const SectionBoundingBox& boundingBox() const;
    Handle(Poly_Triangulation) mesh() const;

    // 默认平面用于初始预览；轮毂轴则通过网格点云的主成分估算。
    gp_Pnt defaultPlanePoint() const;
    gp_Dir defaultPlaneNormal() const;
    bool estimateHubAxis(gp_Pnt* axisPoint,
                         gp_Dir* axisDirection,
                         double* outerRadius,
                         QString* errorMessage = nullptr) const;

    // 计算三角网格与半平面的交线段。本阶段只求交，不清洗或拟合。
    // axisDirection 和 radialDirection 应互相垂直，二者共同定义截面局部坐标系。
    SectionResult section(const gp_Pnt& axisPoint,
                          const gp_Dir& axisDirection,
                          const gp_Dir& radialDirection) const;

    // 合并重复端点，将无序交线段重建为有序闭合轮廓。
    static bool cleanSectionResult(
        SectionResult* result,
        const SectionProcessingOptions& options = SectionProcessingOptions{});

    // 根据用户选择的两个点，从闭合轮廓中构造目标开放折线。
    static bool selectTargetContour(const SectionResult& result,
                                    const TargetContourSelection& selection,
                                    SectionContour* selectedContour,
                                    QString* errorMessage = nullptr);

    // 目标开放折线采用自适应三次 C2 拟合；最大最短距离误差未达标则返回 false，
    // 仅保留目标折线，不能作为成功拟合结果导出。默认绝对容差为 0.1 mm。
    static bool fitSectionResult(SectionResult* result,
                                 const SectionProcessingOptions& options =
                                     SectionProcessingOptions{});

    // 一次性执行清洗和拟合，供不需要分步控制的调用方使用。
    static bool processSectionResult(
        SectionResult* result,
        const SectionProcessingOptions& options = SectionProcessingOptions{});

    // 将原始截面点写为 CSV，每行依次为 x,y,z。
    bool saveSectionPoints(const SectionResult& result,
                           const QString& outputDirectory,
                           QString* errorMessage = nullptr) const;

    // 按当前两点及侧路径选择导出目标折线，CSV 每行 x,y,z，顺序即连接顺序。
    // 无需先拟合；filePath 是用户指定的完整输出文件路径。
    static bool saveTargetContourPolyline(const SectionResult& result,
                                         const QString& filePath,
                                         QString* errorMessage = nullptr);

    // 将目标曲线的次数、节点、重数、控制点和权重写为 CSV。
    bool saveFittedBSplines(const SectionResult& result,
                            const QString& outputDirectory,
                            QString* errorMessage = nullptr) const;

private:
    SectionTolerances calculateTolerances() const;

    static QString formatDouble(double value);

    Handle(Poly_Triangulation) m_mesh;
    QString m_filePath;
    SectionBoundingBox m_boundingBox;
};
