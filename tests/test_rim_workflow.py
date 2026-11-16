"""轮辋工作流路由和阶段完成检查，不依赖 Qt、FreeCAD 或模型网络。"""
import json
import unittest

from agent.controller import AgentController
from agent.loop import AgentLoop, LoopActionKind, ToolExecution
from core.session import ChatSession


def response(content="完成", calls=None, reason="stop"):
    message = {"content": content}
    if calls:
        message["tool_calls"] = calls
    return {"choices": [{"message": message, "finish_reason": reason}]}


class RimWorkflowTests(unittest.TestCase):
    def make_loop(self, **kwargs):
        return AgentLoop(AgentController(ChatSession()), "已有文档，不是确认轴线", **kwargs)

    def test_rim_prompt_tools_and_protocol(self):
        loop = self.make_loop(workflow="rim_sections", last_mode="react")
        action = loop.start("提取截面")
        self.assertEqual(loop.mode, "tool_calling")
        self.assertEqual(loop.workflow, "rim_sections")
        names = [t["function"]["name"] for t in action.tools]
        self.assertEqual(len(names), len(set(names)))
        for required in ("rim_process_sections", "rim_transverse_sections", "rim_fit_surface_patch", "rim_cut_spoke_windows"):
            self.assertIn(required, names)
        prompt = action.messages[0]["content"]
        self.assertIn("不得猜测轴线", prompt)
        self.assertIn("已有文档", prompt)
        self.assertNotIn("CURRENT DESIGN PARAMETERS", prompt)

    def test_default_cad_tools(self):
        loop = self.make_loop()
        action = loop.start("创建圆柱体")
        names = [t["function"]["name"] for t in action.tools]
        self.assertIn("execute_code", names)
        self.assertNotIn("rim_process_sections", names)

    def test_unknown_workflow(self):
        with self.assertRaises(ValueError):
            self.make_loop(workflow="unknown")

    def test_no_tool_is_not_success(self):
        loop = self.make_loop(workflow="rim_sections")
        loop.start("信息不足，请询问")
        action = loop.handle_stream_done(response("请提供轴线"), True)
        self.assertEqual(action.kind, LoopActionKind.FINISH)
        self.assertFalse(action.success)
        self.assertFalse(action.from_stream)
        self.assertIn("请提供轴线", action.summary)

    def test_disallowed_tool_and_react_are_not_executed(self):
        for data in [response(calls=[{"function": {"name": "execute_code"}}], reason="tool_calls"),
                     response('<tool name="execute_code">pass</tool>')]:
            loop = self.make_loop(workflow="rim_sections")
            loop.start("截面")
            action = loop.handle_stream_done(data, False)
            self.assertEqual(action.kind, LoopActionKind.FINISH)
            self.assertFalse(action.success)

    def test_valid_tool_call_is_routed(self):
        loop = self.make_loop(workflow="rim_sections")
        loop.start("截面")
        action = loop.handle_stream_done(response(calls=[{
            "id": "tc", "function": {"name": "rim_process_sections", "arguments": "{}"}
        }], reason="tool_calls"), False)
        self.assertEqual(action.kind, LoopActionKind.EXECUTE_TOOLS)

    def test_success_then_failure_resets_gate(self):
        loop = self.make_loop(workflow="rim_sections")
        loop.start("截面")
        summary = {"status": "success", "operation": "process_sections", "unit": "mm",
                   "run_id": "run1", "sections": [{"section_id": "s1"}],
                   "section_artifacts": [{"artifact_id": "s1"}]}
        for payload, expected in [(summary, True), ({"status": "error"}, False),
                                  ({"status": "success"}, False)]:
            loop.handle_tool_results([ToolExecution("rim_process_sections", "{}", "tc", "",
                                                    json.dumps(payload), False)])
            action = loop.handle_stream_done(response(), False)
            self.assertEqual(action.success, expected)

    def test_stop_cannot_report_success(self):
        loop = self.make_loop(workflow="rim_sections")
        loop.start("截面")
        loop.request_stop()
        self.assertFalse(loop.handle_stream_done(response(), False).success)


class CandidateWorkflowTests(unittest.TestCase):
    def test_fit_result_controls_completion(self):
        loop = AgentLoop(AgentController(ChatSession()), "", workflow="rim_sections")
        loop.start("选择候选并拟合")
        payload = {"status": "success", "operation": "fit_profile",
                   "fit_success": True, "compliant": True, "result_path": "fit.json"}
        loop.handle_tool_results([ToolExecution("rim_fit_profile", "{}", "fit_call", "",
                                               json.dumps(payload), False)])
        self.assertTrue(loop._check_quality_gate()[0])
        loop.handle_tool_results([ToolExecution("rim_fit_profile", "{}", "fit_call", "",
                                               '{"status":"error"}', True)])
        self.assertFalse(loop._check_quality_gate()[0])

    def test_candidate_tool_routed_and_review_is_completed_analysis(self):
        loop = AgentLoop(AgentController(ChatSession()), "", workflow="rim_sections")
        loop.start("筛选已有截面")
        action=loop.handle_stream_done(response(calls=[{"id":"analysis_call", "function":{
            "name":"rim_analyze_candidates", "arguments":"{}"}}],reason="tool_calls"),False)
        self.assertEqual(action.kind,LoopActionKind.EXECUTE_TOOLS)
        payload={"status":"success","operation":"analyze_rim_candidates",
                 "analysis_status":"needs_review","analysis_id":"a1","analysis_path":"a1.json"}
        loop.handle_tool_results([ToolExecution("rim_analyze_candidates","{}","analysis_call","",
                                               json.dumps(payload),False)])
        self.assertTrue(loop._check_quality_gate()[0])
        loop._rim_sections_succeeded=True
        loop.handle_tool_results([ToolExecution("rim_analyze_candidates","{}","analysis_call","",
                                               '{"status":"error"}',True)])
        self.assertFalse(loop._check_quality_gate()[0])
