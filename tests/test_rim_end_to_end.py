"""合成回转 STL → 真实 Worker → 筛选/拟合 → 实际 FreeCAD 特征树。

选择候选的模型决策使用确定性测试替身，不发起网络调用。
"""
import json
import math
from pathlib import Path
import tempfile
import unittest
import sys
from unittest.mock import patch

from tests.test_freecad_rim_builder import App


@unittest.skipIf(App is None, "需要 FreeCAD 自带 Python 及 NumPy/SciPy")
class EndToEndTests(unittest.TestCase):
    def test_real_geometry_pipeline(self):
        # FreeCAD 初始化会添加已安装工作台路径；明确测试当前源码副本。
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from agent.controller import AgentController
        from agent.loop import AgentLoop, ToolExecution
        from core.session import ChatSession
        from agent.tools import dispatch_tool
        from rim_reconstruction.adapters.cpp_worker_adapter import CppWorkerAdapter
        configured = CppWorkerAdapter.from_config()
        if not configured.executable.is_file():
            self.skipTest("需要 geometry_worker.exe")
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            root = Path(directory)
            stl = root / "continuous_disc.stl"
            lines = ["solid continuous_disc"]
            # Connected hub/web/rim half-section with a central bore.
            profile = [(0.,1.),(2.,1.),(2.,8.),(5.,8.),(5.,10.),
                       (-5.,10.),(-5.,8.),(0.,8.)]
            points = [[(r*math.cos(i*2*math.pi/128), r*math.sin(i*2*math.pi/128), a)
                       for a,r in profile] for i in range(128)]
            for i in range(128):
                for j in range(len(profile)):
                    a, b = points[i][j], points[(i+1)%128][j]
                    c, d = points[(i+1)%128][(j+1)%len(profile)], points[i][(j+1)%len(profile)]
                    for triangle in ((a,b,c), (a,c,d)):
                        lines.extend(["facet normal 0 0 0", "outer loop"])
                        lines.extend("vertex %.16g %.16g %.16g" % p for p in triangle)
                        lines.extend(["endloop", "endfacet"])
            stl.write_text("\n".join(lines + ["endsolid continuous_disc"]), encoding="utf-8")
            adapter = CppWorkerAdapter(configured.executable, root / "calls")
            loop = AgentLoop(AgentController(ChatSession()), "", workflow="rim_reconstruction")
            loop.start("自动筛选、拟合并创建回转实体")

            def execute(name, arguments):
                raw = json.dumps(arguments)
                self.assertIsNone(loop._rim_pipeline.validate_call(name, raw))
                result = json.loads(dispatch_tool(name, raw))
                self.assertEqual(result["status"], "success", result)
                loop.handle_tool_results([ToolExecution(name, raw, name, "", json.dumps(result), False)])
                return result

            with patch.object(CppWorkerAdapter, "from_config", return_value=adapter):
                # 验证启动 Worker 后，宿主的 DLL 搜索目录没有被永久改变。
                import ctypes
                before = ctypes.create_unicode_buffer(32768)
                ctypes.windll.kernel32.GetDllDirectoryW(len(before), before)
                inspected = execute("rim_inspect_stl", {"stl_path": str(stl)})
                estimated = execute("rim_estimate_axis", {"inspection_call_directory": inspected["call_directory"]})
                self.assertFalse(estimated["axis_validated"])
                sections = execute("rim_process_sections_auto", {"axis_call_directory": estimated["call_directory"],
                    "angles_degrees": [0,47,103,169],
                    "angle_selection_reason": "合成回转测试的探索角度；决策由测试替身给出，不是生产默认值"})
                after = ctypes.create_unicode_buffer(32768)
                ctypes.windll.kernel32.GetDllDirectoryW(len(after), after)
                self.assertEqual(before.value, after.value)
                analysis = execute("rim_analyze_candidates", {"call_directory": sections["call_directory"],
                    "consistency_tolerance_mm": .03})
                self.assertEqual(analysis["analysis_status"], "candidates_found")
                candidate = next(c for c in analysis["candidates"] if c["complete"] and c["fit_ready"])
                fit = execute("rim_fit_profile", {"analysis_path": analysis["analysis_path"],
                    "candidate_id": candidate["candidate_id"],
                    "segment_ids": [s["segment_id"] for s in candidate["segments"]], "tolerance_mm": .01})
                built = execute("rim_build_revolution", {"fit_path": fit["result_path"]})
            try:
                self.assertTrue(loop._check_quality_gate()[0])
                doc = App.getDocument(built["document_name"])
                rev = doc.getObject(built["revolution_name"])
                self.assertTrue(rev.Shape.isValid())
                self.assertEqual(len(rev.Shape.Solids), 1)
                self.assertEqual(doc.getObject(built["sketch_name"]).TypeId, "Sketcher::SketchObject")
                stored_fit=json.loads(Path(fit["result_path"]).read_text(encoding="utf-8"))
                sketch=doc.getObject(built["sketch_name"])
                roles=[]
                for index,item in enumerate(stored_fit["items"]):
                    role=item.get("source",{}).get("line_role")
                    if role:
                        geometry=sketch.Geometry[index]
                        self.assertEqual(type(geometry).__name__,"LineSegment")
                        roles.append(role)
                        if role.startswith("center_cap"):
                            self.assertAlmostEqual(geometry.StartPoint.x,geometry.EndPoint.x,places=10)
                            self.assertTrue(any(c.Type=="Vertical" and c.First==index for c in sketch.Constraints))
                self.assertEqual(set(roles),{"center_cap_low","center_cap_high","axis_closure"})
                # Bore sealed: web r=0..8, width=2; cavity outside web retained.
                expected_volume = math.pi*(8**2*2+(10**2-8**2)*10)
                self.assertAlmostEqual(rev.Shape.Volume/expected_volume, 1., delta=.002)
                body = doc.getObject(built["body_name"])
                request = json.loads((Path(sections["call_directory"])/"request.json").read_text(encoding="utf-8"))
                expected_axis = App.Vector(*request["axis"]["direction"])
                actual_axis = body.Placement.Rotation.multVec(App.Vector(1, 0, 0))
                self.assertLess((actual_axis-expected_axis).Length, 1e-8)
                axis_point = body.Placement.multVec(App.Vector(1, 0, 0))
                self.assertTrue(body.Shape.isInside(axis_point, 1e-7, True))
                cavity_point = body.Placement.multVec(App.Vector(-3, 4, 0))
                self.assertFalse(body.Shape.isInside(cavity_point, 1e-7, True))
            finally:
                App.closeDocument(built["document_name"])


if __name__ == "__main__":
    unittest.main()
