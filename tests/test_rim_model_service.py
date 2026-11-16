"""自主模型解析：验证 C++ 轴引用、模型角度规划和工具参数边界。"""
import math
import unittest
from unittest.mock import patch, Mock

from rim_reconstruction.services.model_service import _radial, process_model_sections
from rim_reconstruction.tool_handlers import rim_inspect_stl, rim_estimate_axis, rim_process_sections_auto
import json


class ModelServiceTests(unittest.TestCase):
    def test_ui_has_three_modes_and_no_workflow_label(self):
        import ast
        from pathlib import Path
        tree = ast.parse((Path(__file__).resolve().parents[1] / "ui/panel_ui.py").read_text(encoding="utf-8"))
        choices = [n.args[1].value for n in ast.walk(tree)
                   if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                   and n.func.attr == "addItem" and isinstance(n.func.value, ast.Attribute)
                   and n.func.value.attr == "workflow_combo"]
        self.assertEqual(choices, ["cad", "rim_reconstruction", "spoke_reconstruction"])
        labels = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        self.assertNotIn("工作流：", labels)
        self.assertIn("模式：", labels)

    def test_radial_is_deterministic_orthogonal_unit(self):
        for axis in ([0,0,1], [1,0,0], [1,2,3], [-4,2,1]):
            radial = _radial(axis)
            self.assertAlmostEqual(sum(a*r for a,r in zip(axis, radial)), 0)
            self.assertAlmostEqual(math.hypot(*radial), 1)

    def test_automatic_section_parameters_come_from_cpp(self):
        request = {"task_id":"t1", "stl_path":"mesh.stl"}
        result = {"metrics":{"axis_candidate":{"origin":[3,4,5],"direction":[0,0,1]}},
                  "warnings":["PCA_INITIALIZATION_ONLY"]}
        with patch("rim_reconstruction.services.model_service._load_call", return_value=(request,result)), \
             patch("rim_reconstruction.services.model_service.process_sections",
                   return_value={"status":"success","warnings":[]}) as service:
            angles = [11, 69.5, 158, 247, 319]
            output = process_model_sections({"axis_call_directory":"axis-call",
                "angles_degrees": angles, "angle_selection_reason": "初始探索采样，尚无轮辐相位证据"}, adapter=Mock())
        parameters = service.call_args.args[0]
        self.assertEqual(parameters["axis_origin"], [3,4,5])
        self.assertEqual(parameters["axis_direction"], [0,0,1])
        self.assertEqual(parameters["angles_degrees"], angles)
        self.assertEqual(output["angle_selection"]["source"], "agent")
        self.assertEqual(output["angle_selection"]["angles_degrees"], angles)
        self.assertFalse(output["axis_validated"])

    def test_no_default_angles_or_reason(self):
        for params in ({"axis_call_directory":"axis"},
                       {"axis_call_directory":"axis","angles_degrees":[1,77,233]},
                       {"axis_call_directory":"axis","angle_selection_reason":"探索"}):
            with self.subTest(params=params), patch(
                "rim_reconstruction.services.model_service.process_sections") as service:
                with self.assertRaises(ValueError):
                    process_model_sections(params, adapter=Mock())
                service.assert_not_called()

    def test_invalid_plans_never_call_worker(self):
        for angles in ([0,1], [0,1,1], [0,1,360], [0,1,float('nan')],
                       [0,1,float('inf')], [0,1,True], "0,47,103"):
            with self.subTest(angles=angles), patch(
                "rim_reconstruction.services.model_service._load_call") as load:
                with self.assertRaises(ValueError):
                    process_model_sections({"axis_call_directory":"axis", "angles_degrees":angles,
                        "angle_selection_reason":"探索"}, adapter=Mock())
                load.assert_not_called()

    def test_schema_requires_agent_plan_and_prompt_explains_feedback(self):
        from agent.tool_defs import RIM_TOOL_DEFINITIONS
        from agent.prompts import RIM_RECONSTRUCTION_SYSTEM_PROMPT
        tool = next(t["function"] for t in RIM_TOOL_DEFINITIONS
                    if t["function"]["name"] == "rim_process_sections_auto")
        self.assertEqual(set(tool["parameters"]["required"]),
                         {"axis_call_directory", "angles_degrees", "angle_selection_reason"})
        self.assertIn("探索性采样", RIM_RECONSTRUCTION_SYSTEM_PROMPT)
        self.assertIn("覆盖率", RIM_RECONSTRUCTION_SYSTEM_PROMPT)
        self.assertNotIn("默认八个", RIM_RECONSTRUCTION_SYSTEM_PROMPT)

    def test_model_cannot_invent_axis_or_task_id(self):
        for handler, args in [
            (rim_inspect_stl, {"stl_path":"mesh.stl","task_id":"invented"}),
            (rim_estimate_axis, {"axis_origin":[0,0,0]}),
            (rim_process_sections_auto, {"axis_call_directory":"axis", "axis_direction":[0,0,1]}),
        ]:
            result=json.loads(handler(json.dumps(args)))
            self.assertEqual(result["status"], "error")
            self.assertEqual(result["error"]["code"], "INVALID_ARGUMENTS")


if __name__ == "__main__":
    unittest.main()
