"""Analytic geometry, on-demand routing, JSON contract, and real CAD snapshots."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import numpy as np

from agent.controller import AgentController
from agent.loop import AgentLoop, LoopActionKind, ToolExecution
from agent.evaluation_intent import requests_evaluation
from core.session import ChatSession
from rim_reconstruction.geometry.surface_metrics import (
    compare_surfaces, point_triangle_distances, surface_distances, summarize)
from rim_reconstruction.tool_handlers import rim_evaluate_revolution
from rim_reconstruction.services.evaluation_service import prepare_evaluation, validate_parameters


def square(z=0):
    return np.array([[[0, 0, z], [1, 0, z], [1, 1, z]],
                     [[0, 0, z], [1, 1, z], [0, 1, z]]], dtype=float)


class SurfaceMetricsTests(unittest.TestCase):
    def test_base_regions_keep_global_samples_and_separate_design_changes(self):
        result = compare_surfaces(square(), square(.3), sample_count=200,
            region_spec={"origin":[0,0,0],"direction":[0,0,1],"disc_radius_mm":.7})
        self.assertEqual(result["model_to_reference"]["sample_count"], 200)
        regions = result["regions"]
        self.assertEqual(sum(v["sample_count"] for v in regions.values()), 200)
        for region in regions.values():
            self.assertGreater(region["sample_count"], 0)
            self.assertAlmostEqual(region["metrics"]["mae_mm"], .3)
        self.assertIn("intentional", regions["reconstructed_disc"]["interpretation"])

    def test_identical_surfaces_not_nearest_vertices(self):
        result = compare_surfaces(square(), square(), sample_count=100)
        for direction in ("model_to_reference",):
            self.assertLess(result[direction]["sampled_max_mm"], 1e-12)
            self.assertEqual([r["within_tolerance_area_percent"] for r in result[direction]["area_ratios"]], [100,100,100])

    def test_known_offset_multiple_thresholds(self):
        result = compare_surfaces(square(), square(.3), sample_count=100)
        metrics = result["model_to_reference"]
        for metric in ("mae_mm", "rms_mm", "p95_mm", "sampled_max_mm"):
            self.assertAlmostEqual(metrics[metric], .3)
        self.assertEqual([r["tolerance_mm"] for r in metrics["area_ratios"]], [.2, 1., 5.])
        self.assertEqual([r["within_tolerance_area_percent"] for r in metrics["area_ratios"]], [0,100,100])
        self.assertNotIn("reference_coverage", result)

    def test_threshold_boundaries_are_inclusive_and_cumulative(self):
        result = summarize(np.array([0., .2, .3, 1., 4., 5., 6.]), np.zeros((7, 3)), [.2, 1., 5.])
        np.testing.assert_allclose([r["within_tolerance_area_percent"] for r in result["area_ratios"]],
                                   np.array([2, 4, 6])/7*100)

    def test_threshold_validation(self):
        self.assertEqual(validate_parameters({})["tolerances_mm"], [.2, 1., 5.])
        self.assertEqual(validate_parameters({"tolerances_mm": [5, .2, 1]})["tolerances_mm"], [.2,1.,5.])
        for value in ([], [.2,.2], [True], [0], [-1], [float("nan")], [float("inf")], "0.2", [1]*33):
            with self.assertRaises(ValueError):
                validate_parameters({"tolerances_mm": value})

    def test_summary_known_values(self):
        distances = np.array([.01, .02, .03, .04, .4])
        result = summarize(distances, np.zeros((5, 3)), [.04])
        self.assertAlmostEqual(result["mae_mm"], .1)
        self.assertAlmostEqual(result["rms_mm"], np.sqrt(.163/5))
        self.assertAlmostEqual(result["p95_mm"], .328)
        self.assertEqual(result["area_ratios"][0]["within_tolerance_area_percent"], 80)

    def test_triangle_interior_edge_vertex(self):
        tri = np.array([[[0, 0, 0], [2, 0, 0], [0, 2, 0]]], float)
        for point, expected in (([.3, .4, 1], 1), ([1, -1, 0], 1), ([-1, -1, 0], np.sqrt(2))):
            self.assertAlmostEqual(point_triangle_distances(np.array(point), tri)[0], expected)

    def test_accelerator_matches_exhaustive_triangle_search(self):
        rng = np.random.default_rng(10)
        tri = rng.normal(size=(80, 3, 3))
        tri[0] *= 20  # Long triangle whose center may be far from the query.
        points = rng.normal(size=(30, 3))
        expected = np.array([point_triangle_distances(p, tri).min() for p in points])
        np.testing.assert_allclose(surface_distances(points, tri), expected, atol=1e-12)

    def test_only_model_is_sampled_and_only_one_distance_pass(self):
        from rim_reconstruction.geometry import surface_metrics
        with patch.object(surface_metrics, "sample_surface", wraps=surface_metrics.sample_surface) as sampling, \
             patch.object(surface_metrics, "surface_distances", wraps=surface_metrics.surface_distances) as distances:
            result = compare_surfaces(square(), square(.3), sample_count=100)
        self.assertEqual(sampling.call_count, 1)
        self.assertEqual(distances.call_count, 1)
        self.assertEqual(result["direction"], "model_to_reference")
        self.assertNotIn("reference_to_model", result)
        self.assertNotIn("bidirectional_sampled_max_mm", result)
        np.testing.assert_allclose(sampling.call_args.args[0], square(.3))

    def test_seed_repeatability(self):
        self.assertEqual(compare_surfaces(square(), square(.1), sample_count=100),
                         compare_surfaces(square(), square(.1), sample_count=100))

    def test_cancelled(self):
        from rim_reconstruction.adapters.cpp_worker_adapter import WorkerCancelledError
        event = threading.Event()
        event.set()
        with self.assertRaises(WorkerCancelledError):
            compare_surfaces(square(), square(), sample_count=100, cancel_event=event)

    def test_empty_or_nonfinite_rejected(self):
        for tri in ([], np.zeros((1, 3, 3)), np.full((1, 3, 3), np.nan)):
            with self.assertRaises(ValueError):
                compare_surfaces(tri, square(), sample_count=100)


class EvaluationRoutingTests(unittest.TestCase):
    def loop(self, text, workflow="rim_reconstruction", session=None):
        loop = AgentLoop(AgentController(session or ChatSession()), "existing document", workflow=workflow)
        action = loop.start(text)
        return loop, action

    def test_explicit_requests_in_both_modes(self):
        for workflow in ("cad", "rim_reconstruction", "rim_sections"):
            for text in ("请你评估一下生成的模型", "帮我计算一下轮辋误差", "Evaluate the generated model",
                         "先不要重建，只评估模型", "检查一下生成的模型"):
                loop, action = self.loop(text, workflow)
                self.assertEqual([t["function"]["name"] for t in action.tools], ["rim_evaluate_revolution"])
                self.assertIsNone(loop._rim_pipeline)
                self.assertFalse(loop._check_quality_gate()[0])

    def test_no_automatic_evaluation_or_historical_authorization(self):
        session = ChatSession()
        session.add_user_message("评估一下模型")
        for text in ("重建轮辋", "不需要评估，重建即可", "如何评估模型", "新增误差评估工具", "只有我需要的时候才评估"):
            loop, action = self.loop(text, session=session)
            self.assertFalse(loop._evaluation_requested)
            self.assertNotIn("rim_evaluate_revolution", [t["function"]["name"] for t in action.tools])

    def test_evaluation_finishes_without_rebuilding(self):
        loop, _ = self.loop("请评估模型")
        result = {"status": "success", "operation": "evaluate_revolution", "metrics": {"mae_mm": .1}}
        loop.handle_tool_results([ToolExecution("rim_evaluate_revolution", "{}", "e1", "", json.dumps(result), False)])
        action = loop.handle_stream_done({"choices": [{"message": {"content": "误差数据"}, "finish_reason": "stop"}]}, False)
        self.assertEqual(action.kind, LoopActionKind.FINISH)
        self.assertTrue(action.success)

    def test_boundary_and_parameter_validation(self):
        for raw in ("[]", "bad", '{"sample_count":true}', '{"tolerances_mm":[NaN]}',
                    '{"seed":1,"seed":2}', '{"code":"x"}', '{"support_distance_mm":0}'):
            self.assertEqual(json.loads(rim_evaluate_revolution(raw))["status"], "error")
        with self.assertRaises(ValueError):
            validate_parameters({"mesh_deflection_mm": -1})


try:
    import FreeCAD as App
    import Part
    import Mesh
except ImportError:
    App = None


@unittest.skipIf(App is None, "需要 FreeCAD 自带 Python")
class RealSnapshotTests(unittest.TestCase):
    def test_world_placement_and_current_geometry(self):
        doc = App.newDocument("EvaluationTest")
        try:
            body = doc.addObject("PartDesign::Body", "Body")
            obj = body.newObject("PartDesign::Feature", "Rim")
            obj.Shape = Part.makeBox(10, 10, 10)
            obj.addProperty("App::PropertyString", "SourceFitPath")
            body.Placement.Base = App.Vector(50, 20, 30)
            doc.recompute()
            with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
                reference = Path(directory)/"rim.stl"
                shape = obj.Shape.copy()
                shape.Placement = obj.getGlobalPlacement()
                vertices, faces = shape.tessellate(.02)
                Mesh.Mesh([[vertices[i] for i in face] for face in faces]).write(str(reference))
                parameters = {"document_name": doc.Name, "object_name": obj.Name,
                              "reference_stl_path": str(reference), "sample_count": 100}
                snapshot = prepare_evaluation(parameters)
                self.assertGreaterEqual(np.asarray(snapshot["reconstructed"])[:, :, 0].min(), 50)
                result = json.loads(rim_evaluate_revolution(json.dumps(parameters), snapshot=snapshot))
                self.assertEqual(result["status"], "success", result)
                self.assertLess(result["metrics"]["model_to_reference"]["sampled_max_mm"], 1e-5)
                # Use the same background execution path as the chat panel.
                from ui.rim_tool_worker import RimToolWorker
                worker = RimToolWorker({"function": {"name": "rim_evaluate_revolution",
                    "arguments": json.dumps(parameters)}}, None, evaluation_snapshot=snapshot)
                worker.start()
                self.assertTrue(worker.wait(10000))
                self.assertEqual(json.loads(worker.result)["status"], "success")
                worker = RimToolWorker({"function": {"name": "rim_evaluate_revolution",
                    "arguments": json.dumps(parameters)}}, None, evaluation_snapshot=snapshot)
                worker.cancel_event.set()
                worker.start()
                self.assertTrue(worker.wait(10000))
                self.assertEqual(json.loads(worker.result)["status"], "cancelled")
                # Editing the object must affect a new snapshot; don't reuse fit geometry.
                obj.Shape = Part.makeBox(10, 10, 12)
                obj.touch()
                self.assertIn("Touched", obj.State)
                result = json.loads(rim_evaluate_revolution(json.dumps(parameters)))
                self.assertEqual(result["status"], "success", result)
                self.assertGreater(result["metrics"]["model_to_reference"]["sampled_max_mm"], 1)
                self.assertNotIn("Touched", obj.State)
                # Explicit Body selection must resolve the source via its Tip.
                call = Path(directory)/"source"
                (call/"fits").mkdir(parents=True)
                obj.SourceFitPath = str(call/"fits"/"fit_test.json")
                (call/"request.json").write_text(json.dumps({"stl_path": str(reference)}), encoding="utf-8")
                body.touch()
                body_parameters = {"document_name": doc.Name, "object_name": body.Name, "sample_count": 100}
                result = json.loads(rim_evaluate_revolution(json.dumps(body_parameters)))
                self.assertEqual(result["status"], "success", result)
                self.assertEqual(result["evaluated_tip_name"], obj.Name)
                self.assertTrue(result["recomputed_before_evaluation"])
                # Genuine invalid geometry must still fail after recomputing.
                obj.Shape = Part.Shape()
                result = json.loads(rim_evaluate_revolution(json.dumps(parameters)))
                self.assertEqual(result["status"], "error")
                self.assertIn(obj.Name, result["error"]["message"])
        finally:
            App.closeDocument(doc.Name)


if __name__ == "__main__":
    unittest.main()
