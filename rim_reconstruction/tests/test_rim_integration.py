"""统一工具调用链测试，不依赖 LLM 和 FreeCAD。"""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent.tool_registry import ToolRegistry
from agent.tool_setup import create_tool_registry
from agent import tool_dispatch
from adapters.cpp_worker_adapter import CppWorkerAdapter


class RimIntegrationTests(unittest.TestCase):
    def test_registry_and_legacy_dispatch(self):
        registry = ToolRegistry()
        with patch.object(tool_dispatch, "DEFAULT_REGISTRY", registry):
            tool_dispatch.register_tool("legacy", lambda args: json.dumps({"status": "success", **json.loads(args)}))
            self.assertEqual(json.loads(tool_dispatch.dispatch_tool("legacy", '{"angle":30}'))["angle"], 30)
            self.assertEqual(tool_dispatch.available_tools(), ["legacy"])
            self.assertEqual(json.loads(tool_dispatch.dispatch_tool("legacy", '[]'))["error"]["code"], "INVALID_ARGUMENTS")
        with self.assertRaises(ValueError):
            registry.register("legacy", lambda: None)
        self.assertEqual(registry.execute("missing", {})["error"]["code"], "UNKNOWN_TOOL")
        self.assertEqual(registry.execute("legacy", {})["status"], "success")

    def test_real_worker_section_and_error(self):
        adapter = CppWorkerAdapter()
        if not adapter.executable.is_file():
            self.skipTest("Build geometry_worker first")
        parent = ROOT / "runtime/rim_integration_tests"
        parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent) as directory:
            stl = Path(directory) / "四面体 sample.stl"
            vertices = [(0,0,0), (1,0,0), (0,1,0), (0,0,1)]
            lines = ["solid tetra"]
            for face in [(0,1,2), (0,3,1), (0,2,3), (1,3,2)]:
                lines += ["facet normal 0 0 0", "outer loop"]
                lines += ["vertex %s %s %s" % vertices[i] for i in face]
                lines += ["endloop", "endfacet"]
            stl.write_text("\n".join(lines + ["endsolid tetra"]), encoding="utf-8")
            registry = create_tool_registry()
            arguments = {"stl_path": str(stl), "axis_origin": [.2,.2,0],
                         "axis_direction": [0,0,1], "radial_reference": [1,0,0], "angle_deg": 0}
            result = registry.execute("geometry.extract_section", arguments)
            self.assertEqual(result["status"], "success", result)
            artifact = Path(result["artifacts"][0]["path"])
            section = json.loads(artifact.read_text(encoding="utf-8"))
            self.assertGreater(len(section["segments"]), 0)
            failure = registry.execute("geometry.extract_section", {**arguments, "angle_deg": 360})
            self.assertEqual(failure["error"]["code"], "INVALID_ANGLES")
        # Worker 结果位于持久化任务目录，不随输入样本临时目录删除。
        self.assertTrue(artifact.is_file())


if __name__ == "__main__":
    unittest.main()
