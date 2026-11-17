"""从现有拟合 JSON 创建可编辑 Sketcher 草图与 PartDesign 回转特征。

必须在 FreeCAD Python 环境的主线程调用。不填补缺口，不重新拟合，
不以 Part::Feature 替代特征树。草图保留自由度以便编辑控制点；
尚未识别轮辋宽度、壁厚等工程尺寸约束。
"""
from __future__ import annotations

import json
import math
from pathlib import Path


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def spline_parameters(curve):
    """将 SciPy 完整重复节点转换成 OCCT 独立节点及重数，不改变几何。"""
    if not isinstance(curve, dict) or curve.get("knot_format") != "full_repeated":
        raise ValueError("需要 full_repeated 节点格式的 B 样条")
    degree = curve.get("degree")
    poles, knots, weights = (curve.get(k) for k in ("control_points", "knots", "weights"))
    if type(degree) is not int or not 1 <= degree <= 25:
        raise ValueError("曲线次数无效")
    if (not isinstance(poles, list) or len(poles) <= degree
            or any(not isinstance(p, list) or len(p) != 2 or not all(_finite(x) for x in p) for p in poles)
            or not isinstance(knots, list) or len(knots) != len(poles) + degree + 1
            or not all(_finite(t) for t in knots)
            or any(b < a for a, b in zip(knots, knots[1:]))
            or not knots[degree] < knots[-degree-1]
            or not isinstance(weights, list) or len(weights) != len(poles)
            or not all(_finite(w) and w == 1 for w in weights)):
        raise ValueError("需要有效的二维非有理样条参数，与当前拟合器输出一致")
    bounds = curve.get("parameter_range")
    if bounds != [knots[degree], knots[-degree-1]]:
        raise ValueError("参数范围与完整节点向量不一致")
    unique, mults = [], []
    for knot in knots:
        if unique and knot == unique[-1]:
            mults[-1] += 1
        else:
            unique.append(knot)
            mults.append(1)
    if any(m > degree + 1 for m in mults) or any(m > degree for m in mults[1:-1]):
        raise ValueError("节点重数无效")
    return poles, unique, mults, degree, weights, bounds


def curve_from_parameters(parameters):
    """精确导入样条。周期 SciPy 系数先按完整非周期表示建立，再裁至有效区间。

    不直接将 SciPy periodic 标志传给 OCCT：两者的周期控制点/节点约定不同。
    裁剪不改变有效区间几何；闭合接缝保留，导入后可编辑控制点。
    """
    import FreeCAD as App
    import Part
    poles, knots, mults, degree, weights, bounds = spline_parameters(parameters)
    curve = Part.BSplineCurve()
    curve.buildFromPolesMultsKnots(
        [App.Vector(a, r, 0) for a, r in poles], mults, knots, False, degree, weights)
    curve.segment(*bounds)
    return curve


def _read_fit(path):
    def reject(value):
        raise ValueError(f"非有限 JSON 数值: {value}")
    data = json.loads(path.read_text(encoding="utf-8-sig"), parse_constant=reject)
    if not isinstance(data, dict) or data.get("schema_version") != 1 or data.get("success") is not True:
        raise ValueError("需要 fitting_service 保存的成功完整拟合结果，不能使用摘要")
    profile = data.get("profile", {})
    if profile.get("unit") != "mm" or profile.get("coordinate_order") != ["axial", "radius"]:
        raise ValueError("拟合坐标必须是毫米 [axial, radius]")
    items = data.get("items")
    if not isinstance(items, list) or not items or len(items) != len(profile.get("segments", [])):
        raise ValueError("拟合片段数量不一致")
    if any(i.get("fit", {}).get("success") is not True or
           i.get("fit", {}).get("compliant") is not True for i in items):
        raise ValueError("存在未通过的拟合片段")
    for item in items:
        if item["fit"].get("geometry_kind") == "line":
            spline = item["fit"]["bspline"]
            if spline.get("degree") != 1 or len(spline.get("control_points", [])) != 2:
                raise ValueError("直线片段必须使用精确一次表示，不能以曲线冒充")
    return data


