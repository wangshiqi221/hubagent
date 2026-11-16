#include "SectionWorkflow.h"

#include <Standard_Failure.hxx>
#include <gp_Vec.hxx>
#include <cmath>
#include <exception>
#include <utility>

namespace {
bool finite(const gp_Pnt& p)
{
    return std::isfinite(p.X()) && std::isfinite(p.Y()) && std::isfinite(p.Z());
}
}

SectionResult SectionWorkflow::compute(const Stlsection& model, const SectionRequest& request)
{
    SectionResult failure;
    if (!model.isLoaded()) {
        failure.error = QStringLiteral("STL model is not loaded.");
        return failure;
    }
    if (!finite(request.axisPoint)
        || !std::isfinite(request.axisDirection.X()) || !std::isfinite(request.axisDirection.Y())
        || !std::isfinite(request.axisDirection.Z()) || !std::isfinite(request.radialDirection.X())
        || !std::isfinite(request.radialDirection.Y()) || !std::isfinite(request.radialDirection.Z())
        || std::abs(request.axisDirection.Dot(request.radialDirection)) > 1.e-8) {
        failure.error = QStringLiteral("Section frame must be finite with perpendicular axis and radial directions.");
        return failure;
    }
    try {
        return model.section(request.axisPoint, request.axisDirection, request.radialDirection);
    } catch (const Standard_Failure& e) {
        failure.error = QStringLiteral("OCCT section failure: %1").arg(QString::fromLatin1(e.what()));
    } catch (const std::exception& e) {
        failure.error = QStringLiteral("Section failure: %1").arg(QString::fromLocal8Bit(e.what()));
    }
    return failure;
}

bool SectionWorkflow::prepareClosedContour(const SectionResult& result, int contourIndex,
                                           SectionContour* output, QString* error)
{
    const auto reject = [error](const QString& message) {
        if (error) *error = message;
        return false;
    };
    if (!output || !result.success || contourIndex < 0
        || static_cast<std::size_t>(contourIndex) >= result.contours.size()) {
        return reject(QStringLiteral("A successful cleaned section and valid contour index are required."));
    }
    const auto& contour = result.contours[contourIndex];
    if (!contour.closed || contour.cleanedPoints.size() < 3) {
        return reject(QStringLiteral("The contour must already be closed with at least three points."));
    }
    for (const auto& point : contour.cleanedPoints) {
        if (!finite(point)) return reject(QStringLiteral("Contour contains non-finite coordinates."));
    }
    // 保留尖角和点序，清除可能过期的拟合结果与合格状态。
    SectionContour prepared;
    prepared.cleanedPoints = contour.cleanedPoints;
    prepared.closed = true;
    *output = std::move(prepared);
    if (error) error->clear();
    return true;
}
