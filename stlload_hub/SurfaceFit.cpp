#include "SurfaceFit.h"
#include <GeomPlate_BuildPlateSurface.hxx>
#include <GeomLib.hxx>
#include <GeomPlate_BuildAveragePlane.hxx>
#include <Geom_Plane.hxx>
#include <Geom_RectangularTrimmedSurface.hxx>
#include <GeomConvert.hxx>
#include <Geom_BoundedSurface.hxx>
#include <Geom_BezierSurface.hxx>
#include <TColgp_Array2OfPnt.hxx>
#include <math_Gauss.hxx>
#include <math_Matrix.hxx>
#include <math_Vector.hxx>
#include <GeomPlate_PointConstraint.hxx>
#include <GeomPlate_Surface.hxx>
#include <GeomPlate_MakeApprox.hxx>
#include <GeomAPI_ProjectPointOnSurf.hxx>
#include <Standard_Failure.hxx>
#include <algorithm>
#include <cmath>
#include <set>
#include <tuple>
#include <limits>
#include <array>

namespace {
double bernstein(int degree,int i,double t) {
    double choose=1;for(int k=1;k<=i;++k)choose=choose*(degree-k+1)/k;
    return choose*std::pow(t,i)*std::pow(1-t,degree-i);
}
// A single smooth height patch has controlled extrapolation, unlike an
// underconstrained collection of plate approximation patches. It is still fit
// only to the actual selected 3D points and checked against every one of them.
bool fitHeightPatch(const std::vector<gp_Pnt>& points,const Handle(Geom_Plane)& plane,
                    double tolerance,SurfaceFitResult* result) {
    const auto frame=plane->Position();const gp_Vec u(frame.XDirection()),v(frame.YDirection()),normal(frame.Direction());
    std::vector<std::array<double,3>> coordinates;
    double u0=1e100,u1=-u0,v0=u0,v1=-u0;
    for(const auto& point:points) {
        const gp_Vec delta(frame.Location(),point);const double a=delta.Dot(u),b=delta.Dot(v);
        coordinates.push_back({a,b,delta.Dot(normal)});u0=std::min(u0,a);u1=std::max(u1,a);v0=std::min(v0,b);v1=std::max(v1,b);
    }
    if(u1-u0<1e-8 || v1-v0<1e-8)return false;
    Handle(Geom_BSplineSurface) best;double bestError=tolerance;
    for(int degree:{2,3,4}) {
        const int side=degree+1,count=side*side;if(points.size()<size_t(count))continue;
        math_Matrix matrix(1,count,1,count,0.);math_Vector rhs(1,count,0.),solution(1,count,0.);
        std::vector<double> basis(count);
        for(const auto& point:coordinates) {
            const double a=(point[0]-u0)/(u1-u0),b=(point[1]-v0)/(v1-v0);
            for(int i=0;i<side;++i)for(int j=0;j<side;++j)basis[i*side+j]=bernstein(degree,i,a)*bernstein(degree,j,b);
            for(int i=0;i<count;++i) {
                rhs(i+1)+=basis[i]*point[2];
                for(int j=0;j<count;++j)matrix(i+1,j+1)+=basis[i]*basis[j];
            }
        }
        for(int i=1;i<=count;++i)matrix(i,i)+=points.size()*1e-8;
        math_Gauss solve(matrix);if(!solve.IsDone())continue;solve.Solve(rhs,solution);
        TColgp_Array2OfPnt poles(1,side,1,side);
        for(int i=0;i<side;++i)for(int j=0;j<side;++j) {
            const gp_Vec delta=u.Multiplied(u0+(u1-u0)*i/degree)+v.Multiplied(v0+(v1-v0)*j/degree)
                +normal.Multiplied(solution(i*side+j+1));
            poles.SetValue(i+1,j+1,frame.Location().Translated(delta));
        }
        auto surface=GeomConvert::SurfaceToBSplineSurface(new Geom_BezierSurface(poles));
        double maximum=0;bool valid=true;
        for(const auto& point:points) {
            GeomAPI_ProjectPointOnSurf projection(point,surface);
            if(!projection.NbPoints()) {valid=false;break;}
            maximum=std::max(maximum,projection.LowerDistance());if(maximum>tolerance) {valid=false;break;}
        }
        if(valid && maximum<=bestError) {
            best=surface;bestError=maximum;
            if(maximum<=tolerance*.1)break;
        }
    }
    if(best.IsNull())return false;
    result->surface=best;result->maximumDeviation=bestError;result->constraintCount=int(points.size());
    result->method="regularized_single_bezier_patch";return true;
}
}

