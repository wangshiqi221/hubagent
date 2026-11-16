"""轮辋截面用例：参数校验、批量请求和摘要；不依赖 Qt/FreeCAD。

参数错误抛 ValueError；WorkerError 原样向上传递（含 code/call_directory）。
返回成功只代表截面提取成功，不代表轮辋识别、拟合或重建成功。
完整点列保留在适配器的截面产物中，可用 load_sections() 读取。
"""
from __future__ import annotations

import copy
import math
from pathlib import Path

from ..adapters.cpp_worker_adapter import CppWorkerAdapter, PROCESSING_DEFAULTS, check_cancelled


def _finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _vector(value, name):
    if not isinstance(value, list) or len(value) != 3 or not all(_finite(x) for x in value):
        raise ValueError(f"{name} 必须是三个有限数值的列表")
    return list(value)


def _direction(value, name):
    vector = _vector(value, name)
    length = math.hypot(*vector)
    if not math.isfinite(length) or length <= 1e-12:
        raise ValueError(f"{name} 必须是非零方向")
    return [x / length for x in vector]


def validate_section_parameters(parameters: dict) -> dict:
    """验证并复制输入；方向归一化，不猜测轴线、不投影错误的径向方向。

    必填：task_id/stl_path/unit/axis_origin/axis_direction/radial_reference/
    angles_degrees。可选 processing；路径相对 Worker 配置的 base_directory。
    """
    required = {"task_id", "stl_path", "unit", "axis_origin", "axis_direction",
                "radial_reference", "angles_degrees"}
    if not isinstance(parameters, dict):
        raise ValueError("截面参数必须是字典")
    missing = required - parameters.keys()
    unknown = parameters.keys() - required - {"processing"}
    if missing or unknown:
        raise ValueError(f"缺少参数: {sorted(missing)}；未知参数: {sorted(unknown)}")
    data = copy.deepcopy(parameters)
    for key in ("task_id", "stl_path"):
        if not isinstance(data[key], str) or not data[key].strip():
            raise ValueError(f"{key} 必须是非空字符串")
    if data["unit"] != "mm":
        raise ValueError("当前仅支持显式毫米单位 mm，不自动缩放 STL")
    data["axis_origin"] = _vector(data["axis_origin"], "axis_origin")
    axis = _direction(data["axis_direction"], "axis_direction")
    radial = _direction(data["radial_reference"], "radial_reference")
    if abs(sum(a * r for a, r in zip(axis, radial))) > 1e-8:
        raise ValueError("radial_reference 必须与 axis_direction 正交")
    data["axis_direction"], data["radial_reference"] = axis, radial
    angles = data["angles_degrees"]
    if (not isinstance(angles, list) or not 1 <= len(angles) <= 360
            or not all(_finite(a) and 0 <= a < 360 for a in angles)
            or len(set(angles)) != len(angles)):
        raise ValueError("angles_degrees 必须包含 1～360 个不重复的 [0,360) 数值")
    supplied = data.get("processing", {})
    if not isinstance(supplied, dict) or supplied.keys() - PROCESSING_DEFAULTS.keys():
        raise ValueError("processing 必须为字典，且只能包含受支持的清洗参数")
    processing = {**PROCESSING_DEFAULTS, **supplied}
    for key, value in processing.items():
        if key == "enable_micro_bump_cleaning":
            if type(value) is not bool:
                raise ValueError(f"{key} 必须是布尔值")
        elif not _finite(value) or (value < 0 if key == "duplicate_tolerance_mm" else value <= 0):
            raise ValueError(f"{key} 数值范围无效")
    if (processing["bump_support_search_length_mm"] < processing["bump_reference_length_mm"]
            or processing["bump_tangent_angle_degrees"] >= 90):
        raise ValueError("清洗参数要求搜索长度不小于参考长度、切向角小于 90 度")
    data["processing"] = processing
    return data


def build_processed_sections_request(parameters: dict) -> dict:
    """构造现有 C++ v1 请求；所有角度放在同一个请求中。"""
    data = validate_section_parameters(parameters)
    return {
        "schema_version": 1, "operation": "process_sections",
        "task_id": data["task_id"], "stl_path": data["stl_path"], "unit": "mm",
        "axis": {"origin": data["axis_origin"], "direction": data["axis_direction"]},
        "radial_reference": data["radial_reference"],
        "angles_degrees": data["angles_degrees"], "processing": data["processing"],
    }


