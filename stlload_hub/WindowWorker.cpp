#include "WindowWorker.h"
#include "SurfaceFit.h"
#include "SurfaceJoin.h"
#include <BRepTools.hxx>
#include <BRep_Builder.hxx>
#include <BRepBuilderAPI_MakeFace.hxx>
#include <BRepCheck_Analyzer.hxx>
#include <BRepGProp.hxx>
#include <GProp_GProps.hxx>
#include <TopoDS.hxx>
#include <QJsonArray>
#include <QJsonDocument>
#include <QFile>
#include <QFileInfo>
#include <QSaveFile>
#include <cmath>
#include <sstream>
#include <stdexcept>

namespace {
void require(bool ok,const QString& message){if(!ok)throw std::runtime_error(message.toStdString());}
QString path(const QDir& base,const QJsonObject& o,const char* key){
    auto value=o.value(key);require(value.isString()&&!value.toString().isEmpty(),QString("Missing ")+key);
    return base.absoluteFilePath(value.toString());
}
QJsonObject read(const QString& name){QFile f(name);require(f.open(QIODevice::ReadOnly),"Cannot read artifact");
    QJsonParseError e;auto d=QJsonDocument::fromJson(f.readAll(),&e);require(e.error==QJsonParseError::NoError&&d.isObject(),"Invalid artifact JSON");return d.object();}
void save(const QString& name,const QJsonObject& value){QSaveFile f(name);auto bytes=QJsonDocument(value).toJson();
    require(f.open(QIODevice::WriteOnly)&&f.write(bytes)==bytes.size()&&f.commit(),"Cannot write artifact");}
gp_Pnt point(const QJsonValue& value){auto a=value.toArray();require(a.size()==3,"Invalid XYZ");
    for(auto v:a)require(v.isDouble()&&std::isfinite(v.toDouble()),"Nonfinite XYZ");return {a[0].toDouble(),a[1].toDouble(),a[2].toDouble()};}
std::vector<gp_Pnt> samples(const QJsonObject& o){std::vector<gp_Pnt> p;for(auto v:o["samples"].toArray())p.push_back(point(v));require(p.size()>=9,"Insufficient samples");return p;}
void shapeWrite(const TopoDS_Shape& shape,const QString& name){std::ostringstream stream;BRepTools::Write(shape,stream);
    auto bytes=stream.str();QSaveFile f(name);require(f.open(QIODevice::WriteOnly)&&f.write(bytes.data(),qint64(bytes.size()))==qint64(bytes.size())&&f.commit(),"Cannot write BREP");}
TopoDS_Shape shapeRead(const QString& name){QFile f(name);require(f.open(QIODevice::ReadOnly),"Cannot read BREP");std::istringstream in(f.readAll().toStdString());
    TopoDS_Shape s;BRep_Builder b;BRepTools::Read(s,in,b);require(!s.IsNull()&&BRepCheck_Analyzer(s).IsValid(),"Invalid BREP");return s;}
}
bool isWindowOperation(const QString& op){return op=="fit_window_surface"||op=="build_window_cutter";}
QJsonObject executeWindowOperation(const QJsonObject& request,const QDir& base,std::unique_ptr<QTemporaryDir>& run){
    require(request["schema_version"].toInt()==1&&request["unit"]=="mm","Unsupported schema/unit");
    const auto operation=request["operation"].toString();const auto task=request["task_id"].toString();require(!task.isEmpty(),"Missing task identity");
    auto output=path(base,request,"output_directory");require(QDir().mkpath(output),"Cannot create output directory");
    run=std::make_unique<QTemporaryDir>(QDir(output).filePath("window-XXXXXX"));require(run->isValid(),"Cannot create artifact directory");
    auto report=QDir(run->path()).filePath("geometry.json");auto geometry=QDir(run->path()).filePath("geometry.brep");
    QJsonObject data{{"unit","mm"},{"operation",operation},{"task_id",task}};QString error;
    if(operation=="fit_window_surface"){
        auto source=read(path(base,request,"samples_path"));auto p=samples(source);auto tolerance=request["tolerance_mm"].toDouble(-1);
        SurfaceFitResult fit;require(fitSurfacePatch(p,tolerance,&fit,&error),error);
        BRepBuilderAPI_MakeFace maker(fit.surface,1e-6);require(maker.IsDone()&&BRepCheck_Analyzer(maker.Face()).IsValid(),"Invalid fitted face");
        shapeWrite(maker.Face(),geometry);data["samples"]=source["samples"];data["region_id"]=source["region_id"];
        data["source_sha256"]=source["source_sha256"];data["maximum_deviation_mm"]=fit.maximumDeviation;data["planar"]=fit.planar;data["tolerance_mm"]=tolerance;
    }else{
        auto reports=request["surface_reports"].toArray();require(reports.size()==4,"Exactly four surface reports required");
        std::array<TopoDS_Face,4> faces;std::array<std::vector<gp_Pnt>,4> points;
        QString hash;
        for(int i=0;i<4;++i){require(reports[i].isString(),"Invalid surface report");auto name=base.absoluteFilePath(reports[i].toString());auto d=read(name);
            require(d["operation"]=="fit_window_surface","Wrong surface artifact");
            if(i==0)hash=d["source_sha256"].toString();require(!hash.isEmpty()&&d["source_sha256"]==hash,"Mixed source meshes");
            auto s=shapeRead(QFileInfo(name).absoluteDir().filePath("geometry.brep"));require(s.ShapeType()==TopAbs_FACE,"Expected fitted face");faces[i]=TopoDS::Face(s);points[i]=samples(d);
        }
        auto axis=request["axis"].toObject();auto direction=point(axis["direction"]);require(std::hypot(direction.X(),direction.Y(),direction.Z())>1e-12,"Invalid axis");
        WindowSolidOptions options;options.axisOrigin=point(axis["origin"]);options.axisDirection=gp_Dir(direction.X(),direction.Y(),direction.Z());
        options.minimumDepth=request["minimum_depth_mm"].toDouble();options.maximumDepth=request["maximum_depth_mm"].toDouble();
        options.fitTolerance=request["tolerance_mm"].toDouble(.1);options.filletRadius=request["fillet_radius_mm"].toDouble(0);
        WindowSolidResult solid;require(buildWindowCuttingSolid(faces,points,options,&solid,&error),error);require(isValidCuttingSolid(solid.solid,&error),error);
        shapeWrite(solid.solid,geometry);GProp_GProps props;BRepGProp::VolumeProperties(solid.solid,props);
        data["source_sha256"]=hash;data["closed_solid"]=true;data["solid_count"]=1;data["volume_mm3"]=props.Mass();
        data["fillet_applied"]=solid.filletApplied;data["warning"]=solid.warning;data["axis"]=axis;
    }
    data["geometry_path"]=geometry;save(report,data);
    QJsonArray artifacts{QJsonObject{{"type","window_geometry"},{"path",geometry}},QJsonObject{{"type","window_report"},{"path",report}}};
    auto metrics=data;metrics.remove("samples");
    return {{"schema_version",1},{"status","success"},{"operation",operation},{"task_id",task},{"unit","mm"},
        {"run_id",QFileInfo(run->path()).fileName()},{"metrics",metrics},{"artifacts",artifacts},{"warnings",QJsonArray{}},{"error",QJsonValue::Null}};
}
