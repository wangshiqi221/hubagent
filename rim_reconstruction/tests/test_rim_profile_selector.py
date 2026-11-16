"""筛选工具的自动半径、连续片段和数据来源验证。"""
import copy
import json
import unittest

import numpy as np

from rim_reconstruction.geometry.rim_profile_selector import analyze_candidates, select_profile
from rim_reconstruction.geometry.bspline_fitter import fit_profile


def sections(outer_closed=True, only_continuous=False):
    result = []
    for angle in [0, 47, 103, 169]:
        theta = np.radians(angle)
        radial = np.array([np.cos(theta), np.sin(theta), 0.])
        profiles = [([[0, 1], [4, 1], [4, 2], [0, 2]], True),
                    ([[0, 8], [4, 8], [4, 10], [0, 10]], outer_closed)]
        if only_continuous:
            profiles = [([[0, 1], [4, 1], [4, 10], [0, 10]], True)]
        contours = []
        for index, (ar, closed) in enumerate(profiles):
            ar = np.asarray(ar)
            xyz = ar[:, 0, None]*np.array([0, 0, 1])+ar[:, 1, None]*radial
            contours.append({"contour_index": index, "closed": closed, "points": xyz.tolist()})
        result.append({"unit": "mm", "section_id": f"s{angle}", "angle_degrees": angle,
                       "frame": {"origin": [0, 0, 0], "axis_direction": [0, 0, 1],
                                 "radial_direction": radial.tolist(), "half_plane": True},
                       "contours": contours})
    return result


class RimProfileSelectorTests(unittest.TestCase):
    def test_single_complete_contour_and_provenance(self):
        source = sections(only_continuous=True)
        before = copy.deepcopy(source)
        result = analyze_candidates(source)
        self.assertEqual(result["status"], "candidates_found")
        np.testing.assert_allclose(result["radial_range_mm"], [1., 10.])
        profile = select_profile(result)
        self.assertEqual(profile["axis"]["direction"], [0, 0, 1])
        self.assertEqual(profile["target"], "continuous_wheel_disc")
        self.assertAlmostEqual(profile["support_ratio"], 1.)
        self.assertEqual(len(profile["segments"]), 4)
        points = source[profile["source_section_index"]]["contours"][0]["points"]
        for segment in profile["segments"]:
            self.assertEqual(segment["points"], [points[i] for i in segment["source_point_indices"]])
            self.assertFalse(segment["closed"])
        self.assertEqual(source, before)
        json.dumps(result, allow_nan=False)
        fitted = fit_profile({**profile, "segments": [{**s, "points": s["points_ar"]} for s in profile["segments"]]})
        self.assertTrue(fitted["success"], fitted)
        from rim_reconstruction.geometry.bspline_fitter import evaluate_bspline
        for segment, item in zip(profile["segments"], fitted["items"]):
            actual = evaluate_bspline(item["fit"]["bspline"], np.linspace(0, 1, 11))
            np.testing.assert_allclose(actual, np.linspace(*segment["points_ar"], 11), atol=1e-10)

    def test_disconnected_hub_and_rim_rejected(self):
        for data in (sections(), sections(outer_closed=False)):
            result = analyze_candidates(data)
            self.assertEqual(result["status"], "needs_review")
            self.assertFalse(result["candidates"])
            with self.assertRaises(ValueError):
                select_profile(result)

    def test_spoke_angle_selected_over_gap_angle(self):
        data = sections()
        data[1] = sections(only_continuous=True)[1]
        result = analyze_candidates(data)
        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(result["candidates"][0]["angle_degrees"], 47)
        self.assertIsNone(result["candidates"][0]["support_ratio"])

    def test_single_angle_complete_contour_sufficient(self):
        self.assertEqual(len(analyze_candidates(sections(only_continuous=True)[:1])["candidates"]), 1)

    def test_matching_tolerance_does_not_allow_disconnected_material(self):
        self.assertFalse(analyze_candidates(sections(), consistency_tolerance_mm=100)["candidates"])

    def test_self_intersection_nonzero_area_rejected(self):
        data = sections(only_continuous=True)[:1]
        data[0]["contours"][0]["points"] = [[r, 0, a] for a, r in [(0,1),(5,10),(0,10),(3,1)]]
        self.assertFalse(analyze_candidates(data)["candidates"])

    def test_invalid_frames_rejected(self):
        for kind in ("angle", "origin", "radial", "unit"):
            data = sections(only_continuous=True)
            if kind == "angle": data[1]["angle_degrees"] = data[0]["angle_degrees"]
            if kind == "origin": data[1]["frame"]["origin"] = [0, 0, 1]
            if kind == "radial": data[1]["frame"]["radial_direction"] = [1, 0, 0]
            if kind == "unit": data[1]["unit"] = "m"
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                analyze_candidates(data)

    def test_ambiguous_complete_contours_rejected(self):
        data = sections(only_continuous=True)[:1]
        data[0]["contours"].append(copy.deepcopy(data[0]["contours"][0]))
        data[0]["contours"][1]["contour_index"] = 1
        self.assertFalse(analyze_candidates(data)["candidates"])

    def test_small_radial_variation_does_not_drop_complete_sections(self):
        data = sections(only_continuous=True)
        radial = np.array(data[1]["frame"]["radial_direction"])
        for p in data[1]["contours"][0]["points"]:
            p[:] = (np.array(p)+radial*.05).tolist()
        self.assertEqual(len(analyze_candidates(data)["candidates"]), 4)

    def test_translated_tilted_axis_is_preserved(self):
        data = sections(only_continuous=True)[:1]
        origin = np.array([10., 20., 30.])
        axis = np.array([1., 1., 0.])/np.sqrt(2)
        radial = np.array([0., 0., 1.])
        section = data[0]
        points = np.asarray(section["contours"][0]["points"])
        section["contours"][0]["points"] = (origin+points[:, 2, None]*axis+points[:, 0, None]*radial).tolist()
        section["frame"].update(origin=origin.tolist(), axis_direction=axis.tolist(), radial_direction=radial.tolist())
        candidate = analyze_candidates(data)["candidates"][0]
        self.assertEqual(candidate["axis"], {"origin": origin.tolist(), "direction": axis.tolist()})
        self.assertAlmostEqual(candidate["minimum_radius_mm"], 1.)
        self.assertAlmostEqual(candidate["maximum_radius_mm"], 10.)

    def test_smooth_closed_boundary_remains_periodic(self):
        data = sections(only_continuous=True)[:1]
        t = np.linspace(0, 2*np.pi, 64, endpoint=False)
        data[0]["contours"][0]["points"] = np.c_[5+4*np.sin(t), np.zeros(64), 2*np.cos(t)].tolist()
        candidate = analyze_candidates(data)["candidates"][0]
        self.assertEqual(len(candidate["segments"]), 1)
        self.assertTrue(candidate["segments"][0]["closed"])
