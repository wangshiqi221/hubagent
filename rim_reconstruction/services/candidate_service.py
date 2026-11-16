"""从已落盘的处理截面生成轮辋候选；完整几何只保存在本地。"""
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


def validate_analysis_parameters(parameters):
    if not isinstance(parameters, dict):
        raise ValueError("分析参数必须为字典")
    allowed = {"call_directory", "consistency_tolerance_mm", "min_support_ratio",
               "tangent_angle_degrees", "offset", "limit", "disc_radius_mm", "feature_tolerance_mm"}
    if set(parameters) - allowed:
        raise ValueError("未知分析参数")
    path = parameters.get("call_directory")
    if not isinstance(path, str) or not path.strip() or not Path(path).is_absolute():
        raise ValueError("call_directory 必须是截面工具返回的绝对调用目录")
    data = copy.deepcopy(parameters)
    for name, default in (("consistency_tolerance_mm", None), ("min_support_ratio", .7),
                          ("tangent_angle_degrees", 35.), ("disc_radius_mm", None),
                          ("feature_tolerance_mm", .08)):
        value = data.get(name, default)
        if value is None and name in ("consistency_tolerance_mm", "disc_radius_mm"):
            data[name] = None
            continue
        try:
            valid = type(value) in (int, float) and math.isfinite(value) and value > 0
        except OverflowError:
            valid = False
        if not valid:
            raise ValueError(f"{name} 必须为有限正数")
        data[name] = value
    if data["min_support_ratio"] > 1 or data["tangent_angle_degrees"] >= 90:
        raise ValueError("支持率须 <=1，切向夹角须 <90 度")
    for name, default, low, high in (("offset", 0, 0, None), ("limit", 20, 1, 50)):
        value = data.get(name, default)
        if type(value) is not int or value < low or (high is not None and value > high):
            raise ValueError(f"{name} 范围无效")
        data[name] = value
    return data


def _candidate_summary(candidate):
    fields = ("candidate_id", "source_section_id", "source_contour_index", "angle_degrees",
              "closed", "complete", "semantic_complete", "fit_ready", "support_ratio",
              "supporting_angles", "source_length_coverage", "issues",
              "mean_radius_mm", "minimum_radius_mm", "maximum_radius_mm", "radial_span_mm",
              "target", "axis", "completeness_definition")
    summary = {key: copy.deepcopy(candidate[key]) for key in fields}
    summary["reconstruction"] = copy.deepcopy(candidate.get("reconstruction"))
    summary["segments"] = [{"segment_id": s["segment_id"], "closed": s["closed"],
                            "point_count": len(s["points"])} for s in candidate["segments"]]
    return summary


def analyze_rim_candidates(parameters, *, adapter=None, config_path=None, cancel_event=None):
    """读 request/result 后交给适配器复验，再调用现有多角度筛选器。

    至少一个角度；允许单条完整材料轮廓，拒绝中心与轮辋分离的截面。status=success 只表示分析完成，analysis_status 可能为
    needs_review。offset/limit 分页摘要；完整候选和点索引写入 analysis_path。
    分页重跑分析，候选 ID 仅在同一 analysis_id 内引用，不自动选择或拟合。
    取消在读取、分析前后及保存前检查；现有数值算法内部不可即时中断。
    """
    data = validate_analysis_parameters(parameters)
    check_cancelled(cancel_event)
    if adapter is not None and config_path is not None:
        raise ValueError("adapter 与 config_path 不能同时指定")
    if adapter is None:
        adapter = (CppWorkerAdapter.from_config(config_path) if config_path is not None
                   else CppWorkerAdapter.from_config())
    call = Path(data["call_directory"]).resolve()
    if call == adapter.call_root.resolve() or not call.is_relative_to(adapter.call_root.resolve()):
        raise ValueError("调用目录必须位于当前 Worker 配置的 call_directory 内")
    request = _read_json(call / "request.json", 1024 * 1024)
    result = _read_json(call / "result.json", adapter.max_result_bytes)
    if request.get("operation") != "process_sections":
        raise ValueError("必须使用 process_sections 产物，不能使用原始截面线段")
    response = WorkerResponse(result=result, call_directory=call, returncode=0)
    sections = adapter.load_sections(response, cancel_event=cancel_event)
    check_cancelled(cancel_event, call)
    # 延迟导入，避免启动 CadAgent 时强制加载 NumPy/SciPy。
    from ..geometry.base_profile_reconstructor import analyze_base_candidates
    analysis = analyze_base_candidates(sections, data)
    check_cancelled(cancel_event, call)
    analysis_id = "analysis_" + uuid.uuid4().hex
    full = {**analysis, "analysis_id": analysis_id, "task_id": request["task_id"],
            "source_run_id": result["run_id"], "source_call_directory": str(call),
            "coordinate_order": ["axial", "radius"],
            "analysis_parameters": {k: data[k] for k in (
                "consistency_tolerance_mm", "min_support_ratio", "tangent_angle_degrees",
                "disc_radius_mm", "feature_tolerance_mm")}}
    output = call / "analyses"
    if output.resolve() != output.absolute():
        raise ValueError("分析输出目录不能重定向到其他位置")
    output.mkdir(exist_ok=True)
    target = output / (analysis_id + ".json")
    encoded = json.dumps(full, ensure_ascii=False, indent=2, allow_nan=False)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=output,
                                         suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(encoded + "\n")
        check_cancelled(cancel_event, call)
        os.replace(temporary, target)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    candidates = analysis["candidates"]
    start, limit = data["offset"], data["limit"]
    diagnostics = copy.deepcopy(analysis["diagnostics"])
    gaps = diagnostics.get("radial_gap_candidates", [])
    if len(gaps) > 10:
        diagnostics["radial_gap_candidates"] = gaps[:10]
        diagnostics["omitted_radial_gap_count"] = len(gaps) - 10
    return {"schema_version": 1, "status": "success", "operation": "analyze_rim_candidates",
            "analysis_status": analysis["status"], "analysis_id": analysis_id,
            "analysis_path": str(target), "call_directory": str(call),
            "task_id": request["task_id"], "source_run_id": result["run_id"], "unit": "mm",
            "coordinate_order": ["axial", "radius"], "radial_range_mm": analysis["radial_range_mm"],
            "candidate_count": len(candidates), "offset": start, "limit": limit,
            "has_more": start + limit < len(candidates),
            "candidates": [_candidate_summary(c) for c in candidates[start:start+limit]],
            "diagnostics": diagnostics, "warnings": list(analysis["warnings"]),
            "reason": analysis["reason"], "user_confirmed": False}
