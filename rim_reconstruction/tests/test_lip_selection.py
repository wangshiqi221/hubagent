"""Check coordinate projection and streamed extrema selection against geometry."""

import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from inspect_lip_selection import local_coordinates, pick_extrema


class LipSelectionTests(unittest.TestCase):
    def test_translated_tilted_frame_preserves_axial_and_radial_coordinates(self):
        origin = np.array([3., -5., 7.])
        axis = np.array([1., 2., -1.]) / np.sqrt(6)
        radial = np.cross(axis, np.array([0., 0., 1.]))
        radial /= np.linalg.norm(radial)
        points = origin + np.array([-20., 15.])[:,None]*axis + np.array([250., 260.])[:,None]*radial
        np.testing.assert_allclose(local_coordinates(points, origin, axis), [[-20.,250.],[15.,260.]], atol=1e-10)

    def test_chunked_extrema_match_brute_force(self):
        rng = np.random.default_rng(20261105)
        points = rng.normal(size=(150,3))
        bins = np.tile(np.arange(5),30)
        values = points @ (np.array([1.,2.,3.])/np.sqrt(14))
        for minimum in [True,False]:
            limits = np.full(5,np.inf if minimum else -np.inf)
            stored = np.zeros((5,3))
            for start,end in [(0,17),(17,83),(83,150)]:
                pick_extrema(points[start:end],bins[start:end],values[start:end],limits,stored,minimum)
            for b in range(5):
                ids = np.flatnonzero(bins==b)
                expected = ids[np.argmin(values[ids]) if minimum else np.argmax(values[ids])]
                np.testing.assert_array_equal(stored[b],points[expected])
                self.assertEqual(limits[b],values[expected])


if __name__ == "__main__":
    unittest.main()
