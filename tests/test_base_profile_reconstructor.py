"""Geometry, evidence, cavity and replay regressions for the base wheel disc."""
import copy
import math
import unittest
import numpy as np

from rim_reconstruction.geometry.base_profile_reconstructor import (
    analyze_base_candidates, _trace, _track_branches, _tracking_start, _recover_surfaces)


def wheel_sections(features=None, angles=(0, 47, 103, 169)):
    result = []
    for i, angle in enumerate(angles):
        low = [[0, 1], [0, 2]]
        value = (features or {}).get(i, 0)
        if value:
            low += [[value, 2], [value, 4], [0, 4]]
        low += [[0, 8]]
        # Real annular rim notch near r=9.5, protected from central processing.
        p = np.asarray(low+[[ -5,8],[-5,10],[5,10],[5,9.5],[4.5,9.5],[4.5,9],
                            [5,9],[5,8],[2,8],[2,1]], dtype=float)
        radial = np.array([math.cos(math.radians(angle)), math.sin(math.radians(angle)), 0.])
        xyz = p[:, :1]*[0,0,1]+p[:, 1:]*radial
        result.append({"section_id":f"s{i}", "unit":"mm", "angle_degrees":angle,
            "frame":{"origin":[0,0,0],"axis_direction":[0,0,1],"radial_direction":radial.tolist(),"half_plane":True},
            "contours":[{"contour_index":0,"closed":True,"points":xyz.tolist()}]})
    return result


def boundary(candidate):
    # All source segments share corner endpoints. Reassemble ordered boundary.
    return np.asarray([p for s in candidate["segments"] for p in s["points_ar"][:-1]])


class BaseReconstructionTests(unittest.TestCase):
    def test_sloped_observations_have_independent_flat_center_caps(self):
        data=wheel_sections()
        for section in data:
            radial=np.asarray(section["frame"]["radial_direction"])
            points=np.asarray(section["contours"][0]["points"])
            radius=points @ radial
            points[:,2] += .015*radius**2
            section["contours"][0]["points"]=points.tolist()
        result=analyze_base_candidates(data,{})
        self.assertEqual(result["status"],"candidates_found",result["reason"])
        from rim_reconstruction.geometry.bspline_fitter import fit_profile, evaluate_bspline
        for c in result["candidates"]:
            self.assertEqual(c["reconstruction"]["center_policy"],"planar_radial_lines_to_axis")
            lines=[s for s in c["segments"] if s.get("geometry_kind")=="line"]
            self.assertEqual({s["line_role"] for s in lines},{"center_cap_low","center_cap_high","axis_closure"})
            for s in lines:
                self.assertEqual(len(s["points_ar"]),2)
                p=np.asarray(s["points_ar"])
                if s["line_role"]!="axis_closure":
                    self.assertEqual(p[0,0],p[1,0])
                else:
                    np.testing.assert_array_equal(p[:,1],[0.,0.])
                fit=fit_profile({"segments":[{**s,"points":s["points_ar"]}]})
                self.assertTrue(fit["success"],fit)
                spline=fit["items"][0]["fit"]["bspline"]
                self.assertEqual(spline["degree"],1)
                np.testing.assert_allclose(evaluate_bspline(spline,np.linspace(0,1,11)),np.linspace(p[0],p[1],11),atol=1e-12)

    def test_local_four_intersection_event_preserves_neighboring_observations(self):
        radii=np.linspace(1,10,129)
        hits=[np.array([0.,2.]) for _ in radii]
        hits[50]=np.array([0.,.5,1.,2.])
        observed,events=_track_branches(hits,radii,.08)
        self.assertEqual(len(events),1)
        self.assertTrue(np.isnan(observed[:,50]).all())
        np.testing.assert_allclose(observed[:,49],[0,2])
        np.testing.assert_allclose(observed[:,51],[0,2])
        clean=np.tile([[0.],[2.]],(1,len(radii)))
        recovered,edits=_recover_surfaces(np.array([observed,clean,clean]),radii,[0,47,103],.08)
        np.testing.assert_allclose(recovered[0],clean,atol=1e-12)
        self.assertTrue(edits[0])

    def test_bore_transition_is_separated_from_observed_surfaces(self):
        radii=np.linspace(27.34,135.28,129)
        hits=[np.array([0.,4.]) for _ in radii]
        hits[2]=np.array([0.,1.,3.,4.])
        hits[3]=np.array([0.,1.,3.,4.])
        start=_tracking_start([hits,hits],radii)
        self.assertEqual(start,4)
        self.assertLess(radii[start],32)

    def test_unobserved_local_branch_cannot_be_filled_by_material_union(self):
        radii=np.linspace(1,10,129)
        hits=[np.array([0.,2.]) for _ in radii]
        hits[50]=np.array([0.,.5,1.,2.])
        observed,_=_track_branches(hits,radii,.08)
        with self.assertRaises(ValueError):
            _recover_surfaces(np.array([observed,observed,observed]),radii,[0,47,103],.08)

    def test_long_multilayer_band_still_requires_review(self):
        radii=np.linspace(1,10,129)
        hits=[np.array([0.,2.]) for _ in radii]
        hits[40:80]=[np.array([0.,.5,1.,2.]) for _ in range(40)]
        with self.assertRaises(ValueError):
            _track_branches(hits,radii,.08)

    def test_pockets_and_bosses_restore_base_not_angular_average(self):
        for features in ({2:.8,3:.8}, {2:-.8,3:-.8}, {1:.8,3:-.8}):
            data = wheel_sections(features)
            original = copy.deepcopy(data)
            result = analyze_base_candidates(data, {})
            self.assertEqual(result["status"], "candidates_found", result)
            self.assertEqual(len(result["candidates"]), 4)
            self.assertTrue(any(c["reconstruction"]["removed_local_features"] for c in result["candidates"]))
            for c in result["candidates"]:
                p = boundary(c)
                np.testing.assert_allclose(_trace(p, [1.,3.,5.]), [[0,0,0],[2,2,2]], atol=1e-8)
                # Rim notch copied, not averaged or convexified.
                self.assertTrue(any(np.linalg.norm(p-[4.5,9.5], axis=1) < 1e-8))
            self.assertEqual(data, original)

    def test_center_filled_but_cavity_retained(self):
        result = analyze_base_candidates(wheel_sections(), {})
        self.assertEqual(result["status"], "candidates_found", result)
        for c in result["candidates"]:
            p = boundary(c)
            self.assertEqual(c["minimum_radius_mm"], 0.)
            np.testing.assert_allclose(_trace(p, [.01,4,8.5]), [[0,0,-5],[2,2,5]], atol=1e-8)

    def test_synchronous_sharp_pockets_require_off_phase_sections(self):
        result = analyze_base_candidates(wheel_sections({i:.8 for i in range(4)}), {})
        self.assertEqual(result["status"], "needs_review")
        self.assertFalse(result["candidates"])

    def test_one_clean_angle_insufficient(self):
        result = analyze_base_candidates(wheel_sections({1:.8,2:.8,3:.8}), {})
        self.assertEqual(result["status"], "needs_review")

    def test_nearby_clean_angles_not_independent_evidence(self):
        result = analyze_base_candidates(wheel_sections({2:.8,3:.8}, angles=(0,1,90,180)), {})
        self.assertEqual(result["status"], "needs_review")

    def test_replay_and_angle_order_independent_geometry(self):
        data = wheel_sections({2:.8,3:-.8})
        a = analyze_base_candidates(data, {})
        self.assertEqual(a, analyze_base_candidates(data, {}))
        b = analyze_base_candidates(list(reversed(data)), {})
        for result in (a,b):
            self.assertEqual(result["status"], "candidates_found", result)
            np.testing.assert_allclose(_trace(boundary(result["candidates"][0]), [3.]), [[0],[2]], atol=1e-8)

    def test_invalid_or_outside_disc_radius(self):
        for params in ({"disc_radius_mm":True},{"disc_radius_mm":float('nan')}, {"feature_tolerance_mm":0}):
            with self.assertRaises(ValueError):
                analyze_base_candidates(wheel_sections(), params)
        self.assertEqual(analyze_base_candidates(wheel_sections(), {"disc_radius_mm":12})["status"], "needs_review")

    def test_multi_layer_center_not_collapsed_into_solid_envelope(self):
        data = wheel_sections()
        # Reentrant axial material path: four intersections at r=3.
        ar = [[0,1],[0,8],[-5,8],[-5,10],[5,10],[5,1],[4,1],[4,4],[2,4],[2,1]]
        for section in data:
            radial=np.array(section["frame"]["radial_direction"])
            p=np.asarray(ar)
            section["contours"][0]["points"]=(p[:,:1]*[0,0,1]+p[:,1:]*radial).tolist()
        result = analyze_base_candidates(data, {})
        self.assertEqual(result["status"], "needs_review")



