#pragma once
#include "SectionTypes.h"
#include <gp_Vec.hxx>
#include <algorithm>
#include <cmath>

namespace micro_bump_detail {
inline bool fitQuadratic(const std::vector<double>& x, const std::vector<double>& y,
                         const std::vector<double>& weights, double coefficients[3])
{
    double matrix[3][4]{};
    for (std::size_t k=0; k<x.size(); ++k) {
        double basis[3]{1., x[k], x[k]*x[k]};
        for (int i=0; i<3; ++i) {
            for (int j=0; j<3; ++j) matrix[i][j] += weights[k]*basis[i]*basis[j];
            matrix[i][3] += weights[k]*basis[i]*y[k];
        }
    }
    for (int col=0; col<3; ++col) {
        int pivot=col;
        for (int row=col+1; row<3; ++row)
            if (std::abs(matrix[row][col])>std::abs(matrix[pivot][col])) pivot=row;
        if (std::abs(matrix[pivot][col])<1.e-12) return false;
        for (int j=col; j<4; ++j) std::swap(matrix[col][j],matrix[pivot][j]);
        const double divisor=matrix[col][col];
        for (int j=col; j<4; ++j) matrix[col][j]/=divisor;
        for (int row=0; row<3; ++row) if (row!=col) {
            const double factor=matrix[row][col];
            for (int j=col; j<4; ++j) matrix[row][j]-=factor*matrix[col][j];
        }
    }
    for (int i=0; i<3; ++i) coefficients[i]=matrix[i][3];
    return true;
}

// 仅进行稳健的支撑预测：被排除的支撑采样点始终保留。
// 同一组支撑点可用直线解释时优先采用直线；两侧都必须提供依据。
inline bool fitStableSupports(const std::vector<double>& x, const std::vector<double>& y,
                              const std::vector<double>& weights, int split,
                              double tolerance, double coefficients[3], std::vector<bool>& inliers)
{
    const int count=static_cast<int>(x.size());
    double bestScore=-1.;
    auto consider=[&](const double c[3],bool linear) {
        double score=0.,left=0.,right=0.,totalLeft=0.,totalRight=0.;
        int nl=0,nr=0;
        for (int k=0;k<count;++k) {
            (k<split?totalLeft:totalRight)+=weights[k];
            const double residual=std::abs(y[k]-c[0]-c[1]*x[k]-c[2]*x[k]*x[k]);
            if (residual<=tolerance) {
                score+=weights[k];
                (k<split?left:right)+=weights[k];
                ++(k<split?nl:nr);
            }
        }
        if (nl<3 || nr<3 || left<0.5*totalLeft || right<0.5*totalRight) return;
        if (!linear) score*=0.98; // 避免用不必要的曲率解释局部缺陷
        if (score>bestScore) { bestScore=score; std::copy(c,c+3,coefficients); }
    };
    double initial[3]{};
    if (fitQuadratic(x,y,weights,initial)) consider(initial,false);
    // 采用确定性候选模型，包括由两侧支撑点确定的直线。
    unsigned seed=1234567;
    for(int trial=0;trial<96;++trial) {
        auto randomIndex=[&](int size) { seed=1664525u*seed+1013904223u; return int(seed%size); };
        int i=randomIndex(split),j=split+randomIndex(count-split),k=randomIndex(count);
        if(std::abs(x[j]-x[i])<1.e-10) continue;
        double slope=(y[j]-y[i])/(x[j]-x[i]);
        double line[3]{y[i]-slope*x[i],slope,0.}; consider(line,true);
        if(k==i || k==j || std::abs(x[k]-x[i])<1.e-10 || std::abs(x[k]-x[j])<1.e-10) continue;
        double curve[3]{};
        std::vector<double> sx{x[i],x[j],x[k]},sy{y[i],y[j],y[k]},sw{1.,1.,1.};
        if(fitQuadratic(sx,sy,sw,curve)) consider(curve,false);
    }
    if(bestScore<0.) return false;
    const bool linear=std::abs(coefficients[2])<1.e-14;
    // 使用一致点集细化模型，并采用弧长权重而非采样点数量。
    for(int pass=0;pass<3;++pass) {
        std::vector<double> w=weights;
        double sw=0.,sx=0.,sy=0.,sxx=0.,sxy=0.;
        for(int k=0;k<count;++k) {
            if(std::abs(y[k]-coefficients[0]-coefficients[1]*x[k]-coefficients[2]*x[k]*x[k])>tolerance) w[k]=0.;
            sw+=w[k];sx+=w[k]*x[k];sy+=w[k]*y[k];sxx+=w[k]*x[k]*x[k];sxy+=w[k]*x[k]*y[k];
        }
        if(linear) {
            double den=sw*sxx-sx*sx;if(den<=1.e-14) return false;
            coefficients[1]=(sw*sxy-sx*sy)/den;coefficients[0]=(sy-coefficients[1]*sx)/sw;coefficients[2]=0.;
        } else if(!fitQuadratic(x,y,w,coefficients)) return false;
    }
    inliers.resize(count);
    double totals[2]{},accepted[2]{};int numbers[2]{};
    for(int k=0;k<count;++k) {
        int side=k<split?0:1;totals[side]+=weights[k];
        inliers[k]=std::abs(y[k]-coefficients[0]-coefficients[1]*x[k]-coefficients[2]*x[k]*x[k])<=tolerance;
        if(inliers[k]) {accepted[side]+=weights[k];++numbers[side];}
    }
    return numbers[0]>=3 && numbers[1]>=3 && accepted[0]>=.5*totals[0] && accepted[1]>=.5*totals[1];
}
}

