import json
from pathlib import Path
import tempfile
import unittest
import numpy as np
from agent.loop import AgentLoop,ToolExecution
from rim_reconstruction.geometry.window_regions import classify_pattern,window_candidates,select_side_regions
from rim_reconstruction.geometry.window_regions import coherent_surface_patch
from rim_reconstruction.services.window_service import WindowWorkerAdapter,_save,_hash

ROOT=Path(__file__).resolve().parents[1]
EXE=ROOT/'stlload_hub/build_surface/closed_cutter/geometry_worker.exe'


class WindowGeometryTests(unittest.TestCase):
    def test_actual_section_seed_beats_larger_unrelated_patch(self):
        tri=np.array([[[0,0,0],[1,0,0],[1,1,0]],[[0,0,0],[1,1,0],[0,1,0]],
                      [[100,0,0],[120,0,0],[120,20,0]],[[100,0,0],[120,20,0],[100,20,0]]],float)
        selected,quality=coherent_surface_patch(tri,np.arange(4),seed_indices=np.array([0,1]))
        self.assertEqual(selected.tolist(),[0,1])
        self.assertEqual(quality['seed_triangle_count'],2)
    def test_recorded_pockets_are_not_second_spoke_type(self):
        from rim_reconstruction.geometry.window_regions import partition_window_candidates
        windows=json.loads((ROOT/'tests/fixtures/recorded_window_descriptors.json').read_text())
        kept,excluded=partition_window_candidates(windows)
        self.assertEqual(len(kept),10)
        self.assertEqual(len(excluded),10)
        pattern=classify_pattern(windows)
        self.assertEqual(pattern['status'],'hypotheses')
        self.assertEqual(sorted(len(g['window_ids']) for g in pattern['groups']),[5,5])
        self.assertTrue(all(g['period_degrees']==72 for g in pattern['groups']))
        self.assertNotIn('window_015',{i for g in pattern['groups'] for i in g['window_ids']})

    def test_irregular_group_requires_review(self):
        windows=[{'window_id':str(i),'angle_degrees':a,'area_mm2':10.,'radial_span_mm':5.,
                  'tangential_span_mm':2.,'radius_mm':10.,'supporting_depths_mm':[0]} for i,a in enumerate([0,20,40,180])]
        self.assertEqual(classify_pattern(windows,1)['status'],'needs_review')

    def test_span_difference_alone_does_not_discard_a_window_type(self):
        from rim_reconstruction.geometry.window_regions import partition_window_candidates
        windows=[{'radial_span_mm':span,'area_mm2':100} for span in [10,10,10,30,30,30]]
        kept,excluded=partition_window_candidates(windows)
        self.assertEqual(len(kept),6);self.assertEqual(excluded,[])
    def test_disconnected_and_opposite_walls_not_fitted_together(self):
        tri=np.array([[[0,0,0],[2,0,0],[2,2,0]],[[0,0,0],[2,2,0],[0,2,0]],
            [[20,20,0],[21,20,0],[20,21,0]],[[0,0,1],[0,2,1],[2,2,1]]],float)
        ids,quality=coherent_surface_patch(tri,np.arange(4))
        self.assertEqual(ids.tolist(),[0,1]);self.assertEqual(quality['excluded_triangle_count'],2)
    def test_rectangular_window_detected_and_four_wall_regions_selected(self):
        outer={'closed':True,'area_mm2':400.,'points_uv':[[-10,-10],[10,-10],[10,10],[-10,10]]}
        hole={'closed':True,'area_mm2':4.,'points_uv':[[3,-1],[5,-1],[5,1],[3,1]]}
        sections=[{'depth_mm':0.,'contours':[outer,hole],
            'frame':{'u_direction':[1,0,0],'v_direction':[0,1,0],'normal':[0,0,1],'origin':[0,0,0]}}]
        windows=window_candidates(sections,10)
        self.assertEqual(len(windows),1)
        triangles=[]
        for side in range(4):
            def p(t,z):return [3,t,z] if side==0 else [4+t,1,z] if side==1 else [5,t,z] if side==2 else [4+t,-1,z]
            for t in np.linspace(-1,.5,4):
                for z in np.linspace(-1,.5,4):
                    a,b,c,d=p(t,z),p(t+.5,z),p(t+.5,z+.5),p(t,z+.5)
                    triangles.extend([[a,b,c],[a,c,d]])
        regions=select_side_regions(np.asarray(triangles),windows[0],{'origin':[0,0,0],'direction':[0,0,1]},.3)
        self.assertEqual(len(regions),4)
        self.assertTrue(all(r['status']=='candidate' for r in regions))
        narrow=select_side_regions(np.asarray(triangles),windows[0],{'origin':[0,0,0],'direction':[0,0,1]},.3,axial_band_mm=.3)
        self.assertTrue(all(r['triangle_count'] for r in narrow))
        for region in narrow:
            self.assertTrue(np.all(abs(np.asarray(triangles)[region['triangle_indices']].mean(axis=1)[:,2])<=.3))

    def test_two_alternating_types_have_independent_phase_and_period(self):
        windows=[]
        for i in range(10):
            windows.append({'window_id':str(i),'angle_degrees':i*36.,'area_mm2':10. if i%2 else 30.,
                'radial_span_mm':5.,'tangential_span_mm':2. if i%2 else 4.,'radius_mm':10.,'supporting_depths_mm':[0,1]})
        pattern=classify_pattern(windows)
        self.assertEqual(pattern['status'],'hypotheses')
        self.assertEqual([len(g['angles_degrees']) for g in pattern['groups']],[5,5])
        self.assertTrue(all(abs(g['period_degrees']-72)<1e-9 for g in pattern['groups']))

    def test_identical_windows_not_forced_into_two_types(self):
        windows=[{'window_id':str(i),'angle_degrees':i*90.,'area_mm2':10.,'radial_span_mm':5.,'tangential_span_mm':2.,'radius_mm':10.,'supporting_depths_mm':[0]} for i in range(4)]
        self.assertEqual(classify_pattern(windows)['status'],'needs_review')


