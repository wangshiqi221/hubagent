"""候选服务与 JSON 工具测试；无需 FreeCAD/Qt 或模型网络。"""
import copy
import json
import math
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from rim_reconstruction.services.candidate_service import analyze_rim_candidates
from rim_reconstruction.tool_handlers import rim_analyze_candidates
from rim_reconstruction.adapters.cpp_worker_adapter import WorkerCancelledError


class CandidateServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / "runtime")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.call = self.root / "call-test"
        self.call.mkdir()
        (self.call / "request.json").write_text(json.dumps({
            "operation": "process_sections", "task_id": "test"}), encoding="utf-8")
        (self.call / "result.json").write_text(json.dumps({"run_id": "run-test"}), encoding="utf-8")
        self.sections = []
        for angle in (0, 47, 103, 169):
            theta = math.radians(angle)
            radial = [math.cos(theta), math.sin(theta), 0]
            contours = []
            for index, ar in enumerate(([(0,1),(4,1),(4,10),(0,10)],
                                        [(0,8),(4,8),(4,10),(0,10)])):
                contours.append({"contour_index": index, "closed": True, "point_count": 4,
                    "points": [[r*radial[0],r*radial[1],a] for a,r in ar]})
            self.sections.append({"unit":"mm", "section_id":f"s{angle}",
                "angle_degrees":angle, "contours":contours,
                "frame":{"origin":[0,0,0],"axis_direction":[0,0,1],
                         "radial_direction":radial,"half_plane":True}})
        self.adapter = Mock(call_root=self.root, max_result_bytes=100000)
        self.adapter.load_sections.return_value = self.sections
        self.params = {"call_directory":str(self.call)}

    def test_candidates_saved_without_points_in_summary(self):
        before = copy.deepcopy(self.sections)
        result = analyze_rim_candidates({**self.params,"limit":1},adapter=self.adapter)
        self.assertEqual(result["analysis_status"],"candidates_found")
        self.assertEqual(len(result["candidates"]),1)
        self.assertTrue(result["has_more"])
        self.assertFalse(result["user_confirmed"])
        def no_points(value):
            if isinstance(value,dict):
                self.assertFalse({"points","points_ar","source_point_indices"} & value.keys())
                for item in value.values(): no_points(item)
            elif isinstance(value,list):
                for item in value: no_points(item)
        no_points(result)
        full=json.loads(Path(result["analysis_path"]).read_text(encoding="utf-8"))
        self.assertIn("points",full["candidates"][0]["segments"][0])
        self.assertEqual(full["analysis_id"],result["analysis_id"])
        self.assertEqual(before,self.sections)
        self.adapter.load_sections.assert_called_once()
        self.adapter.run.assert_not_called()

    def test_single_complete_contours_are_accepted(self):
        for section in self.sections:
            section["contours"] = section["contours"][:1]
        result=analyze_rim_candidates(self.params,adapter=self.adapter)
        self.assertEqual(result["status"],"success")
        self.assertEqual(result["analysis_status"],"candidates_found")
        self.assertEqual(result["candidate_count"],4)
        self.assertIsNone(result["reason"])

    def test_invalid_parameters_and_outside_paths(self):
        for changes in ({"limit":True},{"limit":51},{"min_support_ratio":2},
                        {"tangent_angle_degrees":90},{"points":[]},
                        {"call_directory":str(self.root.parent)}, {"call_directory":"relative"}):
            with self.subTest(changes=changes),self.assertRaises(ValueError):
                analyze_rim_candidates({**self.params,**changes},adapter=self.adapter)
        self.adapter.load_sections.assert_not_called()

    def test_raw_sections_rejected(self):
        (self.call / "request.json").write_text(json.dumps({"operation":"compute_sections"}),encoding="utf-8")
        with self.assertRaises(ValueError):
            analyze_rim_candidates(self.params,adapter=self.adapter)
        self.adapter.load_sections.assert_not_called()

    def test_adapter_validation_failure_preserved(self):
        self.adapter.load_sections.side_effect=ValueError("tampered artifact")
        with self.assertRaisesRegex(ValueError,"tampered"):
            analyze_rim_candidates(self.params,adapter=self.adapter)
        self.assertFalse((self.call / "analyses").exists())

    def test_cancel_does_not_publish(self):
        event=threading.Event()
        event.set()
        with self.assertRaises(WorkerCancelledError):
            analyze_rim_candidates(self.params,adapter=self.adapter,cancel_event=event)
        self.assertFalse((self.call / "analyses").exists())

    def test_json_boundary(self):
        for text in ('[]','{"limit":NaN}','{"limit":1,"limit":2}','bad'):
            result=json.loads(rim_analyze_candidates(text))
            self.assertEqual(result["status"],"error")
        with patch("rim_reconstruction.services.candidate_service.analyze_rim_candidates",
                   return_value={"status":"success","analysis_status":"needs_review"}) as service:
            result=json.loads(rim_analyze_candidates(json.dumps(self.params)))
            self.assertEqual(result["analysis_status"],"needs_review")
            service.assert_called_once_with(self.params,cancel_event=None)


if __name__ == "__main__":
    unittest.main()