class RecordedSectionRegressionTests(unittest.TestCase):
    def test_recorded_sections_keep_valid_candidates_and_exclude_bad_rims(self):
        import json
        from pathlib import Path
        root=Path(__file__).resolve().parents[1]/"runtime"/"geometry_calls"
        paths=[sorted((root/call/"geometry").rglob("section_*.json"))
               for call in ("call-zclsq440","call-v43mtdpm")]
        if not all(paths):
            self.skipTest("Recorded user sections are not present in this checkout")
        for files in paths:
            sections=[json.loads(p.read_text(encoding="utf-8-sig")) for p in files]
            result=analyze_base_candidates(sections,{})
            self.assertEqual(result["status"],"candidates_found",result["reason"])
            self.assertEqual({c["angle_degrees"] for c in result["candidates"]},{0,34,187,221,289})
            self.assertGreater(result["diagnostics"]["base_reconstruction"]["tracking_start_radius_mm"],31)

    def test_reported_68_degree_groove_is_not_offered_for_revolution(self):
        import json
        from pathlib import Path
        root=Path(__file__).resolve().parents[1]/"runtime/geometry_calls/call-glf8yodf/geometry"
        files=sorted(root.rglob("section_*.json"))
        if not files:
            self.skipTest("Recorded user sections are not present")
        result=analyze_base_candidates([json.loads(p.read_text(encoding="utf-8-sig")) for p in files],{})
        self.assertEqual(result["status"],"candidates_found",result["reason"])
        self.assertEqual({c["angle_degrees"] for c in result["candidates"]},{75,142,149})
        check=next(c for c in result["diagnostics"]["base_reconstruction"]["rim_consistency"] if c["angle_degrees"]==68)
        self.assertEqual(check["status"],"local_outlier")
        self.assertTrue(any(e["radial_interval_mm"][0]<181<e["radial_interval_mm"][1] for e in check["local_events"]))