bool fitSurfacePatch(const std::vector<gp_Pnt>& input, double tolerance,
                     SurfaceFitResult* result, QString* error, int maxIterations)
{
    if (!result) return false;
    *result = {};
    auto fail = [&](const QString& message) { if (error) *error = message; return false; };
    if(maxIterations<1 || maxIterations>64) return fail(QStringLiteral("拟合迭代预算必须在1到64之间。"));
    if (!std::isfinite(tolerance) || tolerance <= 0)
        return fail(QStringLiteral("曲面容差必须为正数。"));
    std::vector<gp_Pnt> points;
    std::set<std::tuple<double,double,double>> unique;
    for (const auto& point : input) {
        if (!std::isfinite(point.X()) || !std::isfinite(point.Y()) || !std::isfinite(point.Z()))
            return fail(QStringLiteral("选区包含无效坐标。"));
        if (unique.emplace(point.X(),point.Y(),point.Z()).second) points.push_back(point);
    }
    if (points.size() < 9) return fail(QStringLiteral("选区至少需要 9 个不同采样点。"));
    // Each selected patch must be disk-like, not an entire closed window wall.
    std::set<size_t> constraints;
    const size_t initial = std::min<size_t>(48, points.size());
    for (size_t i=0; i<initial; ++i) constraints.insert(i*(points.size()-1)/(initial-1));
    try {
        Handle(NCollection_HArray1<gp_Pnt>) planePoints=new NCollection_HArray1<gp_Pnt>(1,int(points.size()));
        for(size_t i=0;i<points.size();++i) planePoints->SetValue(int(i)+1,points[i]);
        GeomPlate_BuildAveragePlane average(planePoints,0,1e-8,2,1);
        if(average.IsLine()) return fail(QStringLiteral("选区近似为一条直线，请选择有面积的区域。"));
        if(average.IsPlane()) {
            const auto plane=average.Plane();
            double maximum=0;
            for(const auto& point:points) maximum=std::max(maximum,plane->Pln().Distance(point));
            // A plane barely inside the tolerance can erase real wall curvature
            // and displace its later intersections. Prefer an accurate smooth
            // patch before accepting that marginal planar approximation.
            if(maximum>std::min(.05,tolerance*.1) && fitHeightPatch(points,plane,tolerance,result)) {
                if(error)error->clear();return true;
            }
            if(maximum<=tolerance) {
                double u0,u1,v0,v1; average.MinMaxBox(u0,u1,v0,v1);
                const double margin=std::max(1e-5,std::max(u1-u0,v1-v0)*0.02);
                Handle(Geom_RectangularTrimmedSurface) trimmed=new Geom_RectangularTrimmedSurface(plane,u0-margin,u1+margin,v0-margin,v1+margin);
                result->surface=GeomConvert::SurfaceToBSplineSurface(trimmed);
                result->maximumDeviation=maximum; result->planar=true;
                result->method="plane";
                if(error) error->clear();return true;
            }
        }
        for (int iteration=0; iteration<maxIterations; ++iteration) {
            result->iterations=iteration+1;
            result->constraintCount=int(constraints.size());
            GeomPlate_BuildPlateSurface plate(3, 10, 3, 1e-5, tolerance*0.1);
            for (auto index : constraints)
                plate.Add(new GeomPlate_PointConstraint(points[index], 0, tolerance*0.1));
            plate.Perform();
            if (!plate.IsDone()) return fail(QStringLiteral("曲面求解失败，请缩小选区或排除圆角、折叠区域。"));
            GeomPlate_MakeApprox approx(plate.Surface(), tolerance*0.1, 64, 5, tolerance*0.1, 0);
            auto surface = approx.Surface();
            if (surface.IsNull()) return fail(QStringLiteral("无法转换为 B 样条曲面。"));
            std::vector<std::pair<double,size_t>> errors;
            double maximum=0;
            for (size_t i=0; i<points.size(); ++i) {
                GeomAPI_ProjectPointOnSurf projection(points[i], surface);
                if (projection.NbPoints()==0) return fail(QStringLiteral("采样点到曲面的投影失败。"));
                const double distance=projection.LowerDistance();
                if (!std::isfinite(distance)) return fail(QStringLiteral("曲面误差计算失败。"));
                maximum=std::max(maximum,distance);
                if (distance>tolerance && !constraints.count(i)) errors.emplace_back(distance,i);
            }
            result->maximumDeviation=maximum;
            if (maximum<=tolerance) {
                result->surface=surface; result->maximumDeviation=maximum;
                result->method="geom_plate_bspline";
                if (error) error->clear();
                return true;
            }
            if (errors.empty()) return fail(QStringLiteral("曲面超差：%1 mm，目标 %2 mm。请调整选区。").arg(maximum).arg(tolerance));
            std::sort(errors.rbegin(),errors.rend());
            for (size_t i=0;i<std::min<size_t>(32,errors.size());++i) constraints.insert(errors[i].second);
        }
        return fail(QStringLiteral("达到曲面拟合迭代上限 %1 次，最大采样偏差 %2 mm，目标 %3 mm；约束点 %4。请细化选区或增加迭代预算，不自动放宽容差。")
            .arg(maxIterations).arg(result->maximumDeviation,0,'g',8).arg(tolerance,0,'g',8).arg(result->constraintCount));
    } catch (const Standard_Failure& failure) {
        return fail(QStringLiteral("曲面拟合异常：%1").arg(QString::fromUtf8(failure.GetMessageString())));
    }
}


