"""检查拟合精度、周期接缝、曲线重建及失败边界，不依赖 C++/LLM。"""
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np
from scipy.interpolate import BSpline

from geometry.bspline_fitter import (
    evaluate_bspline, fit_contour, fit_profile, fit_section, save_fit_result,
    _constrained_spline, _reference_directions,
)


class BSplineFitterTests(unittest.TestCase):
    def test_straight_line_is_compact_and_input_is_unchanged(self):
        x = np.linspace(0, 10, 50)
        points = np.c_[x, 2*x, np.zeros_like(x)]
        before = points.copy()
        result = fit_contour(points, closed=False, tolerance_mm=1e-6)
        self.assertTrue(result["success"], result)
        self.assertEqual(result["bspline"]["control_point_count"], 4)
        self.assertLessEqual(result["maximum_deviation_mm"], 1e-6)
        np.testing.assert_array_equal(points, before)
        rebuilt = evaluate_bspline(result["bspline"], np.linspace(0, 1, 500))
        np.testing.assert_allclose(rebuilt, result["sample_points"], atol=1e-12)

    def test_space_curve_residual_is_independently_verified(self):
        t = np.linspace(0, 2, 90)
        points = np.c_[t, np.sin(3*t), np.cos(2*t)]
        result = fit_contour(points, closed=False, tolerance_mm=.005)
        self.assertTrue(result["compliant"], result)
        u = np.r_[0, np.cumsum(np.linalg.norm(np.diff(points, axis=0), axis=1))]
        u /= u[-1]
        distances = np.linalg.norm(evaluate_bspline(result["bspline"], u)-points, axis=1)
        self.assertAlmostEqual(float(distances.max()), result["maximum_deviation_mm"])
        self.assertLessEqual(distances.max(), .005)

    def test_closed_circle_has_periodic_position_and_derivatives(self):
        t = np.linspace(0, 2*np.pi, 80, endpoint=False)
        points = np.c_[10*np.cos(t), 10*np.sin(t)]
        result = fit_contour(points, closed=True, tolerance_mm=.01)
        self.assertTrue(result["success"], result)
        curve = result["bspline"]
        spline = BSpline(curve["knots"], curve["control_points"], 3)
        for derivative in (0, 1, 2):
            np.testing.assert_allclose(spline(0, nu=derivative), spline(1, nu=derivative), atol=1e-8)
        self.assertFalse(np.array_equal(result["sample_points"][0], result["sample_points"][-1]))
        repeated = fit_contour(np.vstack((points, points[0])), closed=True, tolerance_mm=.01)
        self.assertEqual(repeated["retained_point_count"], 80)

    def test_nonuniform_periodic_knots_preserve_seam(self):
        t = np.linspace(0, 2*np.pi, 55, endpoint=False)
        points = np.c_[np.cos(t), .6*np.sin(t)]
        work = np.vstack([points, points[0]])
        u = np.r_[0., np.cumsum(np.linalg.norm(np.diff(work, axis=0), axis=1))]
        u /= u[-1]
        spline, _ = _constrained_spline(work, u, np.array([0, .05, .2, .4, .7, 1.]),
                                       True, .01, .2, 1., 1.)
        for order in range(3):
            np.testing.assert_allclose(spline(0, nu=order), spline(1, nu=order), atol=1e-10)
        directions = _reference_directions(work, u, np.array([0., 1.]), .1, True)
        np.testing.assert_allclose(directions[0], directions[1], atol=1e-12)

    def test_soft_constraints_affect_solution_and_ignore_collinear_density(self):
        work = np.array([[0., 0.], [1., .4], [2., -.2], [3., 0.]])
        u = np.r_[0., np.cumsum(np.linalg.norm(np.diff(work, axis=0), axis=1))]
        u /= u[-1]
        breaks = np.linspace(0, 1, 6)
        plain, _ = _constrained_spline(work, u, breaks, False, .1, .5, 0., 1.)
        directed, _ = _constrained_spline(work, u, breaks, False, .1, .5, 10., 1.)
        q = np.linspace(0, 1, 501)
        self.assertGreater(np.max(np.linalg.norm(plain(q)-directed(q), axis=1)), 1e-4)
        smooth, _ = _constrained_spline(work, u, breaks, False, .1, .5, 0., 1000.)
        self.assertLess(np.sum(smooth(q, nu=2)**2), np.sum(plain(q, nu=2)**2))
        refined = np.vstack([work[0], .7*work[0]+.3*work[1], work[1:]])
        refined_u = np.r_[u[0], .7*u[0]+.3*u[1], u[1:]]
        dense, _ = _constrained_spline(refined, refined_u, breaks, False, .1, .5, 0., 1.)
        np.testing.assert_allclose(plain(q), dense(q), atol=1e-11)
        np.testing.assert_allclose(directed([0., 1.]), work[[0, -1]], atol=1e-12)

    def test_invalid_direction_options(self):
        points = [[0, 0], [1, 0], [2, 1], [3, 0]]
        for name in ("direction_weight", "direction_window_mm"):
            for value in (-1, float("nan"), True, "1"):
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    fit_contour(points, closed=False, **{name: value})

    def test_duplicate_points_and_invalid_input(self):
        points = [[0, 0], [1, 0], [1, 0], [2, 0], [3, 0]]
        result = fit_contour(points, closed=False)
        self.assertEqual(result["retained_point_indices"], [0, 1, 3, 4])
        for data in ([[0, 0]]*5, [[0, 0], [1, 1]], [[0, 0], [1, 0], [2, 0], [float('nan'), 0]]):
            with self.subTest(data=data), self.assertRaises(ValueError):
                fit_contour(data, closed=False)
        for options in ({"tolerance_mm": 0}, {"degree": 2}, {"closed": 1}, {"max_control_points": True}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                fit_contour(points, **{"closed": False, **options})

    def test_control_point_budget_failure_is_not_success(self):
        x = np.linspace(0, 8, 80)
        result = fit_contour(np.c_[x, np.sin(5*x)], closed=False,
                             tolerance_mm=1e-5, max_control_points=4, max_iterations=3)
        self.assertFalse(result["success"])
        self.assertEqual(result["error"]["code"], "CONTROL_POINT_LIMIT")
        json.dumps(result, allow_nan=False)

    def test_removed_points_still_participate_in_acceptance(self):
        # 毛刺点即使被用户设置的去重阈值吞并，也不能从误差验收中消失。
        points = [[0, 0], [.05, .05], [1, 0], [2, 0], [3, 0]]
        result = fit_contour(points, closed=False, tolerance_mm=.001,
                             duplicate_tolerance_mm=.1, max_iterations=3)
        self.assertFalse(result["success"])
        self.assertGreater(result["maximum_deviation_mm"], .001)

    def test_profile_segments_remain_open_and_failures_are_preserved(self):
        line = {"segment_id": "edge_1", "points": [[0, 0], [1, 0], [2, 0], [3, 0]]}
        result = fit_profile({"closed": True, "complete": False, "segments": [line]})
        self.assertTrue(result["success"])
        self.assertFalse(result["items"][0]["fit"]["closed"])
        self.assertFalse(result["profile_complete"])
        section = fit_section({"unit": "mm", "section_id": "s1", "contours": [
            {**line, "closed": False}, {"closed": False, "points": []}]})
        self.assertFalse(section["success"])
        self.assertTrue(section["items"][0]["fit"]["success"])
        self.assertEqual(section["items"][1]["fit"]["error"]["code"], "INVALID_CONTOUR")
        self.assertFalse(fit_profile({"segments": []})["success"])

    def test_saved_json_round_trip_and_parameter_range(self):
        result = fit_contour([[0, 0], [1, 1], [2, 1], [3, 0]], closed=False)
        # 在项目可写目录内建立临时目录，退出时自动清理。
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            path = save_fit_result(result, Path(directory)/"fit.json")
            loaded = json.loads(path.read_text(encoding="utf-8"))
            np.testing.assert_allclose(evaluate_bspline(loaded["bspline"], [.25, .75]),
                                       evaluate_bspline(result["bspline"], [.25, .75]))
        with self.assertRaises(ValueError):
            evaluate_bspline(result["bspline"], [-.1])


if __name__ == "__main__":
    unittest.main()