def _placement(App, source_request, origin, axis_direction, radial_direction):
    """局部 X=轴向、Y=径向；绕草图 H_Axis 回转后与源轴对齐。"""
    supplied = (origin, axis_direction, radial_direction)
    if any(v is not None for v in supplied) and not all(v is not None for v in supplied):
        raise ValueError("自定义放置必须同时提供 origin、axis_direction、radial_direction")
    if origin is None:
        if source_request.is_file():
            request = json.loads(source_request.read_text(encoding="utf-8-sig"))
            if request.get("unit") != "mm" or request.get("operation") != "process_sections":
                raise ValueError("源请求不是毫米处理截面请求")
            origin = request["axis"]["origin"]
            axis_direction = request["axis"]["direction"]
            radial_direction = request["radial_reference"]
        else:
            # 独立 JSON 无源请求时，采用明确的标准坐标系：全局 Z 为轴。
            origin, axis_direction, radial_direction = [0, 0, 0], [0, 0, 1], [1, 0, 0]
    for vector in (origin, axis_direction, radial_direction):
        if not isinstance(vector, (list, tuple)) or len(vector) != 3 or not all(_finite(x) for x in vector):
            raise ValueError("放置参数必须为三个有限数值")
    x, y = App.Vector(*axis_direction), App.Vector(*radial_direction)
    if x.Length <= 1e-12 or y.Length <= 1e-12:
        raise ValueError("轴向和径向不能为零向量")
    x.normalize()
    y.normalize()
    if abs(x.dot(y)) > 1e-8:
        raise ValueError("轴向与径向必须正交")
    return App.Placement(App.Vector(*origin), App.Rotation(x, y, x.cross(y), "ZXY"))


