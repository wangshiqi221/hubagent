#include "SectionWorkflow.h"
#include "WindowWorker.h"
#include <QCoreApplication>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QJsonParseError>
#include <QSaveFile>
#include <QTemporaryDir>
#include <Standard_Failure.hxx>
#include <Message.hxx>
#include <Message_PrinterOStream.hxx>
#include <gp_Ax1.hxx>
#include <cmath>
#include <cstdio>
#include <memory>
#include <set>
#include <stdexcept>

namespace {
struct Failure { QString code, message; int exitCode; };
[[noreturn]] void fail(const char* code, const QString& message, int exitCode=2)
{ throw Failure{QString::fromLatin1(code),message,exitCode}; }

QString identity(const QString& path)
{
    const QFileInfo info(path);
    return info.exists() ? info.canonicalFilePath() : info.absoluteFilePath();
}
bool samePath(const QString& a, const QString& b)
{
#ifdef Q_OS_WIN
    return identity(a).compare(identity(b),Qt::CaseInsensitive)==0;
#else
    return identity(a)==identity(b);
#endif
}
QString resolved(const QDir& base, const QString& value)
{ return QDir::cleanPath(QFileInfo(value).isAbsolute() ? value : base.absoluteFilePath(value)); }

QString stringField(const QJsonObject& object,const char* key)
{
    const auto value=object.value(QString::fromLatin1(key));
    if (!value.isString() || value.toString().trimmed().isEmpty())
        fail("INVALID_REQUEST",QStringLiteral("%1 must be a non-empty string").arg(QString::fromLatin1(key)));
    return value.toString();
}
QJsonArray xyz(double x,double y,double z) { return {x,y,z}; }
QJsonArray xyz(const gp_Pnt& p) { return xyz(p.X(),p.Y(),p.Z()); }
QJsonArray xyz(const gp_Dir& p) { return xyz(p.X(),p.Y(),p.Z()); }
gp_Pnt pointField(const QJsonObject& object,const char* key)
{
    const auto value=object.value(QString::fromLatin1(key));
    if (!value.isArray() || value.toArray().size()!=3)
        fail("INVALID_REQUEST",QStringLiteral("%1 must contain three finite numbers").arg(QString::fromLatin1(key)));
    const auto array=value.toArray();
    for (const auto item: array)
        if (!item.isDouble() || !std::isfinite(item.toDouble()))
            fail("INVALID_REQUEST","Frame coordinates must be finite numbers");
    return {array[0].toDouble(),array[1].toDouble(),array[2].toDouble()};
}
gp_Dir directionField(const QJsonObject& object,const char* key)
{
    const auto p=pointField(object,key);
    const double length=std::hypot(p.X(),p.Y(),p.Z());
    if (!std::isfinite(length) || length<=1.e-12) fail("INVALID_FRAME","Direction must be non-zero and finite");
    return gp_Dir(p.X()/length,p.Y()/length,p.Z()/length);
}
void writeJson(const QString& path,const QJsonObject& object)
{
    QSaveFile file(path);
    const QByteArray data=QJsonDocument(object).toJson(QJsonDocument::Indented);
    if (!file.open(QIODevice::WriteOnly) || file.write(data)!=data.size() || !file.commit())
        fail("OUTPUT_WRITE_FAILED",QStringLiteral("Cannot atomically write %1: %2").arg(path,file.errorString()),5);
}
void printJson(const QJsonObject& object)
{
    const auto bytes=QJsonDocument(object).toJson(QJsonDocument::Compact)+"\n";
    std::fwrite(bytes.constData(),1,static_cast<std::size_t>(bytes.size()),stdout);
    std::fflush(stdout);
}

SectionProcessingOptions parseProcessing(const QJsonObject& object)
{
    SectionProcessingOptions o;
    const std::set<QString> allowed{"duplicate_tolerance_mm","enable_micro_bump_cleaning",
        "bump_detection_tolerance_mm","maximum_bump_width_mm","maximum_bump_displacement_mm",
        "bump_reference_length_mm","bump_support_search_length_mm","bump_support_residual_mm",
        "bump_tangent_angle_degrees"};
    for (auto it=object.begin();it!=object.end();++it)
        if (!allowed.count(it.key())) fail("INVALID_PROCESSING_OPTIONS","Unknown option: "+it.key());
    const auto number=[&](const char* key,double fallback,bool zero=false) {
        if (!object.contains(key)) return fallback;
        const auto v=object.value(key);
        if (!v.isDouble() || !std::isfinite(v.toDouble()) || (zero ? v.toDouble()<0 : v.toDouble()<=0))
            fail("INVALID_PROCESSING_OPTIONS",QStringLiteral("Invalid finite numeric option: %1").arg(key));
        return v.toDouble();
    };
    o.duplicateTolerance=number("duplicate_tolerance_mm",o.duplicateTolerance,true);
    if (object.contains("enable_micro_bump_cleaning")) {
        if (!object.value("enable_micro_bump_cleaning").isBool())
            fail("INVALID_PROCESSING_OPTIONS","enable_micro_bump_cleaning must be boolean");
        o.enableMicroBumpCleaning=object.value("enable_micro_bump_cleaning").toBool();
    }
    o.bumpDetectionTolerance=number("bump_detection_tolerance_mm",o.bumpDetectionTolerance);
    o.maximumBumpWidth=number("maximum_bump_width_mm",o.maximumBumpWidth);
    o.maximumBumpDisplacement=number("maximum_bump_displacement_mm",o.maximumBumpDisplacement);
    o.bumpReferenceLength=number("bump_reference_length_mm",o.bumpReferenceLength);
    o.bumpSupportSearchLength=number("bump_support_search_length_mm",o.bumpSupportSearchLength);
    o.bumpSupportResidual=number("bump_support_residual_mm",o.bumpSupportResidual);
    o.bumpTangentAngleDegrees=number("bump_tangent_angle_degrees",o.bumpTangentAngleDegrees);
    if (o.bumpSupportSearchLength<o.bumpReferenceLength || o.bumpTangentAngleDegrees>=90)
        fail("INVALID_PROCESSING_OPTIONS","Search length must be >= reference length; tangent angle must be < 90");
    return o;
}
QJsonObject processingJson(const SectionProcessingOptions& o)
{
    return {{"duplicate_tolerance_mm",o.duplicateTolerance},{"enable_micro_bump_cleaning",o.enableMicroBumpCleaning},
        {"bump_detection_tolerance_mm",o.bumpDetectionTolerance},{"maximum_bump_width_mm",o.maximumBumpWidth},
        {"maximum_bump_displacement_mm",o.maximumBumpDisplacement},{"bump_reference_length_mm",o.bumpReferenceLength},
        {"bump_support_search_length_mm",o.bumpSupportSearchLength},{"bump_support_residual_mm",o.bumpSupportResidual},
        {"bump_tangent_angle_degrees",o.bumpTangentAngleDegrees}};
}
QJsonArray contoursJson(const std::vector<SectionContour>& contours)
{
    QJsonArray array;
    int index=0;
    for (const auto& contour:contours) {
        QJsonArray points;
        for (const auto& point:contour.cleanedPoints) points.append(xyz(point));
        array.append(QJsonObject{{"contour_index",index++},{"closed",contour.closed},
            {"point_count",static_cast<int>(contour.cleanedPoints.size())},{"points",points}});
    }
    return array;
}
QJsonObject processingMetrics(const SectionResult& r)
{
    int closed=0;
    for (const auto& c:r.contours) if (c.closed) ++closed;
    return {{"contour_count",static_cast<int>(r.contours.size())},{"closed_contour_count",closed},
        {"open_contour_count",static_cast<int>(r.contours.size())-closed},
        {"duplicate_point_count",r.duplicatePointCount},{"invalid_point_count",r.invalidPointCount},
        {"repaired_bump_count",r.repairedBumpCount},{"moved_bump_point_count",r.movedBumpPointCount},
        {"maximum_bump_movement_mm",r.maximumBumpMovement},{"bump_support_rejected",r.bumpSupportRejected},
        {"bump_shape_rejected",r.bumpShapeRejected},{"bump_budget_rejected",r.bumpBudgetRejected},
        {"bump_safety_rejected",r.bumpSafetyRejected}};
}

struct Request {
    QString operation,taskId,stlPath,outputDirectory;
    SectionRequest frame;
    std::vector<double> angles;
    SectionProcessingOptions processing;
};
Request parse(const QJsonObject& object,const QDir& base)
{
    Request request;
    if (!object.value("schema_version").isDouble() || object.value("schema_version").toDouble()!=1)
        fail("INVALID_REQUEST","schema_version must be 1");
    request.operation=stringField(object,"operation");
    request.taskId=stringField(object,"task_id");
    if (request.taskId.size()>128) fail("INVALID_REQUEST","task_id is limited to 128 characters");
    if (request.operation!="inspect_stl" && request.operation!="estimate_axis" && request.operation!="compute_sections" && request.operation!="process_sections")
        fail("UNSUPPORTED_OPERATION","Supported operations: inspect_stl, estimate_axis, compute_sections, process_sections");
    if (stringField(object,"unit")!="mm") fail("UNSUPPORTED_UNIT","Only explicit millimeter units (mm) are supported");
    request.stlPath=resolved(base,stringField(object,"stl_path"));
    if (request.operation=="process_sections") {
        if (object.contains("processing") && !object.value("processing").isObject())
            fail("INVALID_PROCESSING_OPTIONS","processing must be an object");
        request.processing=parseProcessing(object.value("processing").toObject());
    } else if (object.contains("processing")) {
        fail("INVALID_PROCESSING_OPTIONS","processing is only supported by process_sections");
    }
    if (request.operation=="compute_sections" || request.operation=="process_sections") {
        request.outputDirectory=resolved(base,stringField(object,"output_directory"));
        if (!object.value("axis").isObject()) fail("INVALID_FRAME","axis must contain origin and direction");
        const auto axis=object.value("axis").toObject();
        request.frame.axisPoint=pointField(axis,"origin");
        request.frame.axisDirection=directionField(axis,"direction");
        request.frame.radialDirection=directionField(object,"radial_reference");
        if (std::abs(request.frame.axisDirection.Dot(request.frame.radialDirection))>1.e-8)
            fail("INVALID_FRAME","radial_reference must be perpendicular to axis.direction");
        const auto value=object.value("angles_degrees");
        if (!value.isArray() || value.toArray().isEmpty() || value.toArray().size()>360)
            fail("INVALID_ANGLES","angles_degrees requires 1 to 360 unique angles in [0,360)");
        std::set<double> seen;
        for (const auto angle:value.toArray()) {
            const double degrees=angle.toDouble(-1);
            if (!angle.isDouble() || !std::isfinite(degrees) || degrees<0 || degrees>=360 || !seen.insert(degrees).second)
                fail("INVALID_ANGLES","Angles must be unique finite numbers in [0,360)");
            request.angles.push_back(degrees);
        }
    }
    return request;
}

QJsonObject execute(const Request& request,std::unique_ptr<QTemporaryDir>& run)
{
    Stlsection model;
    QString error;
    if (!QFileInfo(request.stlPath).isFile() || !model.loadStl(request.stlPath,&error))
        fail("STL_LOAD_FAILED",QStringLiteral("Cannot load STL %1: %2").arg(request.stlPath,error),3);
    for (int i=1;i<=model.mesh()->NbNodes();++i) {
        const auto p=model.mesh()->Node(i);
        if (!std::isfinite(p.X()) || !std::isfinite(p.Y()) || !std::isfinite(p.Z()))
            fail("INVALID_MESH","STL contains non-finite mesh coordinates",4);
    }
    const auto& box=model.boundingBox();
    if (!box.valid || !std::isfinite(box.diagonal)) fail("INVALID_MESH","Invalid model bounds",4);
    QJsonObject metrics{{"triangle_count",model.mesh()->NbTriangles()}, {"node_count",model.mesh()->NbNodes()},
        {"source_path",request.stlPath},{"file_size_bytes",static_cast<double>(QFileInfo(request.stlPath).size())},
        {"bounding_box",QJsonObject{{"minimum",xyz(box.minimum)},{"maximum",xyz(box.maximum)},
            {"center",xyz(box.center)},{"diagonal_mm",box.diagonal}}}};
    QJsonArray artifacts,warnings;
    warnings.append("Mesh loading and bounds do not establish watertightness or reconstruction accuracy.");
    if (request.operation=="estimate_axis") {
        gp_Pnt origin; gp_Dir direction; double radius=0;
        if (!model.estimateHubAxis(&origin,&direction,&radius,&error)) fail("AXIS_ESTIMATION_FAILED",error,4);
        metrics.insert("axis_candidate",QJsonObject{{"origin",xyz(origin)},{"direction",xyz(direction)},
            {"outer_radius_mm",radius},{"confirmed",false},{"method","PCA of OCCT mesh nodes; initialization only"}});
        warnings.append("PCA_INITIALIZATION_ONLY: this candidate is not an approved reconstruction axis.");
        if (!error.isEmpty()) warnings.append(error);
    } else if (request.operation=="compute_sections" || request.operation=="process_sections") {
        if (!QDir().mkpath(request.outputDirectory)) fail("OUTPUT_WRITE_FAILED","Cannot create output_directory",5);
        run=std::make_unique<QTemporaryDir>(QDir(request.outputDirectory).filePath("run-XXXXXX"));
        if (!run->isValid()) fail("OUTPUT_WRITE_FAILED","Cannot create unique artifact directory",5);
        QJsonArray summaries;
        int index=0;
        for (double degrees:request.angles) {
            SectionRequest frame=request.frame;
            frame.radialDirection.Rotate(gp_Ax1(frame.axisPoint,frame.axisDirection),degrees*std::acos(-1.)/180.);
            auto section=SectionWorkflow::compute(model,frame);
            if (!section.success) fail("SECTION_FAILED",QStringLiteral("Angle %1: %2").arg(degrees).arg(section.error),4);
            if (section.rawSegments.empty()) warnings.append(QStringLiteral("EMPTY_SECTION: angle %1 degrees has no intersections").arg(degrees));
            const bool processed=request.operation=="process_sections";
            // Reuse ordering, conditional closure and optional bump repair only.
            if (processed && !Stlsection::cleanSectionResult(&section,request.processing))
                fail("CONTOUR_PROCESSING_FAILED",QStringLiteral("Angle %1: %2").arg(degrees).arg(section.error),4);
            const QString id=QStringLiteral("section_%1").arg(++index,3,10,QChar('0'));
            const QString path=QDir(run->path()).absoluteFilePath(id+".json");
            QJsonArray segments;
            for (const auto& segment:section.rawSegments)
                segments.append(QJsonObject{{"first",xyz(segment.first)},{"second",xyz(segment.second)},
                    {"triangle_index",segment.triangleIndex}});
            QJsonObject frameJson{{"origin",xyz(frame.axisPoint)},{"axis_direction",xyz(frame.axisDirection)},
                {"radial_direction",xyz(frame.radialDirection)},{"half_plane",true}};
            QJsonObject artifact{{"schema_version",1},{"task_id",request.taskId},{"section_id",id},
                {"run_id",QFileInfo(run->path()).fileName()},
                {"unit","mm"},{"angle_degrees",degrees},{"frame",frameJson},{"segments",segments},
                {"tolerances",QJsonObject{{"plane_mm",section.tolerances.plane},
                    {"minimum_segment_mm",section.tolerances.minimumSegment},{"duplicate_point_mm",section.tolerances.duplicatePoint}}}};
            if (processed) {
                const auto stats=processingMetrics(section);
                artifact.insert("contours",contoursJson(section.contours));
                artifact.insert("before_bump_contours",contoursJson(section.beforeBumpContours));
                artifact.insert("processing",processingJson(request.processing));
                artifact.insert("processing_metrics",stats);
                artifact.insert("closure_policy","existing_algorithm_safe_closure");
                if (stats.value("open_contour_count").toInt()>0)
                    warnings.append(QStringLiteral("OPEN_CONTOUR: angle %1 retains open contours; unsafe closure was not forced").arg(degrees));
            }
            writeJson(path,artifact);
            artifacts.append(QJsonObject{{"artifact_id",id},{"type",processed ? "processed_section" : "raw_section"},{"path",path},
                {"angle_degrees",degrees},{"segment_count",static_cast<int>(section.rawSegments.size())}});
            QJsonObject summary{{"section_id",id},{"angle_degrees",degrees},
                {"segment_count",static_cast<int>(section.rawSegments.size())}};
            if (processed) summary.insert("processing_metrics",processingMetrics(section));
            summaries.append(summary);
        }
        metrics.insert("sections",summaries);
        if (request.operation=="process_sections") {
            warnings.append("Ordered contours and conditional closure only; no self-intersection, watertightness or reconstruction validation.");
            metrics.insert("processing",processingJson(request.processing));
        } else {
            warnings.append("Raw intersections only: no contour closure, rim selection, fitting or reconstruction validation.");
        }
    }
    QJsonObject result{{"schema_version",1},{"status","success"},{"operation",request.operation},{"task_id",request.taskId},
        {"unit","mm"},{"metrics",metrics},{"artifacts",artifacts},{"warnings",warnings},{"error",QJsonValue::Null}};
    if (run) result.insert("run_id",QFileInfo(run->path()).fileName());
    return result;
}
}

