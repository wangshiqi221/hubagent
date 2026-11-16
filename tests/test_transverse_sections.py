import json
import struct
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
import numpy as np
from rim_reconstruction.geometry.transverse_sections import load_stl,section_triangles
from rim_reconstruction.services.transverse_service import process_transverse_sections
from rim_reconstruction.tool_handlers import rim_transverse_sections
from rim_reconstruction.adapters.cpp_worker_adapter import WorkerCancelledError


def cube():
    v=np.array([[x,y,z] for x in (0.,1.) for y in (0.,1.) for z in (0.,1.)])
    quads=((0,1,3,2),(4,6,7,5),(0,4,5,1),(2,3,7,6),(0,2,6,4),(1,5,7,3))
    return np.array([v[list(t)] for a,b,c,d in quads for t in ((a,b,c),(a,c,d))])


class TransverseTests(unittest.TestCase):
    def test_section_workflow_finishes_without_claiming_a_solid(self):
        from agent.loop import AgentLoop, ToolExecution
        from agent.controller import AgentController
        from core.session import ChatSession
        loop=AgentLoop(AgentController(ChatSession()),'',workflow='rim_sections')
        loop.start('横向截取截面')
        payload={'status':'success','operation':'transverse_sections','unit':'mm','result_path':'sections.json','sections':[{'depth_mm':0}]}
        loop.handle_tool_results([ToolExecution('rim_transverse_sections','{}','id','',json.dumps(payload),False)])
        self.assertTrue(loop._check_quality_gate()[0])
        loop.handle_tool_results([ToolExecution('rim_transverse_sections','{}','id','','{}',True)])
        self.assertFalse(loop._check_quality_gate()[0])

    def test_cube_closed_area_empty_and_coplanar(self):
        result=section_triangles(cube(),[0,0,0],[0,0,1],[.5,2.,0.])
        for s in (result['sections'][0],result['sections'][2]):
            self.assertEqual(s['status'],'closed_contours')
            self.assertEqual(len(s['contours']),1)
            self.assertAlmostEqual(s['contours'][0]['area_mm2'],1.)
        self.assertEqual(result['sections'][1]['status'],'empty')
        self.assertEqual(result['sections'][2]['coplanar_triangle_count'],2)

    def test_tilted_translated_signed_depth_and_full_plane(self):
        rotation=np.array([[1,0,0],[0,0,-1],[0,1,0.]])
        origin=np.array([10,20,30.]);tri=cube()@rotation.T+origin
        s=section_triangles(tri,origin,rotation[:,2],[.5])['sections'][0]
        self.assertAlmostEqual(s['contours'][0]['area_mm2'],1.)
        xyz=np.array(s['contours'][0]['points_xyz'])
        np.testing.assert_allclose((xyz-origin)@rotation[:,2],.5)

    def test_open_mesh_not_forced_closed(self):
        s=section_triangles(cube()[:2],[0,0,0],[0,0,1],[.5])['sections'][0]
        self.assertEqual(s['status'],'needs_review')
        self.assertFalse(s['contours'][0]['closed'])

    def test_binary_and_ascii_stl(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/"runtime") as folder:
            binary=Path(folder)/'binary.stl';ascii_path=Path(folder)/'ascii.stl'
            binary.write_bytes(b'solid binary'.ljust(80,b' ')+struct.pack('<I',12)+b''.join(struct.pack('<12fH',*([0]*3+list(t.flatten())),0) for t in cube()))
            ascii_path.write_text('solid a\n'+''.join('facet normal 0 0 0\nouter loop\n'+''.join('vertex %s %s %s\n'%tuple(p) for p in t)+'endloop\nendfacet\n' for t in cube())+'endsolid a',encoding='ascii')
            np.testing.assert_allclose(load_stl(binary),cube())
            np.testing.assert_allclose(load_stl(ascii_path),cube())

    def test_boundary_validation_and_cancellation(self):
        for text in ('[]','bad','{"depths_mm":[NaN]}','{"x":1,"x":2}'):
            self.assertEqual(json.loads(rim_transverse_sections(text))['status'],'error')
        for depths in ([True],[.5,.5],[],[float('nan')]):
            with self.assertRaises(ValueError):section_triangles(cube(),[0,0,0],[0,0,1],depths)
        event=threading.Event();event.set()
        with self.assertRaises(WorkerCancelledError):section_triangles(cube(),[0,0,0],[0,0,1],[.5],cancel_event=event)

    def test_service_atomic_output_and_summary(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/"runtime") as folder:
            root=Path(folder);call=root/'axis';call.mkdir();stl=root/'source.stl';stl.write_bytes(b'placeholder')
            request={'unit':'mm','stl_path':str(stl),'task_id':'test'}
            result={'metrics':{'axis_candidate':{'origin':[0,0,0],'direction':[0,0,1]}}}
            with patch('rim_reconstruction.services.transverse_service._load_call',return_value=(request,result)),patch('rim_reconstruction.geometry.transverse_sections.load_stl',return_value=cube()):
                out=process_transverse_sections({'axis_call_directory':str(call),'depths_mm':[.5]},adapter=Mock())
            self.assertEqual(out['sections'][0]['closed_contour_count'],1)
            self.assertNotIn('points_uv',json.dumps(out))
            saved=json.loads(Path(out['result_path']).read_text(encoding='utf-8'))
            self.assertEqual(len(saved['source_sha256']),64)
            self.assertEqual(saved['coordinate_order'],['u','v'])
