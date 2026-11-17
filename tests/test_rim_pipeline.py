"""完整重建的声明、路由、引用绑定和实体完成条件。"""
import json
import unittest

from agent.controller import AgentController
from agent.loop import AgentLoop, ToolExecution, LoopActionKind
from agent.tool_defs import RIM_RECONSTRUCTION_TOOL_NAMES
from core.session import ChatSession
from tests.test_rim_workflow import response


class PipelineTests(unittest.TestCase):
    def test_reconstruction_budget_crosses_old_limit_and_stops_at_own_limit(self):
        import core.config as config
        from unittest.mock import patch
        with patch.object(config,'MAX_ITERATIONS',30),patch.object(config,'RECONSTRUCTION_MAX_ITERATIONS',100):
            for workflow in ('rim_reconstruction','spoke_reconstruction'):
                loop=AgentLoop(AgentController(ChatSession()),'',workflow=workflow)
                loop._iteration=30
                action=loop.prepare_llm_call()
                self.assertEqual(action.kind,LoopActionKind.CALL_LLM)
                self.assertEqual(action.iteration,31)
                self.assertEqual(loop.max_iterations,100)
                loop._iteration=99
                self.assertEqual(loop.prepare_llm_call().kind,LoopActionKind.CALL_LLM)
                action=loop.prepare_llm_call()
                self.assertEqual(action.kind,LoopActionKind.FINISH)
                self.assertIn('100',action.summary)
            ordinary=AgentLoop(AgentController(ChatSession()),'')
            ordinary._iteration=30
            self.assertEqual(ordinary.prepare_llm_call().kind,LoopActionKind.FINISH)

    def test_user_can_stop_before_reconstruction_budget_is_used(self):
        self.loop.request_stop()
        self.assertEqual(self.loop.prepare_llm_call().kind,LoopActionKind.FINISH)
        self.assertEqual(self.loop.iteration,1)

    def test_reconstruction_config_defaults_override_and_refresh(self):
        import os
        import core.config as config
        from unittest.mock import patch
        with patch.dict(os.environ,{'RECONSTRUCTION_MAX_ITERATIONS':''}):
            cfg=config.Config()
            self.assertEqual(cfg.RECONSTRUCTION_MAX_ITERATIONS,100)
            os.environ['RECONSTRUCTION_MAX_ITERATIONS']='150'
            cfg.refresh()
            self.assertEqual(cfg.RECONSTRUCTION_MAX_ITERATIONS,150)
            os.environ['RECONSTRUCTION_MAX_ITERATIONS']='invalid'
            cfg.refresh()
            self.assertEqual(cfg.RECONSTRUCTION_MAX_ITERATIONS,100)

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

    def test_limit_reports_actual_stage_and_failure(self):
        self.sections();self.analysis();self.fit();self.solid()
        self.record('rim_fit_surface_patch',{'status':'error','error':{
            'code':'SURFACE_FIT_FAILED','message':'maximum deviation 0.38 mm',
            'details':{'maximum_deviation_mm':.38}}})
        self.loop._workflow='spoke_reconstruction'
        self.pipeline.require_spokes=True
        self.loop._iteration=100
        action=self.loop.prepare_llm_call()
        self.assertEqual(action.kind,LoopActionKind.FINISH)
        self.assertFalse(action.success)
        self.assertIn('回转毛坯已创建',action.summary)
        self.assertIn('rim_fit_surface_patch',action.summary)
        self.assertIn('0.38',action.summary)
        self.assertEqual(self.pipeline.request_context()['recent_failures'][-1]['code'],'SURFACE_FIT_FAILED')

    def test_successful_fit_reference_keeps_region_identity(self):
        self.record('rim_fit_surface_patch',{'status':'success','surface_report':'/surface'},
                    {'analysis_path':'/windows','region_id':'window_001_side_2'})
        self.assertEqual(self.pipeline.window_artifacts[-1]['region_id'],'window_001_side_2')

    def test_successful_diagnostic_with_failed_match_is_not_geometry_success(self):
        self.record('rim_validate_window_pattern',{'status':'success','stl_match_passed':False,
                    'instances':[{'maximum_sample_distance_mm':1.8,'matched':False}]})
        self.assertTrue(self.pipeline.last_tool_failed)
        self.assertEqual(self.pipeline.failures[-1]['code'],'GEOMETRY_CHECK_FAILED')

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