int main(int argc,char** argv)
{
    QCoreApplication app(argc,argv);
    // OCCT 默认写标准输出；这里将其移到标准错误，标准输出仅保留 JSON。
    Message::DefaultMessenger()->ChangePrinters().Clear();
    Handle(Message_PrinterOStream) printer=new Message_PrinterOStream("cerr",false);
    printer->SetToColorize(false);
    Message::DefaultMessenger()->AddPrinter(printer);
    const auto args=app.arguments();
    QString requestPath,resultPath;
    QJsonObject envelope{{"schema_version",1},{"status","error"},{"operation",QJsonValue::Null},
        {"task_id",QJsonValue::Null},{"metrics",QJsonObject{}},{"artifacts",QJsonArray{}},{"warnings",QJsonArray{}}};
    bool allowResultWrite=false;
    std::unique_ptr<QTemporaryDir> run;
    try {
        if (args.size()==2 && args[1]=="--help") {
            std::puts("geometry_worker --request request.json --result result.json"); return 0;
        }
        for (int i=1;i<args.size();i+=2) {
            if (i+1>=args.size()) fail("INVALID_ARGUMENTS","Expected --request file --result file");
            if (args[i]=="--request" && requestPath.isEmpty()) requestPath=QFileInfo(args[i+1]).absoluteFilePath();
            else if (args[i]=="--result" && resultPath.isEmpty()) resultPath=QFileInfo(args[i+1]).absoluteFilePath();
            else fail("INVALID_ARGUMENTS","Unknown or duplicate command-line option");
        }
        if (requestPath.isEmpty() || resultPath.isEmpty()) fail("INVALID_ARGUMENTS","Both --request and --result are required");
        if (samePath(requestPath,resultPath)) fail("OUTPUT_PATH_CONFLICT","Result must not overwrite request",5);
        allowResultWrite=true;
        QFile file(requestPath);
        if (!file.open(QIODevice::ReadOnly)) fail("REQUEST_READ_FAILED","Cannot open request file");
        if (file.size()>1024*1024) fail("INVALID_REQUEST","Request exceeds 1 MiB");
        QJsonParseError parseError;
        const auto document=QJsonDocument::fromJson(file.readAll(),&parseError);
        if (parseError.error!=QJsonParseError::NoError || !document.isObject()) fail("INVALID_JSON","Request must be a valid JSON object");
        const auto object=document.object();
        if (object.value("operation").isString()) envelope.insert("operation",object.value("operation"));
        if (object.value("task_id").isString()) envelope.insert("task_id",object.value("task_id"));
        // 即使其他请求字段无效，也要防止结果文件覆盖输入 STL。
        if (object.value("stl_path").isString()
            && samePath(resultPath,resolved(QFileInfo(requestPath).absoluteDir(),object.value("stl_path").toString()))) {
            allowResultWrite=false;
            fail("OUTPUT_PATH_CONFLICT","Result must not overwrite source STL",5);
        }
        if(isWindowOperation(object.value("operation").toString()))
            envelope=executeWindowOperation(object,QFileInfo(requestPath).absoluteDir(),run);
        else {
            const auto request=parse(object,QFileInfo(requestPath).absoluteDir());
            envelope=execute(request,run);
        }
        writeJson(resultPath,envelope);
        if (run) run->setAutoRemove(false); // 摘要成功提交后，才保留本次截面文件。
        printJson(envelope);
        return 0;
    } catch (const Failure& e) {
        envelope.remove("run_id");
        envelope.insert("status","error"); envelope.insert("metrics",QJsonObject{}); envelope.insert("artifacts",QJsonArray{});
        envelope.insert("error",QJsonObject{{"code",e.code},{"message",e.message}});
        if (allowResultWrite) {
            try { writeJson(resultPath,envelope); }
            catch (const Failure& outputError) {
                envelope.insert("error",QJsonObject{{"code",outputError.code},{"message",outputError.message},{"original_code",e.code}});
                printJson(envelope); return outputError.exitCode;
            }
        }
        printJson(envelope); return e.exitCode;
    } catch (const Standard_Failure& e) {
        envelope.remove("run_id");
        envelope.insert("status","error"); envelope.insert("metrics",QJsonObject{}); envelope.insert("artifacts",QJsonArray{});
        envelope.insert("error",QJsonObject{{"code","OCCT_FAILURE"},{"message",QString::fromLatin1(e.what())}});
    } catch (const std::exception& e) {
        envelope.remove("run_id");
        envelope.insert("status","error"); envelope.insert("metrics",QJsonObject{}); envelope.insert("artifacts",QJsonArray{});
        envelope.insert("error",QJsonObject{{"code","INTERNAL_ERROR"},{"message",QString::fromLocal8Bit(e.what())}});
    }
    if (allowResultWrite) {
        try { writeJson(resultPath,envelope); }
        catch (const Failure& e) {
            const auto original=envelope.value("error").toObject().value("code");
            envelope.insert("error",QJsonObject{{"code",e.code},{"message",e.message},{"original_code",original}});
            printJson(envelope); return e.exitCode;
        }
    }
    printJson(envelope); return 6;
}