def build_rim_from_fit(fit_path, *, doc=None, origin=None, axis_direction=None,
                       radial_direction=None, join_tolerance_mm=1e-7):
    """创建 Body → RimProfile → RimRevolution（360°）。返回对象名和体积。

    输入为 fits/fit_*.json 完整结果。按文件片段顺序构造单一闭合边界；
    大于连接容差的端点缺口直接拒绝，不补线。默认沿用同次调用的 request.json
    中回转轴；独立文件默认全局 Z 轴，也可显式传入放置。失败回滚文档事务。
    """
    import FreeCAD as App
    import Part
    import Sketcher

    if not _finite(join_tolerance_mm) or not 0 < join_tolerance_mm <= 1e-4:
        raise ValueError("join_tolerance_mm 必须在 (0, 1e-4] mm 内")
    path = Path(fit_path).resolve()
    data = _read_fit(path)
    if data["profile"].get("target") in ("continuous_wheel_disc", "closed_center_base_disc") and not data["profile"].get("complete"):
        raise ValueError("连续轮盘必须使用完整材料截面的全部片段")
    curves = [curve_from_parameters(i["fit"]["bspline"]) for i in data["items"]]
    for index, curve in enumerate(curves):
        next_curve = curves[(index + 1) % len(curves)]
        end = curve.value(curve.LastParameter)
        start = next_curve.value(next_curve.FirstParameter)
        if (end - start).Length > join_tolerance_mm:
            raise ValueError(f"片段 {index} 与下一段未闭合，不能回转为实体；请先构造完整材料截面")
    edges = [c.toShape() for c in curves]
    if any(e.BoundBox.YMin < -join_tolerance_mm for e in edges):
        raise ValueError("轮廓进入负半径区域，不能绕轴重建轮辋")
    wire = Part.Wire(edges)
    if data["profile"].get("target") == "closed_center_base_disc":
        if not any(abs(e.BoundBox.YMin) <= join_tolerance_mm and
                   abs(e.BoundBox.YMax) <= join_tolerance_mm and e.Length > join_tolerance_mm for e in edges):
            raise ValueError("中心封孔缺少轴上的非零闭合边")
    if not wire.isClosed() or not wire.isValid():
        raise ValueError("输入未形成有效单一闭合边界")
    face = Part.Face(wire)
    if not face.isValid() or face.Area <= 1e-12:
        raise ValueError("截面无效、自交或面积为零")
    trial = face.revolve(App.Vector(0, 0, 0), App.Vector(1, 0, 0), 360)
    if not trial.isValid() or len(trial.Solids) != 1 or trial.Volume <= 0:
        raise ValueError("截面回转未得到有效单实体")
    placement = _placement(App, path.parent.parent / "request.json", origin, axis_direction, radial_direction)
    created_doc = doc is None
    if created_doc:
        doc = App.newDocument("RimReconstruction")
    doc.openTransaction("从拟合曲线创建轮辋回转特征")
    try:
        body = doc.addObject("PartDesign::Body", "RimBody")
        body.Placement = placement
        sketch = body.newObject("Sketcher::SketchObject", "RimProfile")
        sketch.Label = "轮毂连续轮盘材料截面"
        for index, (curve, item) in enumerate(zip(curves, data["items"])):
            if item["fit"].get("geometry_kind") == "line":
                sketch.addGeometry(Part.LineSegment(curve.value(curve.FirstParameter),
                    curve.value(curve.LastParameter)), False)
                role = item.get("source", {}).get("line_role")
                if role in ("center_cap_low", "center_cap_high"):
                    sketch.addConstraint(Sketcher.Constraint("Vertical", index))
                elif role == "axis_closure":
                    sketch.addConstraint(Sketcher.Constraint("Horizontal", index))
            else:
                sketch.addGeometry(curve, False)
        if len(curves) == 1:
            # 周期输入转换为等价的夹持表示后，显式保持接缝端点重合。
            sketch.addConstraint(Sketcher.Constraint("Coincident", 0, 2, 0, 1))
        else:
            # 仅约束已在极小连接容差内的端点；大缺口已在建模前拒绝。
            for index in range(len(curves)):
                sketch.addConstraint(Sketcher.Constraint("Coincident", index, 2,
                                     (index + 1) % len(curves), 1))
        doc.recompute()
        if sketch.solve() != 0:
            raise ValueError("草图约束求解失败")
        revolution = body.newObject("PartDesign::Revolution", "RimRevolution")
        revolution.Label = "轮毂连续轮盘回转实体"
        revolution.Profile = sketch
        revolution.ReferenceAxis = (sketch, ["H_Axis"])
        revolution.Angle = 360.0
        revolution.Reversed = False
        revolution.Midplane = False
        revolution.addProperty("App::PropertyString", "SourceFitPath", "Reconstruction")
        revolution.SourceFitPath = str(path)
        revolution.setEditorMode("SourceFitPath", 1)
        doc.recompute()
        shape = revolution.Shape
        if shape.isNull() or not shape.isValid() or len(shape.Solids) != 1 or shape.Volume <= 0:
            raise ValueError("FreeCAD 回转特征未生成有效单实体：" + str(revolution.State))
        body.Tip = revolution
        doc.recompute()
        if ("Touched" in body.State or "Invalid" in body.State
                or body.Shape.isNull() or not body.Shape.isValid()
                or len(body.Shape.Solids) != 1 or body.Shape.Volume <= 0):
            raise ValueError("最终 Body 重算未得到有效单实体：" + str(body.State))
        if App.GuiUp:
            sketch.Visibility = False
            revolution.Visibility = True
            from .view_utils import show_objects
            show_objects(doc, [body])
        doc.commitTransaction()
    except Exception:
        doc.abortTransaction()
        if created_doc:
            App.closeDocument(doc.Name)
        raise
    return {"status": "success", "document_name": doc.Name, "body_name": body.Name,
            "sketch_name": sketch.Name, "revolution_name": revolution.Name,
            "volume_mm3": float(revolution.Shape.Volume), "fit_path": str(path),
            "warnings": ["草图可编辑控制点；尚未生成宽度、壁厚等工程尺寸约束。",
                         "有效实体不代表与 STL 的重建误差已通过。"]}