// 单次处理期间输入保持不变，端点和未受影响的采样点保持原值。
inline void repairMicroBumps(std::vector<gp_Pnt>& points, bool closed,
                            const SectionProcessingOptions& o, SectionResult* result)
{
    SectionContour before;
    before.cleanedPoints = points;
    before.closed = closed;
    result->beforeBumpContours.push_back(std::move(before));
    if (!o.enableMicroBumpCleaning || points.size() < 9) return;
    const auto original = points;
    const int n = static_cast<int>(points.size());
    std::vector<bool> occupied(n, false);
    auto index = [n](int i) { return (i % n + n) % n; };
    auto at = [&](int i) -> const gp_Pnt& { return original[index(i)]; };
    const double eps = std::max(1.e-10, result->tolerances.duplicatePoint);
    // 参考切线要求每侧至少有三个采样点。
    for (int a = 0; a < n; ++a) {
        if (!closed && a < 3) continue;
        double length = 0.;
        for (int b = a + 1; b < a + n - 6; ++b) {
            if (!closed && b + 3 >= n) break;
            length += at(b - 1).Distance(at(b));
            if (length > o.maximumBumpWidth) break;
            if (b - a < 2) continue;
            bool overlap = false;
            for (int k = a; k <= b; ++k) overlap |= occupied[index(k)];
            if (overlap) continue;
            int left = a, right = b;
            double l = 0., r = 0.;
            while (a - left < n - (b - a) - 3 && (closed || left > 0)
                   && l < std::max(o.bumpReferenceLength,o.bumpSupportSearchLength)) {
                l += at(left).Distance(at(left - 1)); --left;
            }
            while (right - b < n - (b - a) - (a - left) - 1
                   && (closed || right + 1 < n) && r < std::max(o.bumpReferenceLength,o.bumpSupportSearchLength)) {
                r += at(right).Distance(at(right + 1)); ++right;
            }
            if (a - left < 3 || right - b < 3 || l < o.bumpReferenceLength
                || r < o.bumpReferenceLength) { ++result->bumpSupportRejected; continue; }
            gp_Vec t0(at(left), at(a)), t1(at(b), at(right));
            gp_Vec chord(at(a), at(b));
            const double span = chord.Magnitude();
            if (span <= eps || t0.Magnitude() <= eps || t1.Magnitude() <= eps) continue;
            auto aligned = [](gp_Vec x, gp_Vec y, double degrees) {
                if (x.Magnitude() <= 1.e-12 || y.Magnitude() <= 1.e-12) return false;
                return x.Dot(y) / (x.Magnitude() * y.Magnitude())
                    >= std::cos(degrees * 0.017453292519943295);
            };
            // 在截面平面内处理；对于单独导入的轮廓，先推导其所在平面。
            gp_Vec plane;
            if (result->hasSectionFrame)
                plane=gp_Vec(result->axisDirection).Crossed(gp_Vec(result->radialDirection));
            else {
                for (int k=left; k<=right; ++k) {
                    plane=chord.Crossed(gp_Vec(at(a),at(k)));
                    if (plane.Magnitude()>eps*span) break;
                }
                if (plane.Magnitude()<=eps*span) {
                    gp_Vec seed=std::abs(chord.Z()/span)<0.9 ? gp_Vec(0,0,1) : gp_Vec(0,1,0);
                    plane=chord.Crossed(seed);
                }
            }
            if (plane.Magnitude()<=1.e-12) continue;
            plane.Normalize();
            gp_Vec base=chord; base.Normalize();
            gp_Vec normal=plane.Crossed(base); normal.Normalize();
            std::vector<int> support;
            for (int k=left; k<=a; ++k) support.push_back(k);
            for (int k=b; k<=right; ++k) support.push_back(k);
            // 仅根据支撑点计算主方向，排除毛刺内部点。
            double mx=0.,my=0.;
            for (int k:support) { gp_Vec v(at(a),at(k)); mx+=v.Dot(base); my+=v.Dot(normal); }
            mx/=support.size(); my/=support.size();
            double xx=0.,xy=0.,yy=0.;
            for (int k:support) {
                gp_Vec v(at(a),at(k)); double x=v.Dot(base)-mx,y=v.Dot(normal)-my;
                xx+=x*x; xy+=x*y; yy+=y*y;
            }
            double angle=0.5*std::atan2(2*xy,xx-yy);
            gp_Vec tangent=base*std::cos(angle)+normal*std::sin(angle);
            if (tangent.Dot(chord)<0.) tangent=-tangent;
            normal=plane.Crossed(tangent); normal.Normalize();
            const double scale=std::max(span+l+r,eps);
            auto tx=[&](int k) { return gp_Vec(at(a),at(k)).Dot(tangent)/scale; };
            auto ny=[&](int k) { return gp_Vec(at(a),at(k)).Dot(normal); };
            bool monotonic=true;
            // 支撑区域的毛刺可能折返；仅要求待修复段为单值。
            for (int k=a; k<b; ++k) if (tx(k+1)<=tx(k)+1.e-12) monotonic=false;
            if (!monotonic) { ++result->bumpShapeRejected; continue; }
            std::vector<double> xs,ys,weights;
            for (int k:support) {
                xs.push_back(tx(k)); ys.push_back(ny(k));
                // 弧长权重可防止密集采样区域主导拟合。
                double w=0.;
                if (k>left && k!=b) w+=at(k-1).Distance(at(k));
                if (k<right && k!=a) w+=at(k).Distance(at(k+1));
                weights.push_back(std::max(w/scale,1.e-12));
            }
            double c[3]{};
            std::vector<bool> stable;
            if (!micro_bump_detail::fitStableSupports(xs,ys,weights,a-left+1,
                                                     o.bumpSupportResidual,c,stable)) {
                ++result->bumpSupportRejected; continue;
            }
            auto predict=[&](double x) { return c[0]+c[1]*x+c[2]*x*x; };
            auto predictedTangent=[&](double x) {
                gp_Vec v=tangent+normal*((c[1]+2*c[2]*x)/scale); v.Normalize(); return v;
            };
            bool reliable=true;
            for (std::size_t i=0;i<support.size();++i) {
                if(stable[i] && std::abs(gp_Vec(at(a),at(support[i])).Dot(plane))>o.bumpSupportResidual) reliable=false;
            }
            double stableLength[2]{};
            for(std::size_t i=0;i+1<support.size();++i) {
                int k=support[i],next=support[i+1];
                if(next!=k+1 || !stable[i] || !stable[i+1]) continue;
                if(aligned(gp_Vec(at(k),at(next)),predictedTangent((tx(k)+tx(next))*0.5),o.bumpTangentAngleDegrees))
                    stableLength[k<=a?0:1]+=at(k).Distance(at(next));
            }
            reliable &= stableLength[0]>=.5*o.bumpReferenceLength && stableLength[1]>=.5*o.bumpReferenceLength;
            if (!reliable) { ++result->bumpSupportRejected; continue; }
            double height=0.,positive=0.,negative=0.;
            for (int k=a+1; k<b; ++k) {
                double residual=(ny(k)-predict(tx(k)))
                    /std::sqrt(1.+std::pow((c[1]+2*c[2]*tx(k))/scale,2));
                height=std::max(height,std::abs(residual));
                positive=std::max(positive,residual); negative=std::max(negative,-residual);
            }
            if (height<=o.bumpDetectionTolerance) continue;
            if (std::min(positive,negative)>o.bumpSupportResidual) {
                ++result->bumpShapeRejected; continue;
            }
            // 端点必须已回到预测的正常轮廓上。
            if (std::abs(ny(a)-predict(tx(a)))>o.bumpSupportResidual
                || std::abs(ny(b)-predict(tx(b)))>o.bumpSupportResidual) {
                ++result->bumpShapeRejected; continue;
            }
            t0=predictedTangent(tx(a)); t1=predictedTangent(tx(b));
            if (!aligned(t0,chord,60.) || !aligned(t1,chord,60.)) {
                ++result->bumpShapeRejected; continue;
            }
            // 实际相邻的折线边也必须平滑连接。
            if (!aligned(gp_Vec(at(a-1),at(a)),t0,o.bumpTangentAngleDegrees)
                || !aligned(gp_Vec(at(b),at(b+1)),t1,o.bumpTangentAngleDegrees)) {
                ++result->bumpShapeRejected; continue;
            }
            // 三次 Hermite 基线沿两侧轮廓延伸，并保留曲率信息。
            // 匹配端点的纵向速度，并设置上限以避免插值过冲。
            const double speed0=std::min(1.5*span,span/std::max(0.5,t0.Dot(chord)/span));
            const double speed1=std::min(1.5*span,span/std::max(0.5,t1.Dot(chord)/span));
            auto reference = [&](double u) {
                const double u2 = u*u, u3 = u2*u;
                return at(a).Translated(chord * (-2*u3 + 3*u2)
                    + t0 * (speed0*(u3 - 2*u2 + u))
                    + t1 * (speed1*(u3 - u2)));
            };
            std::vector<gp_Pnt> replacement;
            double travelled = 0.;
            bool valid = true;
            bool budget = false;
            for (int k = a + 1; k < b; ++k) {
                travelled += at(k - 1).Distance(at(k));
                const auto p = reference(travelled / length);
                const gp_Vec delta(p, at(k));
                const double d = delta.Magnitude();
                if (d > o.maximumBumpDisplacement) { valid=false; budget=true; }
                replacement.push_back(p);
            }
            if (budget) { ++result->bumpBudgetRejected; continue; }
            // 还需检查插值区间内部，不能只检查替换采样点。
            for (int sample=0; sample<=32; ++sample) {
                const gp_Pnt p=reference(sample/32.);
                const gp_Vec v(at(a),p);
                if (std::abs(v.Dot(normal)-predict(v.Dot(tangent)/scale))
                    > 2*o.bumpSupportResidual) valid=false;
            }
            gp_Pnt previous=at(a);
            for (const auto& p:replacement) {
                if (gp_Vec(previous,p).Dot(tangent)<=eps) valid=false;
                previous=p;
            }
            if (gp_Vec(previous,at(b)).Dot(tangent)<=eps) valid=false;
            if (!valid) { ++result->bumpSafetyRejected; continue; }
            // 保持连接平滑；拒绝终点仍位于毛刺内部的候选段。
            if (!aligned(gp_Vec(at(a), replacement.front()), t0, o.bumpTangentAngleDegrees)
                || !aligned(gp_Vec(replacement.back(), at(b)), t1, o.bumpTangentAngleDegrees)) {
                ++result->bumpSafetyRejected; continue;
            }
            // 拒绝与非相邻线段接触或相交的任何新线段。
            // 在三维空间计算线段距离，避免投影掩盖相交情况。
            auto intersects = [eps](const gp_Pnt& p, const gp_Pnt& q,
                                    const gp_Pnt& x, const gp_Pnt& y) {
                gp_Vec u(p,q), v(x,y), w(x,p);
                double aa=u.Dot(u), bb=u.Dot(v), cc=v.Dot(v), dd=u.Dot(w), ee=v.Dot(w);
                if (aa <= eps*eps || cc <= eps*eps) return true;
                double den=aa*cc-bb*bb;
                double s=den > 1.e-20*aa*cc ? std::clamp((bb*ee-cc*dd)/den,0.,1.) : 0.;
                double t=std::clamp((bb*s+ee)/cc,0.,1.);
                s=std::clamp((bb*t-dd)/aa,0.,1.);
                t=std::clamp((bb*s+ee)/cc,0.,1.);
                return (w+u*s-v*t).Magnitude() <= eps;
            };
            auto candidate = points;
            for (int k=a+1; k<b; ++k) candidate[index(k)]=replacement[k-a-1];
            for (int k=a; valid && k<b; ++k) {
                int i=index(k), next=index(k+1);
                for (int j=0; j<(closed?n:n-1); ++j) {
                    int jnext=index(j+1);
                    if (j==i || j==next || jnext==i) continue;
                    if (intersects(candidate[i],candidate[next],candidate[j],candidate[jnext])) {
                        valid=false; break;
                    }
                }
            }
            if (!valid) { ++result->bumpSafetyRejected; continue; }
            for (int k=a; k<=b; ++k) occupied[index(k)]=true;
            for (int k=a+1; k<b; ++k) {
                double movement=at(k).Distance(candidate[index(k)]);
                if (movement > eps) ++result->movedBumpPointCount;
                result->maximumBumpMovement=std::max(result->maximumBumpMovement,movement);
            }
            points.swap(candidate);
            ++result->repairedBumpCount;
            break;
        }
    }
}
