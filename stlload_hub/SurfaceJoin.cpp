#include "SurfaceJoin.h"
#include <BRep_Tool.hxx>
#include <BRep_Builder.hxx>
#include <BRepBndLib.hxx>
#include <Bnd_Box.hxx>
#include <Geom_BoundedSurface.hxx>
#include <GeomLib.hxx>
#include <GeomLib_IsPlanarSurface.hxx>
#include <Geom_Plane.hxx>
#include <BRepBuilderAPI_MakeFace.hxx>
#include <BRepBuilderAPI_MakeVertex.hxx>
#include <BRepAlgoAPI_Section.hxx>
#include <BRepAlgoAPI_Fuse.hxx>
#include <BRepPrimAPI_MakePrism.hxx>
#include <BRepAdaptor_Surface.hxx>
#include <BOPAlgo_MakerVolume.hxx>
#include <BRepExtrema_DistShapeShape.hxx>
#include <BRepFilletAPI_MakeFillet.hxx>
#include <BRepCheck_Analyzer.hxx>
#include <BRepClass3d_SolidClassifier.hxx>
#include <BRepLib.hxx>
#include <BRepGProp.hxx>
#include <GProp_GProps.hxx>
#include <TopExp.hxx>
#include <TopExp_Explorer.hxx>
#include <TopoDS.hxx>
#include <TopoDS_Compound.hxx>
#include <TopTools_IndexedDataMapOfShapeListOfShape.hxx>
#include <TopTools_IndexedMapOfShape.hxx>
#include <Standard_Failure.hxx>
#include <gp_Vec.hxx>
#include <QStringList>
#include <cmath>
#include <algorithm>
#include <set>
#include <limits>
#include <sstream>

bool isValidCuttingSolid(const TopoDS_Shape& shape, QString* error)
{
    auto fail=[&](const QString& message){if(error) *error=message;return false;};
    if(shape.IsNull() || shape.ShapeType()!=TopAbs_SOLID) return fail(QStringLiteral("结果不是单个实体。"));
    if(!BRepCheck_Analyzer(shape).IsValid()) return fail(QStringLiteral("实体拓扑检查失败。"));
    TopTools_IndexedMapOfShape shells;TopExp::MapShapes(shape,TopAbs_SHELL,shells);
    if(shells.Extent()!=1 || !BRep_Tool::IsClosed(shells(1))) return fail(QStringLiteral("实体存在开放边界或多个壳体。"));
    TopTools_IndexedDataMapOfShapeListOfShape edges;
    TopExp::MapShapesAndAncestors(shape,TopAbs_EDGE,TopAbs_FACE,edges);
    for(int i=1;i<=edges.Extent();++i) {
        if(BRep_Tool::Degenerated(TopoDS::Edge(edges.FindKey(i)))) continue;
        if(edges.FindFromIndex(i).Extent()!=2) return fail(QStringLiteral("实体含自由边或非流形边。"));
    }
    BRepClass3d_SolidClassifier classifier(shape);classifier.PerformInfinitePoint(1e-6);
    if(classifier.State()!=TopAbs_OUT) return fail(QStringLiteral("实体方向错误或无界。"));
    GProp_GProps props;BRepGProp::VolumeProperties(shape,props);
    if(!std::isfinite(props.Mass()) || props.Mass()<=1e-9) return fail(QStringLiteral("实体体积无效。"));
    if(error) error->clear();return true;
}

