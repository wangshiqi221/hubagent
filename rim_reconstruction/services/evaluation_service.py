"""Read current CAD on main thread; evaluate immutable mesh snapshots in worker."""
import math
from pathlib import Path
import threading


def validate_parameters(parameters):
    allowed = {"document_name", "object_name", "reference_stl_path", "tolerances_mm",
               "sample_count", "mesh_deflection_mm", "seed"}
    if not isinstance(parameters, dict) or parameters.keys()-allowed:
        raise ValueError("评估参数包含未知字段")
    data = dict(parameters)
    tolerances = data.setdefault("tolerances_mm", [.2, 1., 5.])
    if (not isinstance(tolerances, list) or not 1 <= len(tolerances) <= 32
            or any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for v in tolerances)):
        raise ValueError("tolerances_mm 必须为包含1～32个有限正数的数组")
    if len(set(tolerances)) != len(tolerances):
        raise ValueError("tolerances_mm 不能包含重复阈值")
    data["tolerances_mm"] = sorted(float(v) for v in tolerances)
    for key, default in (("mesh_deflection_mm", .02),):
        value = data.setdefault(key, default)
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError(key+" 必须是有限正数")
    for key, default, low, high in (("sample_count", 5000, 100, 100000), ("seed", 2026, 0, 2**32-1)):
        value = data.setdefault(key, default)
        if type(value) is not int or not low <= value <= high:
            raise ValueError(key+" 超出整数范围")
    for key in ("document_name", "object_name", "reference_stl_path"):
        if key in data and (not isinstance(data[key], str) or not data[key].strip()):
            raise ValueError(key+" 必须是非空字符串")
    return data


def prepare_evaluation(parameters):
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("评估几何快照必须在 FreeCAD 主线程读取")
    data = validate_parameters(parameters)
    import FreeCAD as App
    import Mesh
    doc = App.getDocument(data["document_name"]) if data.get("document_name") else App.ActiveDocument
    if doc is None:
        raise ValueError("没有活动文档，请打开已重建的轮辋模型")
    if data.get("object_name"):
        obj = doc.getObject(data["object_name"])
    else:
        objects = [o for o in doc.Objects if hasattr(o, "SourceFitPath") and hasattr(o, "Shape")]
        if len(objects) != 1:
            raise ValueError("无法唯一确定重建轮辋，请指定 document_name 和 object_name")
        obj = objects[0]
    if obj is None or not hasattr(obj, "Shape"):
        raise ValueError("指定对象没有几何表面")
    # A newly created Body or edited dependency may be touched while its old
    # Shape is still valid. Recompute before reading Shape, not after rejecting it.
    try:
        doc.recompute()
    except Exception as exc:
        raise ValueError(f"文档 {doc.Name} 自动重算失败：{exc}") from exc
    dependencies = [obj] + list(obj.OutListRecursive)
    for feature in dependencies:
        state = list(feature.State)
        if any(flag in state for flag in ("Invalid", "Error", "RecomputeFailed", "Touched")):
            raise ValueError(f"自动重算后对象 {feature.Name} 仍未就绪：{state}；请检查该特征的依赖或约束")
    tip = getattr(obj, "Tip", None)
    for feature in ([obj, tip] if tip is not None else [obj]):
        shape = feature.Shape
        if shape.isNull() or not shape.isValid() or len(shape.Solids) != 1 or shape.Volume <= 0:
            raise ValueError(f"自动重算后对象 {feature.Name} 未形成有效单实体")
    source_object = obj if getattr(obj, "SourceFitPath", "") else tip
    fit_path = getattr(source_object, "SourceFitPath", "")
    reconstruction = None
    region_spec = None
    if fit_path and Path(fit_path).is_file():
        from ..adapters.cpp_worker_adapter import _read_json
        fitted_profile = _read_json(Path(fit_path), 128*1024*1024).get("profile", {})
        if fitted_profile.get("target") == "closed_center_base_disc":
            reconstruction = fitted_profile.get("reconstruction")
            if reconstruction:
                placement = obj.getGlobalPlacement()
                region_spec = {"origin": list(placement.Base),
                    "direction": list(placement.Rotation.multVec(App.Vector(1, 0, 0))),
                    "disc_radius_mm": reconstruction["effective_disc_radius_mm"]}
    shape = obj.Shape.copy()
    shape.Placement = obj.getGlobalPlacement()
    vertices, faces = shape.tessellate(data["mesh_deflection_mm"])
    reconstructed = [[list(vertices[i]) for i in face] for face in faces]
    source = data.get("reference_stl_path")
    warnings = ["距离针对当前 CAD 的离散表面，非精确 BRep 距离；最大值为采样最大值。",
                "各误差阈值内的面积比例均以整个重建表面的采样点为分母；超差点不从统计中剔除。",
                "不进行自动对齐；沿用当前模型全局坐标，参考数据按毫米解释。",
                "未自动验收；误差阈值仅为统计参数，不能代替工程验收要求。"]
    if reconstruction:
        warnings.append("轮盘区域包含主动去局部特征和中心封孔。全局距离仍保留所有样本；分区统计仅区分轮辋保留区与轮盘重建区，后者差异不能解释为拟合误差。分区面积比例的分母是各自区域样本数。")
    if source is None:
        from ..adapters.cpp_worker_adapter import _read_json
        if not fit_path:
            raise ValueError("模型缺少来源记录，请指定 reference_stl_path")
        request = _read_json(Path(fit_path).resolve().parent.parent / "request.json", 1024*1024)
        source = request["stl_path"]
        scope = "entire_source_stl_unsegmented"
        warnings.append("默认比较整个原始 STL，未自动剔除轮辐/安装盘；结果不能直接视为纯轮辋误差。可指定已分离轮辋 reference_stl_path。")
    else:
        scope = "user_supplied_reference_stl"
    path = Path(source)
    if not path.is_absolute() or not path.is_file() or path.suffix.lower() != ".stl":
        raise ValueError("参考文件必须是存在的绝对 STL 路径")
    mesh = Mesh.Mesh(str(path))
    points, facets = mesh.Topology
    reference = [[list(points[i]) for i in face] for face in facets]
    return {"parameters": data, "reference": reference, "reconstructed": reconstructed,
            "region_spec": region_spec,
            "metadata": {"document_name": doc.Name, "object_name": obj.Name,
                         "reference_stl_path": str(path.resolve()), "reference_scope": scope,
                         "source_fit_path": fit_path or None,
                         "base_reconstruction": reconstruction,
                         "recomputed_before_evaluation": True,
                         "evaluated_tip_name": tip.Name if tip is not None else None}, "warnings": warnings}


def evaluate_snapshot(snapshot, *, cancel_event=None):
    from ..geometry.surface_metrics import compare_surfaces
    data = snapshot["parameters"]
    metrics = compare_surfaces(snapshot["reference"], snapshot["reconstructed"],
        **{k: data[k] for k in ("sample_count", "tolerances_mm", "seed")},
        cancel_event=cancel_event, **({"region_spec": snapshot["region_spec"]} if snapshot.get("region_spec") else {}))
    return {"status": "success", "operation": "evaluate_revolution", "unit": "mm",
            "parameters": data, "sampling": "seeded_uniform_surface_area_monte_carlo",
            "evaluation_version": "single_direction_multi_threshold_v2",
            **snapshot["metadata"], "metrics": metrics, "warnings": snapshot["warnings"],
            "compliant": None}
