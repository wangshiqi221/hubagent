#include "SurfaceFit.h"
#include <iostream>
#include <GeomAPI_ProjectPointOnSurf.hxx>
#include <limits>
int main()
{
    SurfaceFitResult result; QString error;
    if (fitSurfacePatch({},0.1,&result,&error) || error.isEmpty()) return 1;
    std::vector<gp_Pnt> samples;
    for (int i=0;i<9;++i) for (int j=0;j<9;++j)
        samples.emplace_back(i,j,0.015*i*i+0.01*i*j);
    if (fitSurfacePatch(samples,0,&result,&error)) return 2;
    if (!fitSurfacePatch(samples,0.1,&result,&error)) {
        std::cerr << error.toStdString(); return 3;
    }
    if (result.surface.IsNull() || result.maximumDeviation>0.1) return 4;
    const auto original=result.surface;
    double oldU0,oldU1,oldV0,oldV1;
    original->Bounds(oldU0,oldU1,oldV0,oldV1);
    Handle(Geom_BSplineSurface) extended;
    if(!extendSurfacePatch(original,120,75,&extended,&error)) {
        std::cerr << error.toStdString(); return 7;
    }
    double u0,u1,v0,v1;extended->Bounds(u0,u1,v0,v1);
    for(bool inU:{true,false}) {
        const double fixed=inU?(v0+v1)*0.5:(u0+u1)*0.5;
        double length=0;
        gp_Pnt previous=extended->Value(inU?u0:fixed,inU?fixed:v0);
        for(int i=1;i<=64;++i) {
            gp_Pnt point=extended->Value(inU?u0+(u1-u0)*i/64.0:fixed,inU?fixed:v0+(v1-v0)*i/64.0);
            length+=point.Distance(previous);previous=point;
        }
        if(length<(inU?120:75)*0.999) return 8;
    }
    for(const auto& point:samples) {
        GeomAPI_ProjectPointOnSurf projection(point,extended);
        if(!projection.NbPoints() || projection.LowerDistance()>0.1) return 9;
    }
    double checkU0,checkU1,checkV0,checkV1;
    original->Bounds(checkU0,checkU1,checkV0,checkV1);
    if(oldU0!=checkU0 || oldU1!=checkU1 || oldV0!=checkV0 || oldV1!=checkV1) return 10;
    std::vector<gp_Pnt> planarPoints;
    for(int i=0;i<5;++i) for(int j=0;j<5;++j) planarPoints.emplace_back(i,j,2*i+0.3*j+5);
    if(!fitSurfacePatch(planarPoints,0.1,&result,&error) || !result.planar || result.maximumDeviation>1e-7)return 11;
    std::vector<gp_Pnt> duplicates(20,gp_Pnt(1,1,1));
    if (fitSurfacePatch(duplicates,0.1,&result,&error) || !result.surface.IsNull()) return 5;
    samples[0]=gp_Pnt(std::numeric_limits<double>::quiet_NaN(),0,0);
    if (fitSurfacePatch(samples,0.1,&result,&error)) return 6;
    std::cout << "Surface fitting, tolerance and invalid input checks passed.\n";
    return 0;
}
