"""CadAgent 工具边界：接收 JSON 字符串，返回 JSON 摘要字符串。

本模块不注册工具、不加载 FreeCAD。process_sections 为同步服务，
Qt 接入时应在后台执行；可通过内部 cancel_event 协作取消。
"""
from __future__ import annotations

import json

from .adapters.cpp_worker_adapter import WorkerError, WorkerCancelledError, check_cancelled
from .services.section_service import process_sections


def _reject_constant(value):
    raise ValueError(f"不允许非有限 JSON 数值: {value}")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"重复的 JSON 参数: {key}")
        result[key] = value
    return result


def rim_process_sections(args_json: str, *, cancel_event=None) -> str:
    """参数契约与 section_service.process_sections 完全一致。

    错误码区分 JSON/参数错误、Worker 错误和未预期执行错误。
    Worker 的业务码、调用目录、退出码得以保留；不回传完整 worker_result。
    """
    try:
        if not isinstance(args_json, str):
            raise ValueError("工具参数必须是 JSON 字符串")
        parameters = json.loads(args_json, parse_constant=_reject_constant,
                                object_pairs_hook=_unique_object)
        if not isinstance(parameters, dict):
            raise ValueError("工具参数必须是 JSON 对象")
    except (ValueError, TypeError, RecursionError) as exc:
        result = {"status": "error", "error": {
            "code": "INVALID_ARGUMENTS", "message": str(exc)}}
    else:
        try:
            check_cancelled(cancel_event)
            result = (process_sections(parameters, cancel_event=cancel_event) if cancel_event is not None
                      else process_sections(parameters))
            check_cancelled(cancel_event, result.get("call_directory"))
        except WorkerError as exc:
            result = {
                "status": "cancelled" if isinstance(exc, WorkerCancelledError) else "error",
                "error": {"code": exc.code, "message": str(exc)},
                "call_directory": str(exc.call_directory) if exc.call_directory else None,
                "returncode": exc.returncode,
            }
        except ValueError as exc:
            result = {"status": "error", "error": {
                "code": "INVALID_ARGUMENTS", "message": str(exc)}}
        except Exception as exc:
            result = {"status": "error", "error": {
                "code": "RIM_SECTION_FAILED", "message": str(exc)}}
    return json.dumps(result, ensure_ascii=False, allow_nan=False)


def rim_analyze_candidates(args_json: str, *, cancel_event=None) -> str:
    """根据已有调用目录筛选候选，只回传摘要；不接受模型提供的点列。"""
    try:
        if not isinstance(args_json, str):
            raise ValueError("工具参数必须是 JSON 字符串")
        parameters = json.loads(args_json, parse_constant=_reject_constant,
                                object_pairs_hook=_unique_object)
        from .services.candidate_service import analyze_rim_candidates
        result = analyze_rim_candidates(parameters, cancel_event=cancel_event)
    except WorkerError as exc:
        result = {"status": "cancelled" if isinstance(exc, WorkerCancelledError) else "error",
                  "error": {"code": exc.code, "message": str(exc)},
                  "call_directory": str(exc.call_directory) if exc.call_directory else None,
                  "returncode": exc.returncode}
    except (ValueError, TypeError, RecursionError) as exc:
        result = {"status": "error", "error": {"code": "INVALID_ARGUMENTS", "message": str(exc)}}
    except ImportError as exc:
        result = {"status": "error", "error": {"code": "ANALYSIS_DEPENDENCY_MISSING", "message": str(exc)}}
    except Exception as exc:
        result = {"status": "error", "error": {"code": "RIM_ANALYSIS_FAILED", "message": str(exc)}}
    return json.dumps(result, ensure_ascii=False, allow_nan=False)


def rim_fit_profile(args_json: str, *, cancel_event=None) -> str:
    """直接拟合模型选定的候选片段，不要求人工确认，不接受点列。"""
    try:
        if not isinstance(args_json, str):
            raise ValueError("工具参数必须是 JSON 字符串")
        parameters = json.loads(args_json, parse_constant=_reject_constant,
                                object_pairs_hook=_unique_object)
        from .services.fitting_service import fit_selected_profile
        result = fit_selected_profile(parameters, cancel_event=cancel_event)
    except WorkerError as exc:
        result = {"status": "cancelled" if isinstance(exc, WorkerCancelledError) else "error",
                  "error": {"code": exc.code, "message": str(exc)},
                  "call_directory": str(exc.call_directory) if exc.call_directory else None}
    except (ValueError, TypeError, RecursionError) as exc:
        result = {"status": "error", "error": {"code": "INVALID_ARGUMENTS", "message": str(exc)}}
    except ImportError as exc:
        result = {"status": "error", "error": {"code": "FIT_DEPENDENCY_MISSING", "message": str(exc)}}
    except Exception as exc:
        result = {"status": "error", "error": {"code": "RIM_FIT_FAILED", "message": str(exc)}}
    return json.dumps(result, ensure_ascii=False, allow_nan=False)


