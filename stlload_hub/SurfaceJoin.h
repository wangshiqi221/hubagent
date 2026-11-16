#pragma once
#include <TopoDS_Face.hxx>
#include <TopoDS_Solid.hxx>
#include <TopoDS_Shape.hxx>
#include <gp_Pnt.hxx>
#include <gp_Dir.hxx>
#include <QString>
#include <array>
#include <vector>
struct WindowSolidOptions {
    gp_Pnt axisOrigin;
    gp_Dir axisDirection=gp_Dir(0,0,1);
    double minimumDepth=0, maximumDepth=1;
    double fitTolerance=0.1;
    double filletRadius=0; // zero explicitly disables rounding
};
struct WindowSolidResult {
    TopoDS_Solid solid;
    TopoDS_Shape intersections;
    double extensionLength=0;
    bool filletApplied=false;
    QString warning;
    QString diagnostics;
};
// All four side supports and both axial cap planes must contribute to the solid.
// Never exports a shell as a cutter. A failed fillet preserves a valid sharp solid.
bool buildWindowCuttingSolid(const std::array<TopoDS_Face,4>& faces,
    const std::array<std::vector<gp_Pnt>,4>& samples, const WindowSolidOptions& options,
    WindowSolidResult* result, QString* error);
bool isValidCuttingSolid(const TopoDS_Shape& shape, QString* error=nullptr);
