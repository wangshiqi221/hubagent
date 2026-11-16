"""FreeCAD 主线程建模服务：仅消费本地成功拟合产物，不接受模型几何。"""
from pathlib import Path
import threading

from ..adapters.cpp_worker_adapter import CppWorkerAdapter, _read_json


def build_rim_revolution(parameters, *, adapter=None):
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("FreeCAD 建模必须在主线程执行")
    if not isinstance(parameters, dict) or set(parameters) != {"fit_path"}:
        raise ValueError("仅接受 fit_path，不能提交坐标、脚本或修改回转轴")
    raw = parameters["fit_path"]
    if not isinstance(raw, str) or not Path(raw).is_absolute():
        raise ValueError("fit_path 必须是 rim_fit_profile 返回的绝对 result_path")
    adapter = adapter if adapter is not None else CppWorkerAdapter.from_config()
    path = Path(raw).resolve()
    call = path.parent.parent
    if (path.parent.name != "fits" or call == adapter.call_root.resolve()
            or not call.is_relative_to(adapter.call_root.resolve())):
        raise ValueError("拟合文件不在当前 Worker 调用目录内")
    fit = _read_json(path, adapter.max_artifact_bytes)
    if path.name != fit.get("fit_id", "") + ".json" or fit.get("success") is not True:
        raise ValueError("拟合身份或成功状态无效")
    analysis_path = Path(fit["analysis_path"]).resolve()
    if analysis_path.parent != call / "analyses":
        raise ValueError("拟合与分析不属于同一次截面调用")
    analysis = _read_json(analysis_path, adapter.max_artifact_bytes)
    request = _read_json(call / "request.json", 1024 * 1024)
    if (fit.get("analysis_id") != analysis.get("analysis_id")
            or analysis.get("task_id") != request.get("task_id")):
        raise ValueError("拟合来源身份不一致")
    from ..cad.freecad_rim_builder import build_rim_from_fit
    result = build_rim_from_fit(path)
    return {**result, "operation": "build_rim_revolution", "solid_count": 1,
            "fit_id": fit["fit_id"], "analysis_id": fit["analysis_id"],
            "feature_tree": ["PartDesign::Body", "Sketcher::SketchObject", "PartDesign::Revolution"]}