def rim_build_revolution(args_json: str) -> str:
    """主线程工具：读取已拟合参数，创建原生草图和回转实体；不在后台执行。"""
    try:
        if not isinstance(args_json, str):
            raise ValueError("工具参数必须是 JSON 字符串")
        parameters = json.loads(args_json, parse_constant=_reject_constant,
                                object_pairs_hook=_unique_object)
        from .services.cad_service import build_rim_revolution
        result = build_rim_revolution(parameters)
    except (ValueError, TypeError, RecursionError) as exc:
        result = {"status": "error", "error": {"code": "CAD_INPUT_INVALID", "message": str(exc)}}
    except ImportError as exc:
        result = {"status": "error", "error": {"code": "FREECAD_UNAVAILABLE", "message": str(exc)}}
    except Exception as exc:
        result = {"status": "error", "error": {"code": "CAD_BUILD_FAILED", "message": str(exc)}}
    return json.dumps(result, ensure_ascii=False, allow_nan=False)


def rim_evaluate_revolution(args_json: str, *, snapshot=None, cancel_event=None) -> str:
    """On-demand only. UI supplies a main-thread snapshot to the worker."""
    try:
        parameters = json.loads(args_json, parse_constant=_reject_constant,
                                object_pairs_hook=_unique_object)
        from .services.evaluation_service import prepare_evaluation, evaluate_snapshot, validate_parameters
        data = validate_parameters(parameters)
        check_cancelled(cancel_event)
        if snapshot is None:
            snapshot = prepare_evaluation(data)
        elif snapshot["parameters"] != data:
            raise ValueError("几何快照与当前评估参数不一致")
        result = evaluate_snapshot(snapshot, cancel_event=cancel_event)
    except WorkerError as exc:
        result = {"status": "cancelled" if isinstance(exc, WorkerCancelledError) else "error",
                  "error": {"code": exc.code, "message": str(exc)}}
    except (ValueError, TypeError, RecursionError) as exc:
        result = {"status": "error", "error": {"code": "EVALUATION_INPUT_INVALID", "message": str(exc)}}
    except Exception as exc:
        result = {"status": "error", "error": {"code": "EVALUATION_FAILED", "message": str(exc)}}
    return json.dumps(result, ensure_ascii=False, allow_nan=False)


def _model_tool(args_json, operation, cancel_event):
    """统一模型解析边界，轴线只能由 C++ 返回，不能由模型填写。"""
    try:
        if not isinstance(args_json, str):
            raise ValueError("工具参数必须为 JSON 字符串")
        parameters = json.loads(args_json, parse_constant=_reject_constant, object_pairs_hook=_unique_object)
        from .services import model_service
        result = getattr(model_service, operation)(parameters, cancel_event=cancel_event)
    except WorkerError as exc:
        result = {"status": "cancelled" if isinstance(exc, WorkerCancelledError) else "error",
            "error": {"code": exc.code, "message": str(exc)},
            "call_directory": str(exc.call_directory) if exc.call_directory else None}
    except (ValueError, TypeError, RecursionError) as exc:
        result = {"status": "error", "error": {"code": "INVALID_ARGUMENTS", "message": str(exc)}}
    except Exception as exc:
        result = {"status": "error", "error": {"code": "MODEL_ANALYSIS_FAILED", "message": str(exc)}}
    return json.dumps(result, ensure_ascii=False, allow_nan=False)


def rim_inspect_stl(args_json, *, cancel_event=None):
    return _model_tool(args_json, "inspect_model", cancel_event)


def rim_estimate_axis(args_json, *, cancel_event=None):
    return _model_tool(args_json, "estimate_model_axis", cancel_event)


def rim_process_sections_auto(args_json, *, cancel_event=None):
    return _model_tool(args_json, "process_model_sections", cancel_event)


def rim_repair_sections(args_json: str, *, cancel_event=None) -> str:
    """输入已有截面的 call_directory；复用 C++ 修补并返回新截面目录。

    错误码区分 JSON/参数错误、Worker 错误和未预期执行错误。
    Worker 的业务码、调用目录、退出码得以保留；不回传完整 worker_result。
    """
    try:
        if not isinstance(args_json, str):
            raise ValueError("工具参数必须是 JSON 字符串")
        parameters = json.loads(args_json, parse_constant=_reject_constant,
                                object_pairs_hook=_unique_object)
        if not isinstance(parameters, dict):
            raise ValueError("工具参数必须是 JSON 对象")
    except (ValueError, TypeError, RecursionError) as exc:
        result = {"status": "error", "error": {
            "code": "INVALID_ARGUMENTS", "message": str(exc)}}
    else:
        try:
            check_cancelled(cancel_event)
            from .services.section_service import repair_sections
            result = (repair_sections(parameters, cancel_event=cancel_event) if cancel_event is not None
                      else repair_sections(parameters))
            check_cancelled(cancel_event, result.get("call_directory"))
        except WorkerError as exc:
            result = {
                "status": "cancelled" if isinstance(exc, WorkerCancelledError) else "error",
                "error": {"code": exc.code, "message": str(exc)},
                "call_directory": str(exc.call_directory) if exc.call_directory else None,
                "returncode": exc.returncode,
            }
        except ValueError as exc:
            result = {"status": "error", "error": {
                "code": "INVALID_ARGUMENTS", "message": str(exc)}}
        except Exception as exc:
            result = {"status": "error", "error": {
                "code": "RIM_SECTION_FAILED", "message": str(exc)}}
    return json.dumps(result, ensure_ascii=False, allow_nan=False)


def rim_transverse_sections(args_json, *, cancel_event=None):
    return _model_tool(args_json, "process_transverse_sections", cancel_event)
