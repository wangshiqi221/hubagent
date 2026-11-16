"""Reject angle-local rim dents, retain real circumferential details."""
import unittest
import numpy as np
from rim_reconstruction.geometry.rim_consistency import assess_rims
from rim_reconstruction.geometry.base_profile_reconstructor import _intersections


def rim(dent=False):
    wall=[[-5,8]]
    if dent:
        wall += [[-5,8.8],[-3,8.9],[-5,9.0]]
    return np.asarray([[0,1],[0,8]]+wall+[[-5,10],[5,10],[5,9.5],[4.5,9.5],
        [4.5,9],[5,9],[5,8],[2,8],[2,1]],dtype=float)


class RimConsistencyTests(unittest.TestCase):
    def test_local_dent_rejected_with_distributed_angular_evidence(self):
        records=[{"local":rim(i==0)} for i in range(4)]
        result=assess_rims(records,[0,47,103,169],6.5,.08,_intersections)
        self.assertEqual(result[0]["status"],"local_outlier")
        self.assertTrue(result[0]["local_events"])
        self.assertTrue(all(r["status"]=="consistent" for r in result[1:]))

    def test_shared_rim_grooves_are_not_flattened(self):
        records=[{"local":rim(True)} for _ in range(4)]
        result=assess_rims(records,[0,47,103,169],6.5,.08,_intersections)
        self.assertTrue(all(r["status"]=="consistent" for r in result))
        self.assertTrue(all(not r["local_events"] for r in result))

    def test_translation_and_linear_tilt_do_not_create_false_dents(self):
        records=[]
        for i in range(4):
            p=rim();p[:,0]+=i*2+i*.1*p[:,1];records.append({"local":p})
        result=assess_rims(records,[0,47,103,169],6.5,.08,_intersections)
        self.assertTrue(all(r["status"]=="consistent" for r in result))

    def test_single_angle_cannot_verify_rim(self):
        result=assess_rims([{"local":rim()}],[0],6.5,.08,_intersections)
        self.assertEqual(result[0]["status"],"needs_review")