@unittest.skipUnless(EXE.is_file(),'Build the surface worker first')
class SurfaceWorkerTests(unittest.TestCase):
    def test_three_main_fitted_surfaces_form_a_solid_without_a_fake_fourth_wall(self):
        vertices=np.array([[3.,-1.],[5.,0.],[3.,1.]])
        reports=[]
        for side in range(3):
            first=vertices[side];last=vertices[(side+1)%3]
            points=[[*(first+(last-first)*t),z] for t in np.linspace(0,1,5) for z in np.linspace(-.5,.5,5)]
            path=self.root/f'triangle{side}.json';_save(path,{'samples':points,'region_id':str(side),'source_sha256':'test'})
            result=self.adapter.run({'schema_version':1,'operation':'fit_window_surface','task_id':'test','unit':'mm',
                'stl_path':'mesh.stl','samples_path':str(path),'tolerance_mm':.05})
            reports.append(next(a['path'] for a in result.result['artifacts'] if a['type']=='window_report'))
        result=self.adapter.run({'schema_version':1,'operation':'build_window_cutter','task_id':'test','unit':'mm',
            'stl_path':'mesh.stl','surface_reports':reports,'axis':{'origin':[0,0,0],'direction':[0,0,1]},
            'minimum_depth_mm':-3.,'maximum_depth_mm':3.,'tolerance_mm':.05,'fillet_radius_mm':0.})
        self.assertEqual(result.result['metrics']['fitted_side_surface_count'],3)
        self.assertEqual(result.result['metrics']['cap_face_count'],2)
        self.assertAlmostEqual(result.result['metrics']['volume_mm3'],12.,places=4)
    def test_increased_budget_keeps_fixed_tolerance(self):
        from rim_reconstruction.window_tools import schemas
        definition=next(d['function']['parameters']['properties'] for d in schemas() if d['function']['name']=='rim_fit_surface_patch')
        self.assertEqual(definition['max_iterations']['maximum'],64)
        self.assertEqual(definition['max_iterations']['default'],16)
        self.assertEqual(definition['tolerance_mm']['maximum'],1.)
        self.assertEqual(definition['tolerance_mm']['default'],.5)
        self.assertIn('retry_from_call_directory',definition)
    def test_utf8_fit_error_contains_diagnostics(self):
        from rim_reconstruction.adapters.cpp_worker_adapter import WorkerError
        path=self.root/'invalid_samples.json';_save(path,{'samples':[[0,0,0]]*9,'region_id':'bad','source_sha256':'test'})
        with self.assertRaises(WorkerError) as caught:
            self.adapter.run({'schema_version':1,'operation':'fit_window_surface','task_id':'test','unit':'mm',
                'stl_path':'missing.stl','samples_path':str(path),'tolerance_mm':.08,'max_iterations':16})
        self.assertEqual(caught.exception.code,'SURFACE_FIT_FAILED')
        self.assertIn('至少',str(caught.exception))
        self.assertEqual(caught.exception.worker_result['error']['details']['max_iterations'],16)
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'runtime');self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()
        self.adapter=WindowWorkerAdapter(EXE,self.root/'calls',base_directory=ROOT,timeout_seconds=300)

    def build_cutter(self,width=1.,boundary_tolerance=None):
        paths=[]
        for side in range(4):
            pts=[]
            for t in np.linspace(-1,1,5):
                for z in np.linspace(-.5,.5,5):
                    pts.append(([3,t*width,z] if side==0 else [4+t,width,z] if side==1 else [5,t*width,z] if side==2 else [4+t,-width,z]))
            path=self.root/f'points{side}.json';_save(path,{'samples':pts,'region_id':str(side),'source_sha256':'test'})
            response=self.adapter.run({'schema_version':1,'operation':'fit_window_surface','task_id':'test','unit':'mm',
                'stl_path':str(self.root/'source.stl'),'samples_path':str(path),'tolerance_mm':.05})
            paths.append(next(a['path'] for a in response.result['artifacts'] if a['type']=='window_report'))
        request={'schema_version':1,'operation':'build_window_cutter','task_id':'test','unit':'mm',
            'stl_path':str(self.root/'source.stl'),'surface_reports':paths,'axis':{'origin':[0,0,0],'direction':[0,0,1]},
            'minimum_depth_mm':-3.,'maximum_depth_mm':3.,'tolerance_mm':.05,'fillet_radius_mm':0.}
        if boundary_tolerance is not None:request['boundary_tolerance_mm']=boundary_tolerance
        result=self.adapter.run(request)
        return result

    def test_real_four_face_fit_and_capped_solid(self):
        result=self.build_cutter()
        self.assertTrue(result.result['metrics']['closed_solid'])
        self.assertGreater(result.result['metrics']['volume_mm3'],0)
        self.assertEqual(result.result['metrics']['method'],'four_fitted_surface_intersection')
        self.assertEqual(result.result['metrics']['fitted_side_face_count'],4)
        self.assertEqual(result.result['metrics']['cap_face_count'],2)
        self.assertLessEqual(result.result['metrics']['maximum_boundary_sample_deviation_mm'],.05)

    def test_boundary_tolerance_does_not_change_surface_fit_tolerance(self):
        metrics=self.build_cutter(boundary_tolerance=1.).result['metrics']
        self.assertEqual(metrics['fit_tolerances_mm'],[.05]*4)
        self.assertEqual(metrics['boundary_tolerances_mm'],[1.]*4)

    def test_missing_region_fails_without_partial_success(self):
        from rim_reconstruction.adapters.cpp_worker_adapter import WorkerError
        with self.assertRaises(WorkerError):
            self.adapter.run({'schema_version':1,'operation':'fit_window_surface','task_id':'test','unit':'mm',
                'stl_path':'missing.stl','samples_path':str(self.root/'absent.json'),'tolerance_mm':.05})


