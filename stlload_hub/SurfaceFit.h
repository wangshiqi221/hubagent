#pragma once
#include <Geom_BSplineSurface.hxx>
#include <gp_Pnt.hxx>
#include <QString>
#include <vector>
struct SurfaceFitResult {
    Handle(Geom_BSplineSurface) surface;
    double maximumDeviation = 0;
    bool planar = false;
    int iterations = 0;
    int constraintCount = 0;
    QString method;
};
// Validates all supplied samples against the bounded surface, in millimetres.
// Failure never returns a usable surface. Samples should include triangle interiors.
bool fitSurfacePatch(const std::vector<gp_Pnt>& samples, double tolerance,
                     SurfaceFitResult* result, QString* error, int maxIterations = 16);

// Extend a copy to section-plane scale: physical spans, not UV parameter values.
// Original fitting data remains unchanged. Exterior extrapolation is not fit data.
bool extendSurfacePatch(const Handle(Geom_BSplineSurface)& source,
                        double targetULength, double targetVLength,
                        Handle(Geom_BSplineSurface)* extended, QString* error);