namespace {
// Approximate physical iso lengths at three interior positions. Parameter ranges
// are dimensionless for BSplines and cannot be replaced by millimetre lengths.
double minimumSurfaceSpan(const Handle(Geom_BoundedSurface)& surface, bool inU)
{
    double u0,u1,v0,v1; surface->Bounds(u0,u1,v0,v1);
    double minimum=std::numeric_limits<double>::max();
    for(double fraction:{0.25,0.5,0.75}) {
        double length=0;
        gp_Pnt previous=surface->Value(inU?u0:u0+(u1-u0)*fraction,
                                       inU?v0+(v1-v0)*fraction:v0);
        for(int i=1;i<=32;++i) {
            const double t=i/32.0;
            gp_Pnt point=surface->Value(inU?u0+(u1-u0)*t:u0+(u1-u0)*fraction,
                                        inU?v0+(v1-v0)*fraction:v0+(v1-v0)*t);
            length+=point.Distance(previous); previous=point;
        }
        if(!std::isfinite(length)) return 0;
        minimum=std::min(minimum,length);
    }
    return minimum;
}
}

bool extendSurfacePatch(const Handle(Geom_BSplineSurface)& source,
                        double targetULength, double targetVLength,
                        Handle(Geom_BSplineSurface)* extended, QString* error)
{
    if(!extended) return false;
    extended->Nullify();
    auto fail=[&](const QString& message) {if(error) *error=message;return false;};
    if(source.IsNull() || !std::isfinite(targetULength) || !std::isfinite(targetVLength)
       || targetULength<=0 || targetVLength<=0)
        return fail(QStringLiteral("曲面延伸尺寸无效。"));
    try {
        Handle(Geom_BoundedSurface) surface=Handle(Geom_BoundedSurface)::DownCast(source->Copy());
        for(bool inU:{true,false}) {
            const double target=inU?targetULength:targetVLength;
            bool complete=false;
            for(int step=0;step<100;++step) {
                const double span=minimumSurfaceSpan(surface,inU);
                if(span<=1e-9) return fail(QStringLiteral("曲面存在退化方向，无法延伸。"));
                if(span>=target*(1-1e-5)) {complete=true;break;}
                // Several modest tangent extensions are more stable than one
                // large curvature extrapolation beyond a tiny selected patch.
                const double length=std::min((target-span)*0.5,span*0.2);
                GeomLib::ExtendSurfByLength(surface,length,1,inU,false);
                GeomLib::ExtendSurfByLength(surface,length,1,inU,true);
            }
            if(!complete) return fail(QStringLiteral("曲面未能延伸到截取半平面的尺度，请调整选区。"));
        }
        *extended=Handle(Geom_BSplineSurface)::DownCast(surface);
        if(extended->IsNull()) return fail(QStringLiteral("延伸后不是有效 B 样条曲面。"));
        if(error) error->clear();
        return true;
    } catch(const Standard_Failure& failure) {
        return fail(QStringLiteral("曲面延伸失败：%1").arg(QString::fromUtf8(failure.GetMessageString())));
    }
}
