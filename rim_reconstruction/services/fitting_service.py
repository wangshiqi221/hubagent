"""模型选择候选后直接复用 Python 拟合；完整几何与曲线参数仅写入本地。"""
from __future__ import annotations

import copy
import json
import math
import os
from pathlib import Path
import tempfile
import uuid

from ..adapters.cpp_worker_adapter import (
    CppWorkerAdapter, WorkerResponse, _read_json, check_cancelled,
)


def validate_fitting_parameters(parameters):
    if not isinstance(parameters, dict):
        raise ValueError("拟合参数必须为 JSON 对象")
    required = {"analysis_path", "candidate_id", "segment_ids"}
    allowed = required | {"tolerance_mm", "max_control_points", "max_iterations", "sample_count"}
    if required - parameters.keys() or parameters.keys() - allowed:
        raise ValueError("缺少拟合必填字段或包含未知参数；不得提交几何点列")
    data = copy.deepcopy(parameters)
    if not isinstance(data["analysis_path"], str) or not Path(data["analysis_path"]).is_absolute():
        raise ValueError("analysis_path 必须为分析工具返回的绝对路径")
    if not isinstance(data["candidate_id"], str) or not data["candidate_id"]:
        raise ValueError("必须明确指定 candidate_id，不自动选择第一候选")
    ids = data["segment_ids"]
    if (not isinstance(ids, list) or not ids or
            any(not isinstance(x, str) or not x for x in ids) or len(set(ids)) != len(ids)):
        raise ValueError("segment_ids 必须是非空且不重复的片段编号列表")
    tolerance = data.setdefault("tolerance_mm", .08)
    if type(tolerance) not in (int, float) or not math.isfinite(tolerance) or tolerance <= 0:
        raise ValueError("tolerance_mm 必须为有限正数")
    for key, default, low, high in (("max_control_points", 256, 4, 2048),
                                    ("max_iterations", 12, 2, 64),
                                    ("sample_count", 500, 2, 10000)):
        value = data.setdefault(key, default)
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"{key} 必须为 {low}～{high} 的整数")
    return data


