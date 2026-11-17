"""模型自主解析：复用 C++ inspect_stl/estimate_axis，自动构造截面参数。"""
import math
from pathlib import Path
import uuid

from ..adapters.cpp_worker_adapter import CppWorkerAdapter, _read_json, check_cancelled
from .section_service import process_sections


def _adapter(adapter):
    return adapter if adapter is not None else CppWorkerAdapter.from_config()


def _load_call(directory, operation, adapter):
    if not isinstance(directory, str) or not Path(directory).is_absolute():
        raise ValueError("必须使用上一步返回的绝对调用目录")
    call = Path(directory).resolve()
    if call == adapter.call_root.resolve() or not call.is_relative_to(adapter.call_root.resolve()):
        raise ValueError("目录不属于当前 Worker 运行目录")
    request = _read_json(call / "request.json", 1024 * 1024)
    result = _read_json(call / "result.json", adapter.max_result_bytes)
    if request.get("operation") != operation:
        raise ValueError("输入不是所需的模型解析产物")
    adapter._validate_result(result, request, call)
    if operation=='estimate_axis' and request.get('source_axis_call_directory'):
        from .window_service import _hash
        report=_read_json(call/'axis_refinement.json',adapter.max_artifact_bytes)
        if report.get('source_sha256')!=_hash(request['stl_path']):raise ValueError('Source mesh changed after axis refinement')
        if report.get('axis_candidate',result['metrics']['axis_candidate'])!=result['metrics']['axis_candidate']:
            raise ValueError('Refined axis disagrees with its registration report')
    return request, result


def _radial(direction):
    # 选取与轴最不平行的基向量并正交化，完全由确定性计算生成。
    size = math.hypot(*direction)
    axis = [v / size for v in direction]
    index = min(range(3), key=lambda i: abs(axis[i]))
    basis = [float(i == index) for i in range(3)]
    vector = [basis[i] - axis[index] * axis[i] for i in range(3)]
    norm = math.hypot(*vector)
    return [v / norm for v in vector]


def inspect_model(parameters, *, adapter=None, cancel_event=None):
    if (not isinstance(parameters, dict) or not {"stl_path"} <= parameters.keys()
            or parameters.keys() - {"stl_path", "unit"}):
        raise ValueError("只接受 stl_path 和可选 unit；任务标识由程序生成")
    path = parameters["stl_path"]
    if not isinstance(path, str) or not path.strip() or parameters.get("unit", "mm") != "mm":
        raise ValueError("STL 路径不能为空，当前按毫米解释，不自动缩放")
    adapter = _adapter(adapter)
    request = {"schema_version": 1, "operation": "inspect_stl", "task_id": "rim_" + uuid.uuid4().hex,
               "stl_path": path, "unit": "mm"}
    check_cancelled(cancel_event)
    response = adapter.run(request, cancel_event=cancel_event)
    return {"status": "success", "operation": "inspect_stl", "unit": "mm",
            "task_id": response.result["task_id"], "call_directory": str(response.call_directory),
            "metrics": response.result["metrics"], "warnings": response.result["warnings"]}


def estimate_model_axis(parameters, *, adapter=None, cancel_event=None):
    if not isinstance(parameters, dict) or set(parameters) != {"inspection_call_directory"}:
        raise ValueError("只接受 inspection_call_directory，不接受模型生成的轴坐标")
    adapter = _adapter(adapter)
    request, _ = _load_call(parameters["inspection_call_directory"], "inspect_stl", adapter)
    check_cancelled(cancel_event)
    response = adapter.run({"schema_version": 1, "operation": "estimate_axis",
        "task_id": request["task_id"], "stl_path": request["stl_path"], "unit": "mm"},
        cancel_event=cancel_event)
    axis = response.result["metrics"]["axis_candidate"]
    return {"status": "success", "operation": "estimate_axis", "task_id": request["task_id"],
            "call_directory": str(response.call_directory), "unit": "mm",
            "axis_candidate": axis, "radial_reference": _radial(axis["direction"]),
            "axis_validated": False, "warnings": response.result["warnings"]}


