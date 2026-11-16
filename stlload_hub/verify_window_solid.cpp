#include "SurfaceJoin.h"
#include "SurfaceFit.h"
#include "Stlsection.h"
#include <QCoreApplication>
#include <QFile>
#include <QDir>
#include <QJsonDocument>
#include <QJsonObject>
#include <QJsonArray>
#include <QTextStream>
#include <BRepBuilderAPI_MakeFace.hxx>
#include <BRepPrimAPI_MakeBox.hxx>
#include <BRepAlgoAPI_Cut.hxx>
#include <BRepCheck_Analyzer.hxx>
#include <BRepGProp.hxx>
#include <GProp_GProps.hxx>
#include <STEPControl_Writer.hxx>
#include <STEPControl_Reader.hxx>
#include <TopoDS_Compound.hxx>
#include <BRep_Builder.hxx>
#include <gp_Vec.hxx>
#include <limits>
#include <sstream>
int main(int argc,char** argv)
{
    QCoreApplication app(argc,argv);QTextStream log(stdout);
    const auto args=app.arguments();if(args.size()!=4)return 1;
    Stlsection model;QString error;
    if(!model.loadStl(args[1],&error)){log<<error;return 2;}
    WindowSolidOptions options;double radius;
    if(!model.estimateHubAxis(&options.axisOrigin,&options.axisDirection,&radius,&error)){log<<error;return 3;}
    auto box=model.boundingBox();options.minimumDepth=std::numeric_limits<double>::max();options.maximumDepth=-options.minimumDepth;
    for(int x=0;x<2;++x)for(int y=0;y<2;++y)for(int z=0;z<2;++z){
        gp_Pnt point(x?box.maximum.X():box.minimum.X(),y?box.maximum.Y():box.minimum.Y(),z?box.maximum.Z():box.minimum.Z());
        double depth=gp_Vec(options.axisOrigin,point).Dot(gp_Vec(options.axisDirection));
        options.minimumDepth=std::min(options.minimumDepth,depth);options.maximumDepth=std::max(options.maximumDepth,depth);
    }
    options.minimumDepth-=std::max(box.diagonal*0.02,1.0);options.maximumDepth+=std::max(box.diagonal*0.02,1.0);
    options.filletRadius=1;
    log<<"Axis "<<options.axisDirection.X()<<","<<options.axisDirection.Y()<<","<<options.axisDirection.Z()
       <<" cap depths "<<options.minimumDepth<<","<<options.maximumDepth<<Qt::endl;
    std::array<TopoDS_Face,4> faces;std::array<std::vector<gp_Pnt>,4> samples;
    QDir directory(args[2]);QJsonArray inputFiles;
    for(int i=0;i<4;++i){
        auto names=directory.entryList({QStringLiteral("fitted_surface_%1_*.json").arg(i+1)},QDir::Files,QDir::Name|QDir::Reversed);
        if(names.isEmpty()){log<<"Missing patch "<<i+1;return 4;}
        inputFiles.append(directory.filePath(names.front()));
        QFile file(directory.filePath(names.front()));if(!file.open(QIODevice::ReadOnly))return 5;
        auto root=QJsonDocument::fromJson(file.readAll()).object();
        for(auto patch:root["patches"].toArray())for(auto entry:patch.toObject()["validation_samples_mm"].toArray()){
            auto xyz=entry.toArray();samples[i].emplace_back(xyz[0].toDouble(),xyz[1].toDouble(),xyz[2].toDouble());
        }
        SurfaceFitResult fit;
        if(!fitSurfacePatch(samples[i],0.1,&fit,&error)){log<<"Fit "<<i+1<<": "<<error;return 6;}
        faces[i]=BRepBuilderAPI_MakeFace(fit.surface,1e-6).Face();
        log<<"Patch "<<i+1<<": "<<names.front()<<" samples "<<samples[i].size()<<" planar "<<fit.planar<<" deviation "<<fit.maximumDeviation<<Qt::endl;
    }
    WindowSolidResult result;
    const bool success=buildWindowCuttingSolid(faces,samples,options,&result,&error);
    QDir().mkpath(args[3]);
    if(!result.intersections.IsNull()){
        STEPControl_Writer writer;writer.Transfer(result.intersections,STEPControl_AsIs);
        std::ostringstream out;writer.WriteStream(out);QFile file(QDir(args[3]).filePath("actual_intersections.step"));
        if(file.open(QIODevice::WriteOnly))file.write(QByteArray::fromStdString(out.str()));
    }
    if(!success){log<<error<<Qt::endl;return 7;}
    if(!isValidCuttingSolid(result.solid,&error)){log<<error;return 8;}
    STEPControl_Writer writer;writer.Transfer(result.solid,STEPControl_AsIs);std::ostringstream out;writer.WriteStream(out);
    QFile file(QDir(args[3]).filePath("actual_window_cutter.step"));if(!file.open(QIODevice::WriteOnly))return 9;file.write(QByteArray::fromStdString(out.str()));
    file.close();
    STEPControl_Reader reader;std::istringstream inputStep(out.str());
    if(reader.ReadStream("actual_window_cutter.step",inputStep)!=IFSelect_RetDone)return 11;
    reader.TransferRoots();const auto imported=reader.OneShape();
    if(!isValidCuttingSolid(imported,&error)){log<<error;return 12;}
    const double pad=box.diagonal*0.05;
    auto blank=BRepPrimAPI_MakeBox(box.minimum.Translated(gp_Vec(-pad,-pad,-pad)),
        box.maximum.Translated(gp_Vec(pad,pad,pad))).Solid();
    BRepAlgoAPI_Cut cut(blank,imported);cut.Build();
    if(!cut.IsDone()){log<<"Boolean cut failed";return 10;}
    GProp_GProps before,after;BRepGProp::VolumeProperties(blank,before);BRepGProp::VolumeProperties(cut.Shape(),after);
    const bool booleanOk=cut.IsDone()&&BRepCheck_Analyzer(cut.Shape()).IsValid()&&after.Mass()>0&&after.Mass()<before.Mass();
    log<<"Solid valid, fillet "<<result.filletApplied<<", extension "<<result.extensionLength<<" mm, boolean cut "<<booleanOk
       <<", blank volume "<<before.Mass()<<", cut volume "<<after.Mass()<<Qt::endl<<result.warning<<Qt::endl;
    QJsonObject report;
    report["source_stl"]=args[1];report["source_patch_files"]=inputFiles;
    report["unit"]="mm";report["closed_solid_valid"]=true;report["step_roundtrip_valid"]=true;
    report["test_blank"]="expanded axis-aligned model bounding box; not the final intended blank";
    report["boolean_subtraction_passed"]=booleanOk;report["blank_volume_mm3"]=before.Mass();report["cut_volume_mm3"]=after.Mass();
    report["fillet_applied"]=result.filletApplied;report["requested_fillet_radius_mm"]=options.filletRadius;
    report["warning"]=result.warning;report["extension_mm"]=result.extensionLength;
    report["axis_direction"]=QJsonArray{options.axisDirection.X(),options.axisDirection.Y(),options.axisDirection.Z()};
    report["axis_origin"]=QJsonArray{options.axisOrigin.X(),options.axisOrigin.Y(),options.axisOrigin.Z()};
    report["cap_depths_mm"]=QJsonArray{options.minimumDepth,options.maximumDepth};
    QFile reportFile(QDir(args[3]).filePath("actual_window_cutter.validation.json"));
    if(reportFile.open(QIODevice::WriteOnly))reportFile.write(QJsonDocument(report).toJson());
    return booleanOk?0:10;
}
