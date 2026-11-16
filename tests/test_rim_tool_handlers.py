"""轮辋 JSON 工具边界及 CadAgent 注册链路，不依赖实际 FreeCAD。"""
import json
from pathlib import Path
from unittest.mock import patch

import tempfile
import unittest

from agent.tool_dispatch import dispatch_tool, tool_result_is_error
from rim_reconstruction import tool_handlers
from rim_reconstruction.adapters.cpp_worker_adapter import WorkerOperationError
from tests.helpers import load_tools_module


def check_invalid_json_never_calls_service(arguments):
    with patch.object(tool_handlers, "process_sections") as service:
        result = json.loads(tool_handlers.rim_process_sections(arguments))
    service.assert_not_called()
    assert result["error"]["code"] == "INVALID_ARGUMENTS"


def check_success_passes_service_summary_unchanged():
    summary = {"status": "success", "sections": [{"point_count": 42}]}
    with patch.object(tool_handlers, "process_sections", return_value=summary) as service:
        result = tool_handlers.rim_process_sections('{"task_id":"测试"}')
    service.assert_called_once_with({"task_id": "测试"})
    assert json.loads(result) == summary
    assert not tool_result_is_error(result)


def check_service_errors_are_json(exception, code):
    with patch.object(tool_handlers, "process_sections", side_effect=exception):
        result = tool_handlers.rim_process_sections("{}")
    assert json.loads(result)["error"]["code"] == code
    assert tool_result_is_error(result)


def check_worker_error_preserves_diagnostics_without_raw_geometry(tmp_path):
    error = WorkerOperationError("STL_LOAD_FAILED", "无法读取 STL",
        call_directory=tmp_path, returncode=3, worker_result={"points": [[1, 2, 3]]})
    with patch.object(tool_handlers, "process_sections", side_effect=error):
        result = json.loads(tool_handlers.rim_process_sections("{}"))
    assert result["error"]["code"] == "STL_LOAD_FAILED"
    assert result["call_directory"] == str(tmp_path)
    assert result["returncode"] == 3
    assert "points" not in json.dumps(result)


def check_cadagent_import_registers_handler_and_dispatches():
    load_tools_module()
    with patch.object(tool_handlers, "process_sections", return_value={"status": "success"}):
        assert json.loads(dispatch_tool("rim_process_sections", "{}"))["status"] == "success"
    assert tool_result_is_error(dispatch_tool("rim_process_sections", "[]"))


def check_result_status_compatibility(result, expected):
    assert tool_result_is_error(result) is expected


def check_real_worker_through_handler(tmp_path):
    from rim_reconstruction.adapters.cpp_worker_adapter import CppWorkerAdapter
    from rim_reconstruction.services.section_service import process_sections

    configured = CppWorkerAdapter.from_config()
    if not configured.executable.is_file():
        raise unittest.SkipTest("geometry_worker.exe unavailable")
    stl = tmp_path / "四面体.stl"
    vertices = [(0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 1)]
    lines = ["solid tetra"]
    for face in [(0, 1, 2), (0, 3, 1), (0, 2, 3), (1, 3, 2)]:
        lines += ["facet normal 0 0 0", "outer loop"]
        lines += ["vertex %s %s %s" % vertices[i] for i in face]
        lines += ["endloop", "endfacet"]
    stl.write_text("\n".join(lines + ["endsolid tetra"]), encoding="utf-8")
    adapter = CppWorkerAdapter(configured.executable, tmp_path / "calls")
    arguments = {"task_id": "handler_test", "stl_path": str(stl), "unit": "mm",
        "axis_origin": [.2, .2, 0], "axis_direction": [0, 0, 1],
        "radial_reference": [1, 0, 0], "angles_degrees": [0, 47, 103],
        "processing": {"enable_micro_bump_cleaning": False}}
    with patch.object(tool_handlers, "process_sections",
                      side_effect=lambda p: process_sections(p, adapter=adapter)):
        result = json.loads(tool_handlers.rim_process_sections(json.dumps(arguments)))
    assert result["status"] == "success", result
    assert len(result["sections"]) == 3
    assert [s["angle_degrees"] for s in result["sections"]] == [0, 47, 103]
    assert len(list((tmp_path / "calls").glob("call-*"))) == 1
    assert all(Path(a["path"]).is_file() for a in result["section_artifacts"])
    assert '"points"' not in json.dumps(result)


class RimToolHandlerTests(unittest.TestCase):
    def test_invalid_json(self):
        for arguments in [None, "[]", "null", "{", '{"x":NaN}', '{"x":1,"x":2}']:
            with self.subTest(arguments=arguments):
                check_invalid_json_never_calls_service(arguments)

    def test_success(self):
        check_success_passes_service_summary_unchanged()

    def test_service_errors(self):
        for exception, code in [(ValueError("参数错误"), "INVALID_ARGUMENTS"),
                                (OSError("无法读取配置"), "RIM_SECTION_FAILED")]:
            with self.subTest(code=code):
                check_service_errors_are_json(exception, code)

    def test_worker_error(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            check_worker_error_preserves_diagnostics_without_raw_geometry(Path(directory))

    def test_registration(self):
        check_cadagent_import_registers_handler_and_dispatches()

    def test_result_status(self):
        for result, expected in [("ERROR: failed", True), ("FAIL: invalid", True),
                                 ("OK: done", False), ('{"status":"error"}', True),
                                 ('{"status":"cancelled"}', True),
                                 ('{"status":"success"}', False), ('[]', False), ('{}', False)]:
            with self.subTest(result=result):
                check_result_status_compatibility(result, expected)

    def test_real_worker(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            check_real_worker_through_handler(Path(directory))


import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from rim_reconstruction.services.section_service import repair_sections
from rim_reconstruction.tool_handlers import rim_repair_sections

class RepairTests(unittest.TestCase):
    def test_replay_preserves_frame_and_returns_new_directory(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / "runtime") as tmp:
            root=Path(tmp); call=root/'source'; call.mkdir()
            request={'operation':'process_sections','task_id':'t','stl_path':'source.stl',
                'unit':'mm','axis':{'origin':[1,2,3],'direction':[0,0,1]},
                'radial_reference':[1,0,0],'angles_degrees':[0,47,96]}
            (call/'request.json').write_text(json.dumps(request))
            (call/'result.json').write_text('{}')
            adapter=SimpleNamespace(call_root=root,max_result_bytes=10000,
                load_sections=Mock(return_value=[{'section_id':'s','contours':[{'closed':False}]}]))
            output={'sections':[{'contour_count':1,'open_contour_count':0}], 'call_directory':str(root/'new')}
            with patch('rim_reconstruction.services.section_service.process_sections',return_value=output) as process:
                r=repair_sections({'call_directory':str(call)},adapter=adapter)
            self.assertEqual(process.call_args.args[0]['axis_origin'],[1,2,3])
            self.assertEqual(process.call_args.args[0]['angles_degrees'],[0,47,96])
            self.assertEqual(r['repair_status'],'closed')
            self.assertEqual(r['repair_source_call_directory'],str(call))
            self.assertNotEqual(r['call_directory'],str(call))
    def test_bad_arguments(self):
        for args in ('[]','{"call_directory":"relative"}','{"points":[]}'):
            self.assertEqual(json.loads(rim_repair_sections(args))['status'],'error')