def process_model_sections(parameters, *, adapter=None, cancel_event=None):
    # 轮辋逆向扩展：角度必须由模型基于解析摘要显式规划，绝不回退固定列表。
    required = {"axis_call_directory", "angles_degrees", "angle_selection_reason"}
    if not isinstance(parameters, dict) or set(parameters) != required:
        raise ValueError("必须提供 axis_call_directory、angles_degrees、angle_selection_reason；没有默认角度")
    adapter = _adapter(adapter)
    angles = parameters["angles_degrees"]
    if (not isinstance(angles, list) or not 3 <= len(angles) <= 360
            or any(type(a) not in (int, float) or not 0 <= a < 360 for a in angles)
            or len(set(angles)) != len(angles)):
        raise ValueError("模型必须选择3～360个不重复的[0,360)有限角度")
    reason = parameters["angle_selection_reason"]
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 4000:
        raise ValueError("angle_selection_reason 必须说明角度选择依据，且不超过4000字符")
    request, result = _load_call(parameters["axis_call_directory"], "estimate_axis", adapter)
    axis = result["metrics"]["axis_candidate"]
    output = process_sections({"task_id": request["task_id"], "stl_path": request["stl_path"],
        "unit": "mm", "axis_origin": axis["origin"], "axis_direction": axis["direction"],
        "radial_reference": _radial(axis["direction"]),
        "angles_degrees": angles},
        adapter=adapter, cancel_event=cancel_event)
    output["axis_source_call_directory"] = parameters["axis_call_directory"]
    output["axis_validated"] = False
    output["angle_selection"] = {"source": "agent", "angles_degrees": list(angles),
                                 "reason": reason.strip()}
    output["warnings"] = list(result["warnings"]) + output["warnings"]
    return output


def process_transverse_sections(parameters, *, adapter=None, cancel_event=None):
    from .transverse_service import process_transverse_sections as process
    return process(parameters, adapter=adapter, cancel_event=cancel_event)


def refine_model_axis(parameters,*,adapter=None,cancel_event=None):
    if set(parameters)!={'axis_call_directory','instance_count'}:raise ValueError('Use source axis and observed instance count')
    count=parameters['instance_count']
    if type(count)!=int or not 2<=count<=12:raise ValueError('instance_count must be 2..12, from observed pattern evidence')
    adapter=_adapter(adapter);request,result=_load_call(parameters['axis_call_directory'],'estimate_axis',adapter)
    from ..geometry.transverse_sections import load_stl
    from ..geometry.symmetry_axis import refine_symmetry_axis
    from .window_service import _save,_hash
    import copy
    axis,report=refine_symmetry_axis(load_stl(request['stl_path'],cancel_event),result['metrics']['axis_candidate'],count,cancel_event=cancel_event)
    root=adapter.call_root/('axis_refined_'+uuid.uuid4().hex);root.mkdir()
    req=copy.deepcopy(request);req['source_axis_call_directory']=parameters['axis_call_directory'];req['refinement_instance_count']=count
    output=copy.deepcopy(result);output['metrics']['axis_candidate']=axis
    output['run_id']=root.name;output['warnings']=['SYMMETRY_AXIS_REFINED: engineering accuracy remains unconfirmed']
    report.update(source_axis_call_directory=parameters['axis_call_directory'],source_sha256=_hash(request['stl_path']),axis_candidate=axis)
    _save(root/'request.json',req);_save(root/'result.json',output);_save(root/'axis_refinement.json',report)
    adapter._validate_result(output,req,root)
    return {'status':'success','operation':'estimate_axis','task_id':req['task_id'],'call_directory':str(root),
            'axis_candidate':axis,'axis_validated':False,'refinement_report':str(root/'axis_refinement.json'),
            'registration':report,'unit':'mm','warnings':output['warnings']}