def fit_selected_profile(parameters, *, adapter=None, config_path=None, cancel_event=None):
    """直接拟合模型指定片段，无人工确认；不平均点列、不跨缺口拼接。

    再次校验原 Worker 产物和每段源索引，并重新计算二维点，拒绝被改动的
    候选几何。取消在各段前后检查，SciPy 单次求解不能立即中断。
    """
    data = validate_fitting_parameters(parameters)
    check_cancelled(cancel_event)
    if adapter is not None and config_path is not None:
        raise ValueError("adapter 与 config_path 不能同时指定")
    if adapter is None:
        adapter = (CppWorkerAdapter.from_config(config_path) if config_path is not None
                   else CppWorkerAdapter.from_config())
    path = Path(data["analysis_path"]).resolve()
    call = path.parent.parent
    if (path.parent.name != "analyses" or call == adapter.call_root.resolve()
            or not call.is_relative_to(adapter.call_root.resolve())):
        raise ValueError("分析文件必须位于当前 Worker 调用目录的 analyses 内")
    analysis = _read_json(path, adapter.max_artifact_bytes)
    if (analysis.get("status") != "candidates_found" or analysis.get("unit") != "mm"
            or analysis.get("coordinate_order") != ["axial", "radius"]
            or path.name != analysis.get("analysis_id", "") + ".json"
            or Path(analysis["source_call_directory"]).resolve() != call):
        raise ValueError("分析身份、状态或坐标系无效")
    request = _read_json(call / "request.json", 1024 * 1024)
    result = _read_json(call / "result.json", adapter.max_result_bytes)
    if (request.get("operation") != "process_sections"
            or analysis.get("task_id") != request.get("task_id")
            or analysis.get("source_run_id") != result.get("run_id")):
        raise ValueError("分析与源截面调用不一致")
    sections = adapter.load_sections(WorkerResponse(result, call, 0), cancel_event=cancel_event)
    candidates = [c for c in analysis["candidates"] if c["candidate_id"] == data["candidate_id"]]
    if len(candidates) != 1:
        raise ValueError("candidate_id 不存在或不唯一")
    candidate = candidates[0]
    from ..geometry.base_profile_reconstructor import ALGORITHM, TARGET, analyze_base_candidates
    rebuilt = analysis.get("reconstruction_version") is not None
    if rebuilt:
        if analysis["reconstruction_version"] != ALGORITHM or candidate.get("target") != TARGET:
            raise ValueError("未知主体重建版本或目标，请重新分析")
        # Rebuild from verified Worker sections. Neither saved generated points
        # nor their provenance labels alone are accepted as evidence.
        replay = analyze_base_candidates(sections, analysis["analysis_parameters"])
        expected = next((c for c in replay["candidates"] if c["candidate_id"] == data["candidate_id"]), None)
        if expected != candidate:
            raise ValueError("重建候选与源数据不一致，请重新分析")
    elif candidate.get("target") == TARGET or any(s.get("source_kind") for s in candidate["segments"]):
        raise ValueError("重建片段缺少版本化来源，不能按原始轮廓拟合")
    available = {s["segment_id"]: s for s in candidate["segments"]}
    if set(data["segment_ids"]) - available.keys():
        raise ValueError("segment_ids 不属于当前候选")
    if candidate.get("target") in ("continuous_wheel_disc", TARGET) and set(data["segment_ids"]) != set(available):
        raise ValueError("连续轮盘必须选择完整材料截面的全部片段")
    # 延迟导入科学计算依赖，不影响工作台启动。
    import numpy as np
    from ..geometry.bspline_fitter import fit_profile

    selected = []
    # 保持分析中的源片段顺序，不使用模型提交列表重新排列几何。
    for segment in candidate["segments"]:
        if segment["segment_id"] not in data["segment_ids"]:
            continue
        check_cancelled(cancel_event, call)
        if rebuilt:
            selected.append({**copy.deepcopy(segment), "points": copy.deepcopy(segment["points_ar"])})
            continue
        source = [s for s in sections if s["section_id"] == segment["source_section_id"]]
        if len(source) != 1 or segment["source_section_id"] != candidate["source_section_id"]:
            raise ValueError("片段不属于候选源截面")
        section = source[0]
        contours = [c for c in section["contours"]
                    if c["contour_index"] == segment["source_contour_index"]]
        if len(contours) != 1 or segment["source_contour_index"] != candidate["source_contour_index"]:
            raise ValueError("片段不属于候选源轮廓")
        contour = contours[0]
        ids = segment["source_point_indices"]
        n = len(contour["points"])
        if (not isinstance(ids, list) or len(ids) < 2 or len(set(ids)) != len(ids)
                or any(type(i) is not int or not 0 <= i < n for i in ids)):
            raise ValueError("源索引无效或片段不足两个点")
        if any(b != (a + 1) % n or (b == 0 and not contour["closed"])
               for a, b in zip(ids, ids[1:])):
            raise ValueError("片段索引不连续，不能跨缺口拟合")
        if type(segment["closed"]) is not bool or (segment["closed"] and
                (not contour["closed"] or len(ids) != n)):
            raise ValueError("闭合状态与源轮廓不一致")
        xyz = [contour["points"][i] for i in ids]
        frame = section["frame"]
        delta = np.asarray(xyz, dtype=float) - np.asarray(frame["origin"])
        ar = np.c_[delta @ np.asarray(frame["axis_direction"]),
                   delta @ np.asarray(frame["radial_direction"])]
        saved_ar = np.asarray(segment["points_ar"], dtype=float)
        if (segment["points"] != xyz or saved_ar.shape != ar.shape
                or not np.allclose(saved_ar, ar, atol=1e-8, rtol=0)):
            raise ValueError("候选点列与源数据不一致，请重新分析")
        selected.append({**segment, "points": ar.tolist()})
    profile_id = "profile_" + uuid.uuid4().hex
    profile = {"profile_id": profile_id, "unit": "mm", "coordinate_order": ["axial", "radius"],
               "target": candidate.get("target"), "axis": candidate.get("axis"),
               "reconstruction": copy.deepcopy(candidate.get("reconstruction")),
               "complete": candidate["complete"] and len(selected) == len(candidate["segments"]),
               "segments": selected}
    items, warnings = [], ["仅对所选片段拟合；未验证尖角分段、段间连续性、自交或回转实体。"]
    for segment in selected:
        check_cancelled(cancel_event, call)
        fitted = fit_profile({**profile, "segments": [segment]},
            **{k: data[k] for k in ("tolerance_mm", "max_control_points", "max_iterations", "sample_count")})
        items.extend(fitted["items"])
        warnings.extend(fitted.get("warnings", []))
    for i, item in enumerate(items):
        item["item_index"] = i
    check_cancelled(cancel_event, call)
    success = bool(items) and all(i["fit"]["success"] and i["fit"]["compliant"] for i in items)
    fit_id = "fit_" + uuid.uuid4().hex
    full = {"schema_version": 1, "fit_id": fit_id, "profile": profile,
            "analysis_id": analysis["analysis_id"], "analysis_path": str(path),
            "candidate_id": data["candidate_id"], "parameters": data,
            "success": success, "items": items, "warnings": list(dict.fromkeys(warnings))}
    output = call / "fits"
    if output.resolve() != output.absolute():
        raise ValueError("拟合输出目录不能重定向")
    output.mkdir(exist_ok=True)
    target = output / (fit_id + ".json")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=output,
                                         suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(full, stream, ensure_ascii=False, allow_nan=False, indent=2)
        check_cancelled(cancel_event, call)
        os.replace(temporary, target)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return {"schema_version": 1, "status": "success" if success else "error",
            "operation": "fit_profile", "fit_id": fit_id, "profile_id": profile_id,
            "analysis_id": analysis["analysis_id"], "candidate_id": data["candidate_id"],
            "unit": "mm", "coordinate_order": ["axial", "radius"],
            "fit_success": success, "compliant": success, "result_path": str(target),
            "warnings": full["warnings"],
            "error": None if success else {"code": "FIT_FAILED", "message": "部分片段拟合未通过"},
            "items": [{"segment_id": i["source"].get("segment_id"),
                "success": i["fit"]["success"], "compliant": i["fit"]["compliant"],
                "maximum_deviation_mm": i["fit"].get("maximum_deviation_mm"),
                "control_point_count": (i["fit"].get("bspline") or {}).get("control_point_count"),
                "error": i["fit"].get("error")} for i in items]}
