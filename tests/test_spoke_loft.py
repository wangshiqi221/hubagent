import json
from pathlib import Path
import tempfile
import unittest
import numpy as np

from rim_reconstruction.geometry.window_regions import polygon_centroid,is_spoke_window,window_candidates
from rim_reconstruction.geometry.symmetry_axis import refine_symmetry_axis
from rim_reconstruction.services.window_service import _save,_hash
from rim_reconstruction.services.window_loft_service import build_window_loft_cutter
from rim_reconstruction.adapters.cpp_worker_adapter import CppWorkerAdapter

try:
    import FreeCAD as App
except ImportError:
    App=None


class SpokeGeometryTests(unittest.TestCase):
    def test_centroid_does_not_depend_on_edge_tessellation(self):
        simple=[[1,1],[5,1],[5,3],[1,3]]
        dense=[[1,1],[2,1],[3,1],[4,1],[5,1],[5,3],[1,3]]
        np.testing.assert_allclose(polygon_centroid(simple),[3,2])
        np.testing.assert_allclose(polygon_centroid(dense),[3,2])

    def test_central_holes_are_rejected_even_without_larger_loops(self):
        window={'boundary_uv':[[40,-5],[55,-5],[55,5],[40,5]],'radial_span_mm':15}
        self.assertFalse(is_spoke_window(window,200))
        window={'boundary_uv':[[60,-10],[190,-10],[190,10],[60,10]],'radial_span_mm':130}
        self.assertTrue(is_spoke_window(window,200))

    def test_axis_registration_recovers_known_fivefold_axis(self):
        points=[]
        for z in np.linspace(-8,8,6):
            theta=np.arange(60)*2*np.pi/60;r=10+np.cos(5*theta)
            points.append(np.c_[r*np.cos(theta),r*np.sin(theta),np.full(60,z)])
        tri=[]
        for i in range(5):
            for j in range(60):
                k=(j+1)%60;a,b,c,d=points[i][j],points[i][k],points[i+1][k],points[i+1][j]
                tri.extend([[a,b,c],[a,c,d]])
        direction=np.array([.012,-.008,1]);direction/=np.linalg.norm(direction)
        axis,report=refine_symmetry_axis(np.asarray(tri),{'origin':[.15,-.12,0],
            'direction':direction.tolist(),'outer_radius_mm':12},5,sample_count=250)
        np.testing.assert_allclose(axis['direction'],[0,0,1],atol=.002)
        np.testing.assert_allclose(axis['origin'][:2],[0,0],atol=.03)
        self.assertTrue(all(c['after_p95_mm']<c['before_p95_mm'] for c in report['checks']))
        self.assertFalse(axis['confirmed'])


class SurfaceOnlyPolicyTests(unittest.TestCase):
    def test_one_mm_fallback_requires_real_same_region_half_mm_failure(self):
        from types import SimpleNamespace
        from rim_reconstruction.services.window_service import _fit_tolerance_fallback
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'runtime') as temp:
            root=Path(temp);call=root/'call';call.mkdir();job=root/'job';job.mkdir()
            samples=job/'samples.json';_save(samples,{'region_id':'wall_1','source_sha256':'mesh'})
            _save(call/'request.json',{'operation':'fit_window_surface','task_id':'t','tolerance_mm':.5,'samples_path':str(samples)})
            failure={'status':'error','error':{'code':'SURFACE_FIT_FAILED','details':{'maximum_deviation_mm':.7}}}
            _save(call/'result.json',failure)
            adapter=SimpleNamespace(call_root=root,max_result_bytes=10000,max_artifact_bytes=10000)
            data={'task_id':'t','source_sha256':'mesh'}
            args={'analysis_path':str(job/'windows.json'),'region_id':'wall_1','retry_from_call_directory':str(call)}
            self.assertEqual(_fit_tolerance_fallback(args,data,adapter,1.),str(call))
            with self.assertRaises(ValueError):_fit_tolerance_fallback({**args,'region_id':'wall_2'},data,adapter,1.)
            with self.assertRaises(ValueError):_fit_tolerance_fallback({k:v for k,v in args.items() if k!='retry_from_call_directory'},data,adapter,1.)
            _save(call/'result.json',{'status':'success'})
            with self.assertRaises(ValueError):_fit_tolerance_fallback(args,data,adapter,1.)

    def test_layered_routes_are_not_available_to_cadagent(self):
        from rim_reconstruction.window_tools import schemas,BACKGROUND,MAIN,DISABLED_RECONSTRUCTION_TOOLS,dispatch_window_tool
        names={d['function']['name'] for d in schemas()}
        self.assertFalse(names & DISABLED_RECONSTRUCTION_TOOLS)
        self.assertFalse((set(BACKGROUND)|set(MAIN)) & DISABLED_RECONSTRUCTION_TOOLS)
        for name in DISABLED_RECONSTRUCTION_TOOLS:
            result=json.loads(dispatch_window_tool(name,'{}'))
            self.assertEqual(result['status'],'error')
            self.assertIn('disabled',result['error']['message'])

    def test_legacy_direct_generators_are_disabled(self):
        from rim_reconstruction.services.window_loft_service import prepare_window_loft,prepare_window_lofts,build_window_loft_cutter
        from rim_reconstruction.services.window_cad_service import extend_window_cutter_through_disc
        for function in [prepare_window_loft,prepare_window_lofts,build_window_loft_cutter,extend_window_cutter_through_disc]:
            with self.assertRaisesRegex(ValueError,'Disabled'):
                function({})
