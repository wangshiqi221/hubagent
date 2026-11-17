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


@unittest.skipIf(App is None,'FreeCAD Python required')
class ClosedLoftTests(unittest.TestCase):
    def test_disc_clearance_opens_inner_remnant_without_cutting_rear_ring(self):
        import Part
        from rim_reconstruction.services.window_cad_service import import_window_cutter,extend_window_cutter_through_disc
        root=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(dir=root/'runtime') as temp:
            base=Path(temp);calls=base/'calls';calls.mkdir();job=calls/'job';job.mkdir()
            source=base/'mesh.stl';source.write_text('solid sample\nendsolid sample')
            axis={'origin':[0,0,0],'direction':[0,0,1],'outer_radius_mm':10}
            analysis=job/'windows.json';_save(analysis,{'algorithm':'window_regions_v2','source_stl':str(source),
                'source_sha256':_hash(source),'axis':axis});analysis.with_suffix('.sha256').write_text(_hash(analysis))
            fit=base/'fits'/'fit.json';fit.parent.mkdir();fit.write_text('{}')
            _save(base/'request.json',{'axis':axis})
            doc=App.newDocument('DiscClearanceTest')
            try:
                blank=doc.addObject('PartDesign::Body','Blank');feature=blank.newObject('PartDesign::Feature','Base')
                cylinder=lambda radius,height:Part.makeCylinder(radius,height,App.Vector(),App.Vector(1,0,0))
                feature.Shape=cylinder(10,2).fuse(cylinder(5,5)).fuse(cylinder(10,12).cut(cylinder(8,12)))
                blank.Placement=App.Placement(App.Vector(),App.Rotation(App.Vector(1,0,0),App.Vector(0,0,1)))
                feature.addProperty('App::PropertyString','SourceFitPath');feature.SourceFitPath=str(fit);doc.recompute()
                cutter=Part.makeBox(4,2,4,App.Vector(3,-1,-1));geometry=job/'geometry.brep';cutter.exportBrep(str(geometry))
                report=job/'geometry.json';_save(report,{'operation':'build_window_cutter','axis':axis,
                    'axial_scope':'observed_front_window_band','required_axial_range_mm':[-1,3],
                    'geometry_path':str(geometry),'volume_mm3':cutter.Volume})
                _save(job/'cutter_provenance.json',{'analysis_path':str(analysis),'window_id':'window_001',
                    'report_sha256':_hash(report),'geometry_sha256':_hash(geometry)})
                adapter=CppWorkerAdapter(root/'stlload_hub/build_surface/closed_cutter/geometry_worker.exe',calls,base_directory=root)
                old=import_window_cutter({'document_name':doc.Name,'cutter_report':str(report)},adapter=adapter)
                ray=Part.makeCylinder(.05,20,App.Vector(4,0,-2))
                self.assertGreater(blank.Shape.cut(cutter).common(ray).Volume,.01)
                new=extend_window_cutter_through_disc({'document_name':doc.Name,'blank_name':blank.Name,
                    'cutter_name':old['cutter_name']},adapter=adapter)
                result=blank.Shape.cut(doc.getObject(new['cutter_name']).Shape)
                self.assertTrue(result.isValid());self.assertEqual(len(result.Solids),1)
                self.assertLess(result.common(ray).Volume,1e-6)
                ring=Part.makeCylinder(10,12).cut(Part.makeCylinder(8,12))
                self.assertAlmostEqual(result.common(ring).Volume,blank.Shape.common(ring).Volume,places=4)
                self.assertTrue(new['rear_clearance']['empty_separator_band_verified'])
            finally:App.closeDocument(doc.Name)

    def test_measured_sections_produce_single_solid_and_verified_provenance(self):
        root=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(dir=root/'runtime') as temp:
            base=Path(temp);calls=base/'calls';calls.mkdir();job=calls/'job';job.mkdir()
            source=base/'mesh.stl';source.write_text('solid sample\nendsolid sample')
            axis={'origin':[0,0,0],'direction':[0,0,1]}
            analysis=job/'windows.json'
            _save(analysis,{'algorithm':'window_regions_v2','task_id':'t','source_stl':str(source),
                'source_sha256':_hash(source),'axis':axis,'axial_range_mm':[-5,5]})
            analysis.with_suffix('.sha256').write_text(_hash(analysis))
            layers=[]
            for depth in [-1,0,1]:
                theta=np.arange(80)*2*np.pi/80
                layers.append({'depth_mm':depth,'boundary_uv':np.c_[6+2*np.cos(theta),1.5*np.sin(theta)].tolist(),
                    'frame':{'origin':[0,0,depth],'u_direction':[1,0,0],'v_direction':[0,1,0]}})
            path=job/'loft_sections.json';_save(path,{'source_sha256':_hash(source),'analysis_path':str(analysis),
                'window_id':'window_001','layers':layers})
            path.with_suffix('.sha256').write_text(_hash(path))
            adapter=CppWorkerAdapter(root/'stlload_hub/build_surface/closed_cutter/geometry_worker.exe',calls,base_directory=root)
            result=build_window_loft_cutter({'loft_path':str(path),'tolerance_mm':.05},adapter=adapter)
            self.assertEqual(result['solid_count'],1)
            self.assertLessEqual(result['maximum_section_deviation_mm'],.05)
            self.assertGreaterEqual(result['maximum_section_deviation_mm'],0)
            self.assertGreater(result['volume_mm3'],0)
            report=Path(result['cutter_report'])
            provenance=json.loads((report.parent/'cutter_provenance.json').read_text())
            self.assertEqual(provenance['geometry_sha256'],_hash(report.parent/'geometry.brep'))
