"""无 Qt 环境下提取实际面板方法，验证取消队列和迟到回调；不替代 Qt 实机检查。"""
import ast
import json
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock


def panel_methods(*names):
    source = Path(__file__).resolve().parents[1] / "ui/panel.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "AgentPanel")
    methods = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in names]
    scope = {"json": json}
    exec(compile(ast.Module(body=methods, type_ignores=[]), str(source), "exec"), scope)
    return type("PanelHarness", (), {name: scope[name] for name in names})


class PanelCancellationTests(unittest.TestCase):
    def test_revolution_dispatches_in_main_thread_without_background_worker(self):
        panel = panel_methods("_run_next_tool")()
        panel._loop = SimpleNamespace(stopped=False, handle_tool_results=Mock(return_value="finished"))
        panel._tool_queue = [{"id": "build", "function": {
            "name": "rim_build_revolution", "arguments": '{"fit_path":"fit.json"}'}}]
        panel._tool_executions = []
        panel._controller = None
        panel._status_set = Mock()
        panel._record_tool_result = Mock()
        panel._execute_action = Mock()
        dispatch = Mock(return_value='{"status":"success"}')
        method = panel._run_next_tool.__func__
        method.__globals__["dispatch_tool"] = dispatch
        panel._run_next_tool()
        dispatch.assert_called_once_with("rim_build_revolution", '{"fit_path":"fit.json"}')
        panel._record_tool_result.assert_called_once()
        panel._execute_action.assert_called_once_with("finished")

    def test_finished_cancel_is_consumed_once_and_preserves_directory(self):
        panel = panel_methods("_complete_rim_tool")()
        panel._loop = SimpleNamespace(stopped=True)
        panel._turn_active = True
        event = threading.Event()
        event.set()
        worker = SimpleNamespace(loop=panel._loop, cancel_event=event, tool_call={},
            result=json.dumps({"status": "success", "call_directory": "call-1"}), deleteLater=Mock())
        panel._rim_threads = [worker]
        panel._record_tool_result = Mock()
        panel._run_next_tool = Mock()
        panel._complete_rim_tool(worker)
        panel._complete_rim_tool(worker)
        panel._record_tool_result.assert_called_once()
        result = json.loads(panel._record_tool_result.call_args.args[1])
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["call_directory"], "call-1")
        panel._run_next_tool.assert_called_once()
        worker.deleteLater.assert_called_once()

    def test_stale_result_never_advances_new_loop(self):
        panel = panel_methods("_complete_rim_tool")()
        panel._loop = object()
        panel._turn_active = True
        worker = SimpleNamespace(loop=object(), deleteLater=Mock())
        panel._rim_threads = [worker]
        panel._record_tool_result = Mock()
        panel._run_next_tool = Mock()
        panel._complete_rim_tool(worker)
        panel._record_tool_result.assert_not_called()
        panel._run_next_tool.assert_not_called()

    def test_stopped_batch_cancels_pending_tools_without_dispatch(self):
        panel = panel_methods("_run_next_tool")()
        panel._loop = SimpleNamespace(stopped=True, handle_tool_results=Mock(return_value="finished"))
        panel._tool_queue = [{"id": "a"}, {"id": "b"}]
        panel._tool_executions = []
        panel._controller = None
        panel._record_tool_result = Mock()
        panel._execute_action = Mock()
        panel._run_next_tool()
        self.assertEqual(panel._record_tool_result.call_count, 2)
        for call in panel._record_tool_result.call_args_list:
            self.assertEqual(json.loads(call.args[1])["status"], "cancelled")
        panel._execute_action.assert_called_once_with("finished")