namespace {
TopoDS_Face extendedSupport(const TopoDS_Face& face,double length)
{
    if(length==0) return face;
    const auto source=BRep_Tool::Surface(face);
    GeomLib_IsPlanarSurface planar(source,1e-7);
    if(planar.IsPlanar()) {
        // Exact planar extension around the local face centre. All four supports
        // use the common window scale, including tiny selected corner patches.
        Bnd_Box box;BRepBndLib::Add(face,box);
        double x0,y0,z0,x1,y1,z1;box.Get(x0,y0,z0,x1,y1,z1);
        gp_Pln plane=planar.Plan();
        const gp_Pnt center((x0+x1)/2,(y0+y1)/2,(z0+z1)/2);
        gp_Vec normal(plane.Axis().Direction());
        const gp_Pnt projected=center.Translated(normal.Multiplied(-gp_Vec(plane.Location(),center).Dot(normal)));
        plane.SetLocation(projected);
        const double half=std::max(length,gp_Pnt(x0,y0,z0).Distance(gp_Pnt(x1,y1,z1))*0.5);
        return BRepBuilderAPI_MakeFace(plane,-half,half,-half,half).Face();
    }
    Handle(Geom_BoundedSurface) surface=Handle(Geom_BoundedSurface)::DownCast(source->Copy());
    if(surface.IsNull()) return {};
    // Bounded stages, rather than one long extrapolation from a tiny patch.
    for(bool inU:{true,false}) for(bool after:{false,true})
        for(int step=0;step<8;++step) GeomLib::ExtendSurfByLength(surface,length/8,1,inU,after);
    BRepBuilderAPI_MakeFace maker(surface,1e-6);
    return maker.IsDone()?maker.Face():TopoDS_Face();
}
int sourceId(const TopoDS_Shape& face,const std::array<TopTools_IndexedMapOfShape,6>& history)
{
    for(int i=0;i<6;++i) if(history[i].Contains(face)) return i;
    return -1;
}
bool followsSamples(const TopoDS_Shape& solid,const std::array<std::vector<gp_Pnt>,4>& samples,double tolerance)
{
    // Check the sharp candidate before filleting removes corner material.
    for(const auto& patch:samples) {
        const size_t count=std::min<size_t>(24,patch.size());int near=0;
        for(size_t k=0;k<count;++k) {
            const auto& point=patch[k*(patch.size()-1)/std::max<size_t>(1,count-1)];
            BRepExtrema_DistShapeShape distance(BRepBuilderAPI_MakeVertex(point).Vertex(),solid);
            if(distance.IsDone() && distance.Value()<=tolerance) ++near;
        }
        if(near<int(std::ceil(count*0.8))) return false;
    }
    return true;
}
}

