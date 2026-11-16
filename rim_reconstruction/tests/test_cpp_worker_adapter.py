"""Client failure-boundary tests and real worker integration; no LLM calls."""

import copy
import json
import math
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from adapters.cpp_worker_adapter import (CppWorkerAdapter, WorkerOperationError, WorkerProcessError,
                                    WorkerProtocolError, WorkerTimeoutError)

WORKER = ROOT.parent / "stlload_hub/build/geometry_worker.exe"


class ClientTests(unittest.TestCase):
    def setUp(self):
        parent = ROOT / "runtime/worker_tests"
        parent.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="中文 空格-", dir=parent)
        self.base = Path(self.temp.name).resolve()
        assert self.base.is_relative_to(parent.resolve())
        self.addCleanup(self.temp.cleanup)
        self.stl = self.base / "模型 sample.stl"
        vertices = [(0,0,0),(1,0,0),(0,1,0),(0,0,1)]
        faces = [(0,1,2),(0,3,1),(0,2,3),(1,3,2)]
        data = "solid tetra\n"
        for face in faces:
            data += "facet normal 0 0 0\nouter loop\n"
            data += "".join("vertex %s %s %s\n" % vertices[i] for i in face)
            data += "endloop\nendfacet\n"
        self.stl.write_text(data+"endsolid tetra\n", encoding="utf-8")
        self.client = CppWorkerAdapter(sys.executable, self.base / "calls", base_directory=self.base)
        self.request = {"operation":"inspect_stl", "task_id":"中文任务", "stl_path":self.stl.name, "unit":"mm"}

    def emit(self, command, options, *, change=None, code=0, write_file=True, raw_stdout=None):
        request_path, result_path = Path(command[2]), Path(command[4])
        request = json.loads(request_path.read_text(encoding="utf-8"))
        result = {"schema_version":1,"status":"success","task_id":request["task_id"],
            "operation":request["operation"],"unit":"mm","warnings":[],"artifacts":[],"error":None,
            "metrics":{"triangle_count":4,"node_count":4,"file_size_bytes":self.stl.stat().st_size,
                "source_path":str(self.stl),"bounding_box":{"minimum":[0,0,0],"maximum":[1,1,1],"center":[.5,.5,.5],"diagonal_mm":math.sqrt(3)}}}
        if request["operation"]=="compute_sections":
            result["run_id"]="run-test"
            path=Path(request["output_directory"])/"run-test/section_001.json"
            path.parent.mkdir(parents=True,exist_ok=True)
            section={"schema_version":1,"task_id":request["task_id"],"run_id":"run-test","section_id":"section_001",
                "unit":"mm","angle_degrees":0,"frame":{"origin":[.2,.2,0],"axis_direction":[0,0,1],"radial_direction":[1,0,0],"half_plane":True},
                "tolerances":{"plane_mm":1e-7,"minimum_segment_mm":1e-6,"duplicate_point_mm":1e-7},
                "segments":[{"first":[.2,.2,0],"second":[.4,.2,0],"triangle_index":1}]}
            path.write_text(json.dumps(section),encoding="utf-8")
            result["artifacts"]=[{"artifact_id":"section_001","type":"raw_section","angle_degrees":0,"segment_count":1,"path":str(path)}]
            result["metrics"]["sections"]=[{"section_id":"section_001","angle_degrees":0,"segment_count":1}]
        if change:
            change(result, result_path)
        encoded=json.dumps(result,ensure_ascii=False).encode("utf-8")
        if write_file:
            result_path.write_bytes(encoded)
        options["stdout"].write(encoded if raw_stdout is None else raw_stdout)
        options["stderr"].write(b"diagnostic log\n")
        return subprocess.CompletedProcess(command,code)

    def stub(self, **settings):
        return patch("adapters.cpp_worker_adapter.subprocess.run", side_effect=lambda command,**options:self.emit(command,options,**settings))

    def sections_request(self):
        return {**self.request,"operation":"compute_sections","axis":{"origin":[.2,.2,0],"direction":[0,0,1]},
                "radial_reference":[1,0,0],"angles_degrees":[0]}

    def test_valid_call_is_isolated_preserves_input_and_logs(self):
        before=copy.deepcopy(self.request)
        with self.stub() as launch:
            first=self.client.run(self.request)
            second=self.client.run(self.request)
        self.assertNotEqual(first.call_directory,second.call_directory)
        self.assertEqual(self.request,before)
        sent=json.loads((first.call_directory/"request.json").read_text(encoding="utf-8"))
        self.assertEqual(Path(sent["stl_path"]),self.stl)
        self.assertFalse(launch.call_args.kwargs["shell"])
        self.assertEqual((first.call_directory/"stderr.log").read_text(),"diagnostic log\n")

    def test_success_requires_result_file_not_old_call(self):
        with self.stub():
            old=self.client.run(self.request)
        with self.stub(write_file=False),self.assertRaises(WorkerProtocolError) as caught:
            self.client.run(self.request)
        self.assertNotEqual(caught.exception.call_directory,old.call_directory)

    def test_identity_status_unit_and_metadata_mismatches_rejected(self):
        changes=[lambda r,p:r.update(task_id="old-task"),lambda r,p:r.update(operation="estimate_axis"),
                 lambda r,p:r.update(status="error"),lambda r,p:r.update(unit="inch"),
                 lambda r,p:r["metrics"].update(triangle_count=True),lambda r,p:r["metrics"].update(source_path=str(self.base/"other.stl"))]
        for change in changes:
            with self.subTest(change=change),self.stub(change=change),self.assertRaises(WorkerProtocolError):
                self.client.run(self.request)

    def test_malformed_and_nonfinite_stdout_rejected(self):
        for raw in [b"garbage",b'{}\n{}',b'{"x":1,"x":2}',b'{"x":NaN}',b'{"x":1e9999}']:
            with self.subTest(raw=raw),self.stub(raw_stdout=raw),self.assertRaises(WorkerProtocolError):
                self.client.run(self.request)

    def test_stdout_result_disagreement_rejected(self):
        with self.stub(raw_stdout=b'{}'),self.assertRaises(WorkerProtocolError):
            self.client.run(self.request)

    def test_abnormal_exit_is_never_success(self):
        with self.stub(code=-11),self.assertRaises(WorkerProcessError):
            self.client.run(self.request)
        with self.stub(code=3),self.assertRaises(WorkerProcessError):
            self.client.run(self.request)

    def test_worker_error_preserves_code_and_context(self):
        def change(result,path):
            result.update(status="error",error={"code":"STL_LOAD_FAILED","message":"cannot load"},metrics={},artifacts=[])
        with self.stub(code=3,change=change),self.assertRaises(WorkerOperationError) as caught:
            self.client.run(self.request)
        self.assertEqual(caught.exception.code,"STL_LOAD_FAILED")
        self.assertEqual(caught.exception.returncode,3)
        self.assertTrue((caught.exception.call_directory/"stderr.log").exists())

    def test_sections_valid_and_artifact_tampering_rejected(self):
        request=self.sections_request()
        with self.stub():
            self.client.run(request)
        def artifact_change(kind):
            def change(result,path):
                artifact=result["artifacts"][0]
                data_path=Path(artifact["path"])
                if kind=="escape":
                    artifact["path"]=str(self.stl);return
                if kind=="missing":
                    data_path.unlink();return
                section=json.loads(data_path.read_text())
                if kind=="count": section["segments"]=[]
                if kind=="version_bool": section["schema_version"]=True
                if kind=="angle_bool": section["angle_degrees"]=False
                if kind=="unit": section["unit"]="inch"
                if kind=="frame": section["frame"]["origin"]=[0,0,0]
                if kind=="triangle": section["segments"][0]["triangle_index"]=5
                if kind=="off_plane": section["segments"][0]["first"]=[.2,.3,0]
                if kind=="wrong_half": section["segments"][0]["first"]=[-.2,.2,0]
                if kind=="nonfinite": section["segments"][0]["first"]=[float("nan"),.2,0]
                data_path.write_text(json.dumps(section),encoding="utf-8")
            return change
        for kind in ["escape","missing","count","version_bool","angle_bool","unit","frame","triangle","off_plane","wrong_half","nonfinite"]:
            with self.subTest(kind=kind),self.stub(change=artifact_change(kind)),self.assertRaises(WorkerProtocolError):
                self.client.run(request)

    def test_limits_and_invalid_configuration(self):
        for timeout in [0,-1,float("inf"),True]:
            with self.subTest(timeout=timeout),self.assertRaises(ValueError):
                self.client.run(self.request,timeout_seconds=timeout)
        with self.assertRaises(ValueError):
            self.client.run({**self.request,"output_directory":"outside"})
        limited=CppWorkerAdapter(sys.executable,self.base/"calls",base_directory=self.base,max_result_bytes=10)
        with self.stub(),self.assertRaises(WorkerProtocolError):
            limited.run(self.request)
        missing=CppWorkerAdapter(self.base/"missing.exe",self.base/"calls")
        with self.assertRaises(WorkerProcessError) as caught:
            missing.run(self.request)
        self.assertEqual(caught.exception.code,"WORKER_NOT_FOUND")

    @unittest.skipUnless(WORKER.is_file(),"Build geometry_worker first")
    def test_real_worker_unicode_paths_three_operations_and_repeated_calls(self):
        client=CppWorkerAdapter(WORKER,self.base/"真实调用",base_directory=self.base)
        inspect=client.run(self.request)
        self.assertEqual(inspect.result["metrics"]["triangle_count"],4)
        axis=client.run({**self.request,"operation":"estimate_axis"})
        self.assertIs(axis.result["metrics"]["axis_candidate"]["confirmed"],False)
        first=client.run(self.sections_request())
        second=client.run(self.sections_request())
        self.assertNotEqual(first.result["artifacts"][0]["path"],second.result["artifacts"][0]["path"])
        with self.assertRaises(WorkerOperationError) as caught:
            client.run({**self.request,"stl_path":"missing.stl"})
        self.assertEqual(caught.exception.code,"STL_LOAD_FAILED")
        with self.assertRaises(WorkerOperationError) as caught:
            client.run({**self.sections_request(),"angles_degrees":[360]})
        self.assertEqual(caught.exception.code,"INVALID_ANGLES")

    @unittest.skipUnless(WORKER.is_file(),"Build geometry_worker first")
    def test_processed_contours_and_tampering(self):
        """真实 C++ 处理结果可供 Python 读取，损坏点列和处理参数须被拒绝。"""
        client = CppWorkerAdapter(WORKER, self.base / "processed", base_directory=self.base)
        request = {**self.sections_request(), "operation": "process_sections",
                   "processing": {"enable_micro_bump_cleaning": False}}
        original = copy.deepcopy(request)
        response = client.run(request)
        self.assertEqual(request, original)
        sections = client.load_sections(response)
        self.assertEqual(response.result["artifacts"][0]["type"], "processed_section")
        self.assertTrue(sections[0]["contours"])
        self.assertEqual(sections[0]["processing_metrics"]["repaired_bump_count"], 0)
        client.run({**request, "processing": {"enable_micro_bump_cleaning": True}})
        summary = response.result["metrics"]["sections"][0]
        mutations = [
            lambda s: s["contours"][0].update(point_count=999),
            lambda s: s["contours"][0].update(closed=1),
            lambda s: s["contours"][0]["points"].__setitem__(0, [float("nan"), 0, 0]),
            lambda s: s["processing"].update(enable_micro_bump_cleaning=True),
            lambda s: s["processing"].update(duplicate_tolerance_mm=False),
            lambda s: s["processing_metrics"].update(contour_count=999),
        ]
        for mutate in mutations:
            section = copy.deepcopy(sections[0])
            mutate(section)
            with self.subTest(mutation=mutate), self.assertRaises(ValueError):
                client._validate_processed_section(section, summary, response.result, request)
        # 公开转换入口同样能够发出 process_sections 请求。
        result = client.execute("process_sections", {
            "stl_path": str(self.stl), "axis_origin": [.2, .2, 0],
            "axis_direction": [0, 0, 1], "radial_reference": [1, 0, 0],
            "angle_deg": 0, "processing": {"enable_micro_bump_cleaning": False},
        })
        self.assertEqual(result["status"], "success")
        with self.assertRaises(WorkerOperationError) as caught:
            client.run({**request, "processing": {"enable_micro_bump_cleaning": "false"}})
        self.assertEqual(caught.exception.code, "INVALID_PROCESSING_OPTIONS")

    @unittest.skipUnless(WORKER.is_file(),"Build geometry_worker first")
    def test_real_timeout_does_not_accept_any_partial_result(self):
        client=CppWorkerAdapter(WORKER,self.base/"timeout",base_directory=self.base)
        with self.assertRaises(WorkerTimeoutError) as caught:
            client.run(self.request,timeout_seconds=0.000001)
        self.assertTrue((caught.exception.call_directory/"request.json").exists())


if __name__=="__main__":
    unittest.main()