class WindowRoutingTests(unittest.TestCase):
    def test_spoke_workflow_exposes_tools_and_requires_final_cut(self):
        from agent.loop import AgentLoop,ToolExecution
        from agent.controller import AgentController
        from core.session import ChatSession
        loop=AgentLoop(AgentController(ChatSession()),'',workflow='spoke_reconstruction')
        action=loop.start('生成两类切削体并切出辐条')
        names={t['function']['name'] for t in action.tools}
        self.assertTrue({'rim_fit_surface_patch','rim_build_window_cutting_solid','rim_cut_spoke_windows'}<=names)
        self.assertFalse(loop._check_quality_gate()[0])
        result={'status':'success','operation':'cut_spoke_windows','solid_count':1,'volume_mm3':100,'object_name':'cut'}
        loop.handle_tool_results([ToolExecution('rim_cut_spoke_windows','{}','id','',json.dumps(result),False)])
        self.assertTrue(loop._check_quality_gate()[0])
        loop.handle_tool_results([ToolExecution('rim_cut_spoke_windows','{}','id','','{}',True)])
        self.assertFalse(loop._check_quality_gate()[0])


try:
    import FreeCAD as App
    import Part
except ImportError:
    App=None


@unittest.skipUnless(App is not None and EXE.is_file(),'Needs FreeCAD and surface worker')
class WindowCADTests(SurfaceWorkerTests):
    def test_two_cutters_pattern_boolean_save_reopen(self):
        from rim_reconstruction.services.window_cad_service import import_window_cutter,create_window_pattern,cut_window_patterns
        # Real C++ fits and capped solids with two different window widths.
        source=self.root/'source.stl';source.write_text('solid test\nendsolid test',encoding='ascii')
        self.adapter.call_root.mkdir(exist_ok=True)
        analysis=self.adapter.call_root/'windows.json'
        _save(analysis,{'algorithm':'window_regions_v2','source_stl':str(source),'source_sha256':_hash(source),
            'axis':{'origin':[0,0,0],'direction':[0,0,1]}})
        analysis.with_suffix('.sha256').write_text(_hash(analysis),encoding='ascii')
        doc=App.newDocument('WindowTests');doc_name=doc.Name;self.addCleanup(lambda:App.closeDocument(doc_name) if doc_name in App.listDocuments() else None)
        body=doc.addObject('PartDesign::Body','Blank');feature=body.newObject('PartDesign::Feature','Base')
        feature.Shape=Part.makeCylinder(10,2,App.Vector(-1,0,0),App.Vector(1,0,0))
        body.Placement=App.Placement(App.Vector(0,0,0),App.Rotation(App.Vector(1,0,0),App.Vector(0,0,1)))
        fit=self.root/'fits'/'fit.json';fit.parent.mkdir();fit.write_text('{}',encoding='ascii')
        _save(self.root/'request.json',{'axis':{'origin':[0,0,0],'direction':[0,0,1]}})
        feature.addProperty('App::PropertyString','SourceFitPath');feature.SourceFitPath=str(fit);doc.recompute()
        patterns=[]
        for i,width in enumerate((1.,.5)):
            response=self.build_cutter(width)
            report=next(a['path'] for a in response.result['artifacts'] if a['type']=='window_report')
            path=Path(report)
            _save(path.parent/'cutter_provenance.json',{'analysis_path':str(analysis),'window_id':str(i),
                'report_sha256':_hash(path),'geometry_sha256':_hash(path.parent/'geometry.brep')})
            imported=import_window_cutter({'cutter_report':report,'document_name':doc.Name},adapter=self.adapter)
            angles=[i*45+k*90 for k in range(4)]
            pattern=create_window_pattern({'document_name':doc.Name,'blank_name':body.Name,'cutter_name':imported['cutter_name'],'angles_degrees':angles})
            patterns.append(pattern['pattern_name'])
        result=cut_window_patterns({'document_name':doc.Name,'blank_name':body.Name,'pattern_names':patterns})
        self.assertEqual(result['solid_count'],1)
        self.assertGreater(result['removed_volume_mm3'],0)
        expected=result['volume_mm3'];path=self.root/'wheel.FCStd';doc.saveAs(str(path));name=doc.Name;App.closeDocument(name)
        reopened=App.openDocument(str(path));self.addCleanup(lambda:App.closeDocument(reopened.Name) if reopened.Name in App.listDocuments() else None)
        reopened.recompute()
        self.assertAlmostEqual(reopened.getObject(result['object_name']).Shape.Volume,expected,places=5)
        self.assertTrue(reopened.getObject(result['object_name']).Shape.isValid())