bool buildWindowCuttingSolid(const std::array<TopoDS_Face,4>& faces,
    const std::array<std::vector<gp_Pnt>,4>& samples,const WindowSolidOptions& options,
    WindowSolidResult* result,QString* error)
{
    if(!result) return false;*result={};
    auto fail=[&](const QString& message){result->solid.Nullify();if(error) *error=message;return false;};
    if(!std::isfinite(options.minimumDepth) || !std::isfinite(options.maximumDepth)
       || options.maximumDepth<=options.minimumDepth || !std::isfinite(options.filletRadius)
       || options.filletRadius<0 || !std::isfinite(options.fitTolerance) || options.fitTolerance<=0)
        return fail(QStringLiteral("封口深度、拟合容差或圆角半径无效。"));
    Bnd_Box windowBox;gp_XYZ sum(0,0,0);int total=0;
    for(int i=0;i<4;++i) {
        if(faces[i].IsNull() || samples[i].empty()) return fail(QStringLiteral("请先分别拟合四个面。"));
        for(const auto& point:samples[i]) windowBox.Add(point);
        gp_XYZ patchSum(0,0,0);for(const auto& point:samples[i]) patchSum+=point.XYZ();
        sum+=patchSum/double(samples[i].size());++total;
    }
    const gp_Pnt center(sum/double(total));
    double x0,y0,z0,x1,y1,z1;windowBox.Get(x0,y0,z0,x1,y1,z1);
    const double scale=std::max(gp_Pnt(x0,y0,z0).Distance(gp_Pnt(x1,y1,z1)),1.0);
    const gp_Vec axis(options.axisDirection);
    double selectedMin=std::numeric_limits<double>::max(),selectedMax=-selectedMin;
    for(const auto& patch:samples) for(const auto& point:patch) {
        const double depth=gp_Vec(options.axisOrigin,point).Dot(axis);
        selectedMin=std::min(selectedMin,depth);selectedMax=std::max(selectedMax,depth);
    }
    // Close the data-supported depth first. Beyond it, continue the end profiles
    // by straight axial prisms, avoiding hundreds of mm of freeform extrapolation.
    const double localMinimum=std::max(options.minimumDepth,selectedMin-std::max(scale*0.05,1.0));
    const double localMaximum=std::min(options.maximumDepth,selectedMax+std::max(scale*0.05,1.0));
    if(localMaximum<=localMinimum) return fail(QStringLiteral("选区不在指定的轴向封口范围内。"));
    const double centerDepth=gp_Vec(options.axisOrigin,center).Dot(axis);
    const gp_Pnt seed=center.Translated(axis.Multiplied((localMinimum+localMaximum)*0.5-centerDepth));
    QString lastStage;int bestPairs=-1;
    try {
        for(double fraction:{0.0,0.125,0.25,0.5,0.75,1.0,1.5,2.0}) {
            const double length=scale*fraction;
            std::array<TopoDS_Face,6> supports;
            for(int i=0;i<4;++i) {
                supports[i]=extendedSupport(faces[i],length);
                if(supports[i].IsNull()) return fail(QStringLiteral("面 %1 的延伸失败。").arg(i+1));
            }
            for(int i=0;i<2;++i) {
                const double depth=i?localMaximum:localMinimum;
                const gp_Pnt origin=seed.Translated(axis.Multiplied(depth-(localMinimum+localMaximum)*0.5));
                supports[4+i]=BRepBuilderAPI_MakeFace(gp_Pln(origin,options.axisDirection),-scale*3,scale*3,-scale*3,scale*3).Face();
            }
            BRep_Builder builder;TopoDS_Compound curves;builder.MakeCompound(curves);
            std::array<std::set<int>,4> neighbors;QStringList pairs;int pairCount=0;
            for(int i=0;i<4;++i) for(int j=i+1;j<4;++j) {
                BRepAlgoAPI_Section section(supports[i],supports[j],false);
                section.Approximation(true);section.Build();
                if(!section.IsDone()) continue;
                TopTools_IndexedMapOfShape edges;TopExp::MapShapes(section.Shape(),TopAbs_EDGE,edges);
                if(!edges.Extent()) continue;
                builder.Add(curves,section.Shape());neighbors[i].insert(j);neighbors[j].insert(i);
                ++pairCount;pairs<<QStringLiteral("%1-%2").arg(i+1).arg(j+1);
            }
            if(pairCount>=bestPairs) {
                result->intersections=curves;bestPairs=pairCount;
                result->diagnostics=QStringLiteral("延伸 %1 mm；有交线的面组：%2").arg(length,0,'g',5)
                    .arg(pairs.isEmpty()?QStringLiteral("无"):pairs.join(QStringLiteral("、")));
            }
            bool enough=true;for(const auto& adjacent:neighbors) if(adjacent.size()<2) enough=false;
            if(!enough) {lastStage=QStringLiteral("交线检查：至少一个面尚未与两个相邻面相交。");continue;}
            TopTools_ListOfShape arguments;for(const auto& face:supports) arguments.Append(face);
            BOPAlgo_MakerVolume volume;volume.SetArguments(arguments);volume.SetIntersect(true);
            volume.SetNonDestructive(true);volume.SetAvoidInternalShapes(true);volume.Perform();
            if(volume.HasErrors()) {
                std::ostringstream errors;volume.DumpErrors(errors);
                lastStage=QStringLiteral("封闭体构造失败：%1").arg(QString::fromStdString(errors.str()));continue;
            }
            std::array<TopTools_IndexedMapOfShape,6> history;
            for(int i=0;i<6;++i) {
                history[i].Add(supports[i]);
                for(const auto& shape:volume.Modified(supports[i]))
                    for(TopExp_Explorer ex(shape,TopAbs_FACE);ex.More();ex.Next()) history[i].Add(ex.Current());
            }
            TopTools_IndexedMapOfShape solids;TopExp::MapShapes(volume.Shape(),TopAbs_SOLID,solids);
            std::vector<TopoDS_Solid> candidates;
            for(int i=1;i<=solids.Extent();++i) {
                auto solid=TopoDS::Solid(solids(i));
                if(!BRepLib::OrientClosedSolid(solid) || !isValidCuttingSolid(solid)) continue;
                std::set<int> contributions;
                for(TopExp_Explorer ex(solid,TopAbs_FACE);ex.More();ex.Next()) contributions.insert(sourceId(ex.Current(),history));
                bool all=true;for(int id=0;id<6;++id) if(!contributions.count(id)) all=false;
                if(!all || contributions.count(-1)) continue;
                if(!followsSamples(solid,samples,options.fitTolerance*2)) continue;
                candidates.push_back(solid);
            }
            if(candidates.empty()) {lastStage=QStringLiteral("裁剪/封口检查：未找到同时由四个侧面和两个端盖围成的有效实体。");continue;}
            if(candidates.size()>1) {
                std::vector<TopoDS_Solid> inside;
                for(const auto& candidate:candidates) {
                    BRepClass3d_SolidClassifier classify(candidate,seed,1e-6);
                    if(classify.State()==TopAbs_IN) inside.push_back(candidate);
                }
                if(inside.size()!=1) return fail(QStringLiteral("存在多个可能的封闭区域，无法唯一确定窗口内部。请检查选区。"));
                candidates=inside;
            }
            TopoDS_Solid sharp=candidates.front();
            result->extensionLength=length;result->solid=sharp;
            if(options.filletRadius>0) {
                TopTools_IndexedDataMapOfShapeListOfShape edgeFaces;
                TopExp::MapShapesAndAncestors(sharp,TopAbs_EDGE,TopAbs_FACE,edgeFaces);
                BRepFilletAPI_MakeFillet fillet(sharp);int count=0;
                for(int i=1;i<=edgeFaces.Extent();++i) {
                    std::set<int> owners;
                    for(const auto& face:edgeFaces.FindFromIndex(i)) owners.insert(sourceId(face,history));
                    if(owners.size()!=2 || *owners.begin()<0 || *owners.rbegin()>=4) continue;
                    fillet.Add(options.filletRadius,TopoDS::Edge(edgeFaces.FindKey(i)));++count;
                }
                try {
                    if(count>=4) fillet.Build();
                    TopTools_IndexedMapOfShape roundedSolids;
                    if(count>=4 && fillet.IsDone()) TopExp::MapShapes(fillet.Shape(),TopAbs_SOLID,roundedSolids);
                    QString validation;
                    if(roundedSolids.Extent()==1 && isValidCuttingSolid(roundedSolids(1),&validation)) {
                        result->solid=TopoDS::Solid(roundedSolids(1));result->filletApplied=true;
                    } else result->warning=QStringLiteral("封闭实体已生成，但当前半径无法完成全部交接圆角；已保留无圆角实体。可减小半径重试。%1")
                        .arg(validation.isEmpty()?QString():QStringLiteral("\n验证结果：")+validation);
                } catch(const Standard_Failure&) {
                    result->warning=QStringLiteral("封闭实体已生成，圆角求解失败；已保留无圆角实体。");
                }
            }
            // Find the planar axial end caps after rounding, and continue their
            // exact profiles until both end caps are outside the target model.
            std::vector<std::pair<TopoDS_Face,double>> ends;
            for(TopExp_Explorer ex(result->solid,TopAbs_FACE);ex.More();ex.Next()) {
                const auto face=TopoDS::Face(ex.Current());BRepAdaptor_Surface surface(face);
                if(surface.GetType()!=GeomAbs_Plane) continue;
                const auto plane=surface.Plane();
                if(std::abs(plane.Axis().Direction().Dot(options.axisDirection))<1-1e-7) continue;
                const double depth=gp_Vec(options.axisOrigin,plane.Location()).Dot(axis);
                if(std::abs(depth-localMinimum)<1e-4 && localMinimum>options.minimumDepth+1e-6)
                    ends.emplace_back(face,options.minimumDepth-localMinimum);
                if(std::abs(depth-localMaximum)<1e-4 && localMaximum<options.maximumDepth-1e-6)
                    ends.emplace_back(face,options.maximumDepth-localMaximum);
            }
            for(const auto& end:ends) {
                BRepPrimAPI_MakePrism prism(end.first,axis.Multiplied(end.second));
                if(!prism.IsDone()) return fail(QStringLiteral("端盖轮廓轴向延伸失败。"));
                BRepAlgoAPI_Fuse fuse(result->solid,prism.Shape());fuse.Build();
                if(!fuse.IsDone()) return fail(QStringLiteral("端部切削延伸体合并失败。"));
                TopTools_IndexedMapOfShape combined;TopExp::MapShapes(fuse.Shape(),TopAbs_SOLID,combined);
                if(combined.Extent()!=1) return fail(QStringLiteral("端部延伸后没有形成单一实体。"));
                auto solid=TopoDS::Solid(combined(1));
                if(!BRepLib::OrientClosedSolid(solid) || !isValidCuttingSolid(solid))
                    return fail(QStringLiteral("端部延伸后的实体验证失败。"));
                result->solid=solid;
            }
            bool frontCap=false,backCap=false;
            for(TopExp_Explorer ex(result->solid,TopAbs_FACE);ex.More();ex.Next()) {
                BRepAdaptor_Surface surface(TopoDS::Face(ex.Current()));
                if(surface.GetType()!=GeomAbs_Plane) continue;
                const auto plane=surface.Plane();
                if(std::abs(plane.Axis().Direction().Dot(options.axisDirection))<1-1e-7) continue;
                const double depth=gp_Vec(options.axisOrigin,plane.Location()).Dot(axis);
                frontCap=frontCap || std::abs(depth-options.minimumDepth)<1e-4;
                backCap=backCap || std::abs(depth-options.maximumDepth)<1e-4;
            }
            if(!frontCap || !backCap) return fail(QStringLiteral("最终实体的端盖未到达指定模型两侧，不能作为贯穿切削工具。"));
            if(error) error->clear();return true;
        }
        return fail(lastStage+QStringLiteral("\n")+result->diagnostics+
                    QStringLiteral("\n已保留交线预览与原四个面；未生成可导出的切削实体。"));
    } catch(const Standard_Failure& failure) {
        result->solid.Nullify();
        return fail(QStringLiteral("几何求解异常：%1\n%2").arg(QString::fromUtf8(failure.GetMessageString()),result->diagnostics));
    }
}
