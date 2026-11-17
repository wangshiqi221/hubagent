"""完整重建的声明、路由、引用绑定和实体完成条件。"""
import json
import unittest

from agent.controller import AgentController
from agent.loop import AgentLoop, ToolExecution, LoopActionKind
from agent.tool_defs import RIM_RECONSTRUCTION_TOOL_NAMES
from core.session import ChatSession
from tests.test_rim_workflow import response


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.loop = AgentLoop(AgentController(ChatSession()), "", workflow="rim_reconstruction")
        self.action = self.loop.start("多角度分析，自动选择并重建回转实体")
        self.pipeline = self.loop._rim_pipeline

    def record(self, name, data, arguments=None):
        self.action = self.loop.handle_tool_results([ToolExecution(
            name, json.dumps(arguments or {}), "id", "", json.dumps(data), data.get("status") != "success")])

    def sections(self):
        self.record("rim_inspect_stl", {"status": "success", "operation": "inspect_stl",
            "task_id": "t1", "call_directory": "/inspect1"})
        self.record("rim_estimate_axis", {"status": "success", "operation": "estimate_axis",
            "task_id": "t1", "call_directory": "/axis1"})
        self.record("rim_process_sections", {"status": "success", "operation": "process_sections",
            "call_directory": "/call1", "sections": [{}], "section_artifacts": [{}]})

    def analysis(self, status="candidates_found"):
        self.record("rim_analyze_candidates", {"status": "success", "operation": "analyze_rim_candidates",
            "call_directory": "/call1", "analysis_path": "/analysis1", "analysis_id": "a1", "analysis_status": status})

    def fit(self):
        self.record("rim_fit_profile", {"status": "success", "operation": "fit_profile",
            "analysis_id": "a1", "fit_success": True, "compliant": True, "fit_id": "f1", "result_path": "/fit1"})

    def solid(self):
        self.record("rim_build_revolution", {"status": "success", "operation": "build_rim_revolution",
            "fit_id": "f1", "solid_count": 1, "volume_mm3": 100, "document_name": "doc",
            "body_name": "body", "sketch_name": "sketch", "revolution_name": "rev"})

    def test_full_chain_and_stable_tool_declarations(self):
        self.assertEqual({t["function"]["name"] for t in self.action.tools}, set(RIM_RECONSTRUCTION_TOOL_NAMES))
        self.assertFalse(self.loop._check_quality_gate()[0])
        self.sections()
        self.assertIn("rim_analyze_candidates", self.pipeline.allowed_tools())
        self.analysis()
        self.assertIn("rim_fit_profile", self.pipeline.allowed_tools())
        self.assertIsNone(self.pipeline.validate_call("rim_fit_profile", '{"analysis_path":"/analysis1"}'))
        self.fit()
        self.assertFalse(self.loop._check_quality_gate()[0])
        self.assertIn("rim_build_revolution", self.pipeline.allowed_tools())
        self.solid()
        self.assertTrue(self.loop.handle_stream_done(response(), False).success)

    def test_estimation_success_exposes_sections_and_recovers_premature_finish(self):
        self.record("rim_inspect_stl", {"status":"success", "operation":"inspect_stl",
                    "task_id":"same_task", "call_directory":"/inspect"})
        first_names = {t["function"]["name"] for t in self.action.tools}
        self.record("rim_estimate_axis", {"status":"success", "operation":"estimate_axis",
                    "task_id":"same_task", "call_directory":"/axis", "axis_validated":False})
        self.assertEqual(first_names, {t["function"]["name"] for t in self.action.tools})
        self.assertIn("rim_process_sections_auto", first_names)
        self.assertEqual(self.pipeline.next_step(), "rim_process_sections_auto")
        self.assertIn('"axis_call_directory": "/axis"', self.action.messages[-1]["content"])
        result = self.loop.handle_stream_done(response("后续工具不可用"), False)
        self.assertEqual(result.kind, LoopActionKind.CALL_LLM)
        self.assertIn("rim_process_sections_auto", result.messages[-1]["content"])
        second = self.loop.handle_stream_done(response("仍不调用"), False)
        self.assertEqual(second.kind, LoopActionKind.FINISH)
        self.assertFalse(second.success)

    def test_true_tool_failure_does_not_trigger_continuation(self):
        self.record("rim_inspect_stl", {"status":"success", "operation":"inspect_stl",
                    "task_id":"same_task", "call_directory":"/inspect"})
        self.record("rim_estimate_axis", {"status":"error", "error":{"code":"AXIS_ESTIMATION_FAILED"}})
        result = self.loop.handle_stream_done(response("轴估计失败"), False)
        self.assertEqual(result.kind, LoopActionKind.FINISH)
        self.assertFalse(result.success)

    def test_stable_declaration_does_not_allow_out_of_order_execution(self):
        self.assertIn("rim_build_revolution", {t["function"]["name"] for t in self.action.tools})
        self.assertIsNotNone(self.pipeline.validate_call("rim_build_revolution", '{"fit_path":"fake"}'))

    def test_no_candidates_cannot_fit(self):
        self.sections()
        self.analysis("needs_review")
        self.assertNotIn("rim_fit_profile", self.pipeline.allowed_tools())
        self.assertFalse(self.loop._check_quality_gate()[0])

    def test_out_of_order_and_wrong_references(self):
        self.assertIsNotNone(self.pipeline.validate_call("rim_build_revolution", '{}'))
        self.sections()
        self.assertIsNotNone(self.pipeline.validate_call("rim_analyze_candidates", '{"call_directory":"/old"}'))
        self.analysis()
        self.assertIsNotNone(self.pipeline.validate_call("rim_fit_profile", '{"analysis_path":"/old"}'))
        self.fit()
        self.assertIsNotNone(self.pipeline.validate_call("rim_build_revolution", '{"fit_path":"/old"}'))

    def test_upstream_retry_and_build_failure_invalidate_success(self):
        self.sections(); self.analysis(); self.fit(); self.solid()
        self.record("rim_build_revolution", {"status": "error"})
        self.assertFalse(self.loop._check_quality_gate()[0])
        self.solid()
        self.record("rim_inspect_stl", {"status": "error"})
        self.assertEqual(self.pipeline.allowed_tools(), {"rim_inspect_stl"})
        self.assertFalse(self.loop._check_quality_gate()[0])

    def test_only_one_tool_per_step(self):
        call = {"id": "id", "function": {"name": "rim_inspect_stl", "arguments": "{}"}}
        action = self.loop.handle_stream_done(response(calls=[call, call], reason="tool_calls"), False)
        self.assertFalse(action.success)

    def test_four_independent_surface_fits_are_queued_with_original_ids(self):
        self.sections()
        calls=[{'id':f'fit_{i}','function':{'name':'rim_fit_surface_patch','arguments':json.dumps({'analysis_path':'existing.json','region_id':str(i)})}} for i in range(4)]
        action=self.loop.handle_stream_done(response(calls=calls,reason='tool_calls'),False)
        self.assertEqual(action.kind,LoopActionKind.EXECUTE_TOOLS)
        self.assertEqual(action.tool_calls,calls)

    def test_dependent_batch_returns_tool_errors_and_allows_model_to_retry(self):
        calls=[{'id':'inspect','function':{'name':'rim_inspect_stl','arguments':'{}'}},
               {'id':'axis','function':{'name':'rim_estimate_axis','arguments':'{}'}}]
        action=self.loop.handle_stream_done(response(calls=calls,reason='tool_calls'),False)
        self.assertEqual(action.kind,LoopActionKind.CALL_LLM)
        messages=self.loop._controller.session.get_messages()
        replies=[m for m in messages if m.get('role')=='tool']
        self.assertEqual([m['tool_call_id'] for m in replies],['inspect','axis'])
        self.assertTrue(all('BATCH_REQUIRES_SEPARATE_ROUNDS' in m['content'] for m in replies))

    def test_batch_size_limit_cannot_execute_unbounded_operations(self):
        calls=[{'id':str(i),'function':{'name':'rim_fit_surface_patch','arguments':'{}'}} for i in range(9)]
        action=self.loop.handle_stream_done(response(calls=calls,reason='tool_calls'),False)
        self.assertEqual(action.kind,LoopActionKind.CALL_LLM)


if __name__ == "__main__":
    unittest.main()