def process_sections(parameters: dict, *, adapter: CppWorkerAdapter | None = None,
                     config_path: str | Path | None = None, cancel_event=None) -> dict:
    """一次调用取得多个处理截面，返回 JSON 可序列化摘要，不返回完整点列。

    adapter 用于注入测试/自定义运行目录；config_path 用于选择 Worker 配置。
    两者不能同时指定。不捕获 WorkerError，由工具处理器负责转换错误信封。
    本函数是同步函数，Qt 应将它放到后台执行；cancel_event 用于协作取消。
    """
    check_cancelled(cancel_event)
    request = build_processed_sections_request(parameters)
    if adapter is not None and config_path is not None:
        raise ValueError("adapter 与 config_path 不能同时指定")
    if adapter is None:
        adapter = (CppWorkerAdapter.from_config(config_path) if config_path is not None
                   else CppWorkerAdapter.from_config())
    response = (adapter.run(request, cancel_event=cancel_event) if cancel_event is not None
                else adapter.run(request))
    sections = (adapter.load_sections(response, cancel_event=cancel_event) if cancel_event is not None
                else adapter.load_sections(response))
    summaries = []
    warnings = list(response.result["warnings"])
    for section in sections:
        check_cancelled(cancel_event, response.call_directory)
        contours = section["contours"]
        closed = sum(contour["closed"] for contour in contours)
        summaries.append({
            "section_id": section["section_id"], "angle_degrees": section["angle_degrees"],
            "contour_count": len(contours), "closed_contour_count": closed,
            "open_contour_count": len(contours) - closed,
            "point_count": sum(contour["point_count"] for contour in contours),
        })
        if not contours:
            warnings.append(f"截面 {section['section_id']} 没有有序轮廓，请检查轴线和截面角度")
    check_cancelled(cancel_event, response.call_directory)
    return {
        "schema_version": 1, "status": "success", "operation": "process_sections",
        "task_id": response.result["task_id"], "run_id": response.result["run_id"],
        "unit": "mm", "call_directory": str(response.call_directory),
        "request_path": str(response.call_directory / "request.json"),
        "result_path": str(response.call_directory / "result.json"),
        "section_artifacts": copy.deepcopy(response.result["artifacts"]),
        "sections": summaries, "warnings": warnings,
    }


def repair_sections(parameters, *, adapter=None, config_path=None, cancel_event=None):
    """Replay a verified section request through C++ closure; preserve source artifacts.

    This re-extracts from the source STL with the same frame/angles, rather than
    accepting invented point arrays. Unrepairable chains remain explicitly open.
    """
    from ..adapters.cpp_worker_adapter import WorkerResponse, _read_json
    if not isinstance(parameters, dict) or set(parameters) != {"call_directory"}:
        raise ValueError("repair requires only call_directory")
    if not isinstance(parameters["call_directory"], str) or not Path(parameters["call_directory"]).is_absolute():
        raise ValueError("call_directory must be absolute")
    if adapter is not None and config_path is not None:
        raise ValueError("adapter and config_path are mutually exclusive")
    adapter = adapter or (CppWorkerAdapter.from_config(config_path) if config_path else CppWorkerAdapter.from_config())
    call = Path(parameters["call_directory"]).resolve()
    if call == adapter.call_root.resolve() or not call.is_relative_to(adapter.call_root.resolve()):
        raise ValueError("call_directory outside configured call root")
    check_cancelled(cancel_event, call)
    request = _read_json(call / "request.json", 1024 * 1024)
    result = _read_json(call / "result.json", adapter.max_result_bytes)
    if request.get("operation") not in ("compute_sections", "process_sections"):
        raise ValueError("source must be a section operation")
    sections = adapter.load_sections(WorkerResponse(result, call, 0), cancel_event=cancel_event)
    output = process_sections({"task_id": request["task_id"], "stl_path": request["stl_path"],
        "unit": request["unit"], "axis_origin": request["axis"]["origin"],
        "axis_direction": request["axis"]["direction"], "radial_reference": request["radial_reference"],
        "angles_degrees": request["angles_degrees"], "processing": request.get("processing", {})},
        adapter=adapter, cancel_event=cancel_event)
    output["repair_source_call_directory"] = str(call)
    output["repair_method"] = "cpp_cleanSectionResult_reextract_same_frame"
    output["repair_before"] = [{"section_id": s["section_id"],
        "open_contour_count": sum(not c["closed"] for c in s.get("contours", []))} for s in sections]
    output["repair_status"] = "closed" if all(s["contour_count"] and not s["open_contour_count"] for s in output["sections"]) else "needs_review"
    return output
