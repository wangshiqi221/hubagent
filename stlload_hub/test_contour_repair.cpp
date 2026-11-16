#include "Stlsection.h"
#include <cmath>
#include <iostream>
static void circle(SectionResult& r, double cx, bool open, int omitted = 1) {
    const int n=120;
    auto point=[&](int i) { double a=i*2*std::acos(-1.)/n; return gp_Pnt(cx+10*std::cos(a),10*std::sin(a),0); };
    for(int i=0;i<(open?n-omitted:n);++i) {
        auto a=point(i), b=point((i+1)%n);
        r.rawSegments.push_back({a,b,-1});
        r.rawPoints.push_back({a,-1,-1,-1});
        r.rawPoints.push_back({b,-1,-1,-1});
    }
}
int main() {
    SectionProcessingOptions options; options.enableMicroBumpCleaning=false;
    for (int omitted : {1,30}) {
        SectionResult r; r.success=true;
        circle(r,0,false); circle(r,40,true,omitted);
        if(!Stlsection::cleanSectionResult(&r,options) || r.contours.size()!=2) return 1;
        int closed=0; for(const auto& c:r.contours) closed+=c.closed;
        if(closed!=(omitted==1?2:1)) return 2;
    }
    std::cout << "component closure and unsafe-gap preservation passed\n";
}
