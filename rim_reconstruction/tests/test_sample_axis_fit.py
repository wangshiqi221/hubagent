"""Geometric recovery checks for the sample-only shared-circle axis fit."""

import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from fit_sample_axis import frame, residual_components, solve


class SampleAxisFitTests(unittest.TestCase):
    def setUp(self):
        self.expected = np.array([0.35, -0.6, -0.014, 0.008, -120., 119., 259., 258.5])
        self.origin, self.axis = frame(self.expected)
        radial = np.array([0., 1., 0.])
        radial -= (radial @ self.axis) * self.axis
        radial /= np.linalg.norm(radial)
        tangent = np.cross(self.axis, radial)
        angles = np.deg2rad(np.arange(360) + 0.37)
        ring = np.cos(angles)[:, None] * radial + np.sin(angles)[:, None] * tangent
        self.points = np.vstack([
            self.origin + self.expected[4+s] * self.axis + self.expected[6+s] * ring
            for s in [0, 1]
        ])
        self.sides = np.repeat([0, 1], 360)

    def test_recovers_known_tilted_offset_axis_and_rings(self):
        fitted = solve(self.points, self.sides)
        np.testing.assert_allclose(fitted, self.expected, atol=1e-6, rtol=0)
        self.assertLess(np.max(np.linalg.norm(residual_components(fitted, self.points, self.sides), axis=1)), 1e-6)

    def test_unseen_sectors_use_training_only_parameters(self):
        bins = np.tile(np.arange(360), 2)
        train = (bins // 15) % 2 == 0
        fitted = solve(self.points[train], self.sides[train])
        unseen = np.linalg.norm(residual_components(fitted, self.points[~train], self.sides[~train]), axis=1)
        self.assertEqual(len(unseen), 360)
        self.assertLess(unseen.max(), 1e-6)
        # Perturb only held-out samples: their distances must expose the change.
        modified = self.points[~train] + 0.2 * self.axis
        changed = np.linalg.norm(residual_components(fitted, modified, self.sides[~train]), axis=1)
        np.testing.assert_allclose(changed, 0.2, atol=1e-6, rtol=0)


if __name__ == "__main__":
    unittest.main()
