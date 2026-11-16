"""在 FreeCAD 自带 Python 下执行真实 Sketcher/PartDesign 测试；普通 Python 跳过内核测试。"""
import json
import math
from pathlib import Path
import tempfile
import unittest

from rim_reconstruction.cad.freecad_rim_builder import build_rim_from_fit, spline_parameters

try:
    import FreeCAD as App
    import Part
    import Sketcher
except ImportError:
    App = None


def line_curve(a, b):
    return {"degree": 3, "knot_format": "full_repeated", "periodic": False,
            "control_points": [[a[j] + (b[j] - a[j]) * i / 3 for j in range(2)] for i in range(4)],
            "knots": [0.] * 4 + [1.] * 4, "weights": [1.] * 4, "parameter_range": [0., 1.]}


def periodic_curve():
    poles = [[3 * math.cos(i * math.pi / 4), 10 + 3 * math.sin(i * math.pi / 4)] for i in range(8)]
    poles += poles[:3]
    return {"degree": 3, "knot_format": "full_repeated", "periodic": True,
            "control_points": poles, "knots": list(range(-3, 12)),
            "weights": [1.] * 11, "parameter_range": [0, 8]}


def fit_data(curves):
    return {"schema_version": 1, "success": True,
            "profile": {"unit": "mm", "coordinate_order": ["axial", "radius"],
                        "segments": [{} for _ in curves]},
            "items": [{"fit": {"success": True, "compliant": True, "bspline": c}} for c in curves]}


class ParameterTests(unittest.TestCase):
    def test_repeated_knots_and_invalid_data(self):
        curve = line_curve([0, 8], [4, 8])
        self.assertEqual(spline_parameters(curve)[1:3], ([0., 1.], [4, 4]))
        curve["weights"][0] = 0
        with self.assertRaises(ValueError):
            spline_parameters(curve)


@unittest.skipIf(App is None, "使用 FreeCAD 自带 Python 运行内核测试")
class FreeCADBuilderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def tearDown(self):
        for name in list(App.listDocuments()):
            App.closeDocument(name)

    def write(self, curves):
        path = self.root / "fit.json"
        path.write_text(json.dumps(fit_data(curves)), encoding="utf-8")
        return path

    def test_base_disc_target_cannot_reuse_a_bored_profile(self):
        path = self.write([periodic_curve()])
        data = json.loads(path.read_text(encoding="utf-8"))
        data["profile"].update(target="closed_center_base_disc", complete=True)
        path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(ValueError):
            build_rim_from_fit(path)

    def test_curve_cannot_be_declared_an_exact_line(self):
        path=self.write([periodic_curve()])
        data=json.loads(path.read_text(encoding="utf-8"))
        data["items"][0]["fit"]["geometry_kind"]="line"
        path.write_text(json.dumps(data),encoding="utf-8")
        with self.assertRaises(ValueError):
            build_rim_from_fit(path)

    def test_rectangle_tree_save_reopen_and_angle_recompute(self):
        points = [[0, 8], [4, 8], [4, 10], [0, 10]]
        path = self.write([line_curve(a, b) for a, b in zip(points, points[1:] + points[:1])])
        result = build_rim_from_fit(path)
        doc = App.getDocument(result["document_name"])
        sketch = doc.getObject(result["sketch_name"])
        rev = doc.getObject(result["revolution_name"])
        self.assertEqual(sketch.TypeId, "Sketcher::SketchObject")
        self.assertEqual(rev.TypeId, "PartDesign::Revolution")
        self.assertEqual(sketch.GeometryCount, 4)
        self.assertEqual(rev.Profile[0], sketch)
        self.assertAlmostEqual(rev.Shape.Volume, math.pi * (100 - 64) * 4, places=5)
        volume = rev.Shape.Volume
        rev.Angle = 180
        doc.recompute()
        self.assertTrue(rev.Shape.isValid())
        self.assertAlmostEqual(rev.Shape.Volume, volume / 2, places=5)
        saved = self.root / "rim.FCStd"
        doc.saveAs(str(saved))
        App.closeDocument(doc.Name)
        reopened = App.openDocument(str(saved))
        r = reopened.getObject(result["revolution_name"])
        r.Angle = 360
        reopened.recompute()
        self.assertAlmostEqual(r.Shape.Volume, volume, places=5)
        self.assertEqual(len(r.Shape.Solids), 1)

    def test_periodic_geometry_conversion_and_source_placement(self):
        path = self.write([periodic_curve()])
        result = build_rim_from_fit(path, origin=[3, 5, 7],
                                   axis_direction=[1, 0, 0], radial_direction=[0, 1, 0])
        doc = App.getDocument(result["document_name"])
        rev = doc.getObject(result["revolution_name"])
        sketch = doc.getObject(result["sketch_name"])
        self.assertTrue(sketch.Geometry[0].isClosed())
        self.assertTrue(rev.Shape.isValid())
        self.assertEqual(len(rev.Shape.Solids), 1)
        body = doc.getObject(result["body_name"])
        self.assertAlmostEqual(body.Placement.Base.x, 3)
        before = rev.Shape.Volume
        # 实际编辑草图内部控制点，确认回转依赖链会重新计算。
        geometry = sketch.Geometry
        curve = geometry[0]
        pole = curve.getPole(5)
        curve.setPole(5, pole + App.Vector(0, .2, 0))
        geometry[0] = curve
        sketch.Geometry = geometry
        doc.recompute()
        self.assertTrue(rev.Shape.isValid())
        self.assertNotAlmostEqual(before, rev.Shape.Volume, places=5)

    def test_open_input_does_not_create_document(self):
        path = self.write([line_curve([0, 8], [4, 8])])
        count = len(App.listDocuments())
        with self.assertRaisesRegex(ValueError, "未闭合"):
            build_rim_from_fit(path)
        self.assertEqual(count, len(App.listDocuments()))


if __name__ == "__main__":
    unittest.main()
