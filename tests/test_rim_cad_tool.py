"""实体工具的 JSON 边界、主线程限制及真实 FreeCAD 创建路径。"""
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from rim_reconstruction.tool_handlers import rim_build_revolution
from rim_reconstruction.services.cad_service import build_rim_revolution
from tests.test_freecad_rim_builder import App, fit_data, periodic_curve


class CadToolTests(unittest.TestCase):
    def test_invalid_boundary(self):
        for raw in ("[]", "bad", '{"fit_path":NaN}', '{"x":1,"x":2}', '{"code":"fake"}'):
            self.assertEqual(json.loads(rim_build_revolution(raw))["status"], "error")

    def test_background_thread_refused(self):
        errors = []
        def run():
            try:
                build_rim_revolution({"fit_path": "fake"})
            except RuntimeError as exc:
                errors.append(str(exc))
        thread = threading.Thread(target=run)
        thread.start(); thread.join()
        self.assertEqual(len(errors), 1)
        self.assertIn("主线程", errors[0])

    @unittest.skipIf(App is None, "需要 FreeCAD 自带 Python")
    def test_real_tool_builds_tree(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            root = Path(directory)
            call = root / "call-test"
            (call / "fits").mkdir(parents=True)
            (call / "analyses").mkdir()
            analysis = call / "analyses/analysis_test.json"
            analysis.write_text(json.dumps({"analysis_id": "a1", "task_id": "t1"}), encoding="utf-8")
            (call / "request.json").write_text(json.dumps({"task_id": "t1", "unit": "mm",
                "operation": "process_sections", "axis": {"origin": [0,0,0], "direction": [0,0,1]},
                "radial_reference": [1,0,0]}), encoding="utf-8")
            fit = call / "fits/fit_test.json"
            fit.write_text(json.dumps({**fit_data([periodic_curve()]), "fit_id": "fit_test",
                "analysis_id": "a1", "analysis_path": str(analysis)}), encoding="utf-8")
            adapter = SimpleNamespace(call_root=root, max_artifact_bytes=1024*1024)
            with patch("rim_reconstruction.services.cad_service.CppWorkerAdapter.from_config", return_value=adapter):
                result = json.loads(rim_build_revolution(json.dumps({"fit_path": str(fit)})))
            self.assertEqual(result["status"], "success", result)
            self.assertEqual(result["solid_count"], 1)
            doc = App.getDocument(result["document_name"])
            try:
                self.assertEqual(doc.getObject(result["sketch_name"]).TypeId, "Sketcher::SketchObject")
                self.assertEqual(doc.getObject(result["revolution_name"]).TypeId, "PartDesign::Revolution")
            finally:
                App.closeDocument(doc.Name)


if __name__ == "__main__":
    unittest.main()
