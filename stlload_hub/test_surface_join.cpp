#include "SurfaceJoin.h"
#include <Geom_BezierSurface.hxx>
#include <GeomConvert.hxx>
#include <Geom_BSplineSurface.hxx>
#include <TColgp_Array2OfPnt.hxx>
#include <BRepBuilderAPI_MakeFace.hxx>
#include <BRepPrimAPI_MakeBox.hxx>
#include <BRepAlgoAPI_Cut.hxx>
#include <BRepGProp.hxx>
#include <GProp_GProps.hxx>
#include <BRepCheck_Analyzer.hxx>
#include <TopExp_Explorer.hxx>
#include <STEPControl_Writer.hxx>
#include <STEPControl_Reader.hxx>
#include <iostream>
#include <sstream>
static double volume(const TopoDS_Shape& shape){GProp_GProps p;BRepGProp::VolumeProperties(shape,p);return p.Mass();}
int main()
{
    std::array<TopoDS_Face,4> faces;std::array<std::vector<gp_Pnt>,4> samples;
    for(int side=0;side<4;++side) {
        auto point=[&](double t,double z) {
            if(side==0) return gp_Pnt(-5,t,z);
            if(side==1) return gp_Pnt(t,5,z);
            if(side==2) return gp_Pnt(5,t,z);
            return gp_Pnt(t,-5,z);
        };
        const double half=side==3?0.1:1.0; // tiny fourth patch used to fail extension
        TColgp_Array2OfPnt poles(1,2,1,2);
        poles.SetValue(1,1,point(-half,4));poles.SetValue(1,2,point(-half,6));
        poles.SetValue(2,1,point(half,4));poles.SetValue(2,2,point(half,6));
        Handle(Geom_BezierSurface) bezier=new Geom_BezierSurface(poles);
        faces[side]=BRepBuilderAPI_MakeFace(GeomConvert::SurfaceToBSplineSurface(bezier),1e-6).Face();
        for(int i=0;i<5;++i) for(int j=0;j<5;++j) samples[side].push_back(point(-half+half*i/2.0,4+j*0.5));
    }
    WindowSolidOptions options;options.minimumDepth=-3;options.maximumDepth=13;options.filletRadius=1;
    WindowSolidResult result;QString error;
    if(!buildWindowCuttingSolid(faces,samples,options,&result,&error)) {std::cerr<<error.toStdString();return 1;}
    if(!isValidCuttingSolid(result.solid,&error) || !result.filletApplied) {std::cerr<<error.toStdString()<<result.warning.toStdString()<<" volume="<<volume(result.solid);return 2;}
    auto blank=BRepPrimAPI_MakeBox(gp_Pnt(-10,-10,-2),20,20,14).Solid();
    BRepAlgoAPI_Cut cut(blank,result.solid);cut.Build();
    if(!cut.IsDone() || !BRepCheck_Analyzer(cut.Shape()).IsValid()
       || volume(cut.Shape())>=volume(blank)-1 || volume(cut.Shape())<=0) return 3;
    STEPControl_Writer writer;if(writer.Transfer(result.solid,STEPControl_AsIs)!=IFSelect_RetDone)return 4;
    std::ostringstream out;if(writer.WriteStream(out)!=IFSelect_RetDone)return 5;
    STEPControl_Reader reader;std::istringstream in(out.str());
    if(reader.ReadStream("cutter.step",in)!=IFSelect_RetDone)return 6;
    reader.TransferRoots();if(!isValidCuttingSolid(reader.OneShape(),&error))return 7;
    options.filletRadius=100;
    if(!buildWindowCuttingSolid(faces,samples,options,&result,&error) || result.filletApplied || result.warning.isEmpty()
       || !isValidCuttingSolid(result.solid))return 8;
    options.filletRadius=0;
    if(!buildWindowCuttingSolid(faces,samples,options,&result,&error) || result.filletApplied)return 9;
    int faceCount=0;for(TopExp_Explorer ex(result.solid,TopAbs_FACE);ex.More();ex.Next())++faceCount;
    if(faceCount!=6 || result.fittedSideFaceCount!=4 || result.fittedSideSurfaceCount!=4 || result.capFaceCount!=2
       || result.maximumBoundarySampleDeviation>1e-5)return 11;
    faces[0].Nullify();
    if(buildWindowCuttingSolid(faces,samples,options,&result,&error) || !result.solid.IsNull())return 10;
    std::cout<<"Tiny patch, capped solid, fillet, STEP roundtrip and boolean subtraction passed.\n";
    return 0;
}
