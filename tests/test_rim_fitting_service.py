"""复用真实 Python 拟合器，验证来源、二维转换和工具边界。"""
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from tests import test_rim_candidate_service as candidate_tests
from rim_reconstruction.services.candidate_service import analyze_rim_candidates
from rim_reconstruction.services.fitting_service import fit_selected_profile
from rim_reconstruction.tool_handlers import rim_fit_profile
from rim_reconstruction.adapters.cpp_worker_adapter import WorkerCancelledError


class FittingServiceTests(unittest.TestCase):
    def setUp(self):
        fixture = candidate_tests.CandidateServiceTests()
        fixture.setUp()
        fixture.adapter.max_artifact_bytes = 128 * 1024 * 1024
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        # 现有候选测试的假适配器仅返回 run_id，补齐服务要求的源请求字段。
        self.analysis = analyze_rim_candidates(fixture.params, adapter=fixture.adapter)
        candidate = self.analysis["candidates"][0]
        self.params = {"analysis_path": self.analysis["analysis_path"],
                       "candidate_id": candidate["candidate_id"],
                       "segment_ids": [s["segment_id"] for s in candidate["segments"]]}

    def fit(self, parameters=None, **kwargs):
        return fit_selected_profile(parameters or self.params, adapter=self.fixture.adapter, **kwargs)

    def test_real_fit_uses_2d_and_keeps_provenance(self):
        result = self.fit()
        self.assertEqual(result["status"], "success", result)
        self.assertTrue(result["compliant"])
        self.assertEqual(result["coordinate_order"], ["axial", "radius"])
        saved = json.loads(Path(result["result_path"]).read_text(encoding="utf-8"))
        self.assertEqual(saved["items"][0]["fit"]["dimension"], 2)
        self.assertIn("reconstructed_point_indices", saved["items"][0]["source"])
        self.assertEqual(saved["items"][0]["source"]["source_kind"], "deterministic_base_reconstruction")
        self.assertNotIn('"points"', json.dumps(result))
        self.assertNotIn('"control_points"', json.dumps(result))
        self.assertEqual(len(saved["profile"]["segments"]), len(self.params["segment_ids"]))

    def test_invalid_model_selections(self):
        for extra in ({"candidate_id": "fake"}, {"segment_ids": ["fake"]},
                      {"segment_ids": []}, {"segment_ids": ["a", "a"]},
                      {"points": [[0, 0]]}, {"tolerance_mm": True},
                      {"max_iterations": 1}, {"analysis_path": "relative.json"}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                self.fit({**self.params, **extra})
        self.assertFalse((self.fixture.call / "fits").exists())

    def test_tampered_geometry_rejected(self):
        path = Path(self.params["analysis_path"])
        full = json.loads(path.read_text(encoding="utf-8"))
        selected = next(c for c in full["candidates"] if c["candidate_id"] == self.params["candidate_id"])
        selected["segments"][0]["points_ar"][0][0] += 1
        path.write_text(json.dumps(full), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "源数据不一致"):
            self.fit()

    def test_partial_material_boundary_rejected(self):
        with self.assertRaises(ValueError):
            self.fit({**self.params, "segment_ids": self.params["segment_ids"][:1]})

    def test_reconstructed_provenance_and_parameters_are_replayed(self):
        path = Path(self.params["analysis_path"])
        original = json.loads(path.read_text(encoding="utf-8"))
        import copy
        for kind in ("indices", "parameters", "version"):
            full = copy.deepcopy(original)
            c = next(c for c in full["candidates"] if c["candidate_id"] == self.params["candidate_id"])
            if kind == "indices":
                c["segments"][0]["reconstructed_point_indices"][0] += 1
            elif kind == "parameters":
                full["analysis_parameters"]["disc_radius_mm"] = 5.
            else:
                del full["reconstruction_version"]
            path.write_text(json.dumps(full), encoding="utf-8")
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                self.fit()

    def test_cancel_does_not_publish(self):
        event = threading.Event()
        event.set()
        with self.assertRaises(WorkerCancelledError):
            self.fit(cancel_event=event)
        self.assertFalse((self.fixture.call / "fits").exists())

    def test_cancel_after_solver_does_not_publish(self):
        from rim_reconstruction.geometry.bspline_fitter import fit_profile
        event = threading.Event()
        def solve(*args, **kwargs):
            result = fit_profile(*args, **kwargs)
            event.set()
            return result
        with patch("rim_reconstruction.geometry.bspline_fitter.fit_profile", side_effect=solve):
            with self.assertRaises(WorkerCancelledError):
                self.fit(cancel_event=event)
        self.assertFalse((self.fixture.call / "fits").exists())

    def test_partial_fit_failure_is_not_success(self):
        with patch("rim_reconstruction.geometry.bspline_fitter.fit_profile", return_value={
            "items": [{"source": {"segment_id": "segment_1"}, "fit": {
                "success": False, "compliant": False, "error": {"code": "CONTROL_POINT_LIMIT"}}}]}):
            result = self.fit()
        self.assertEqual(result["status"], "error")
        self.assertFalse(result["fit_success"])
        self.assertTrue(Path(result["result_path"]).is_file())

    def test_tool_boundary(self):
        for raw in ("[]", "bad", '{"x":NaN}', '{"x":1,"x":2}'):
            self.assertEqual(json.loads(rim_fit_profile(raw))["status"], "error")
        with patch("rim_reconstruction.services.fitting_service.fit_selected_profile",
                   return_value={"status": "success"}) as service:
            self.assertEqual(json.loads(rim_fit_profile(json.dumps(self.params)))["status"], "success")
            service.assert_called_once_with(self.params, cancel_event=None)


if __name__ == "__main__":
    unittest.main()
