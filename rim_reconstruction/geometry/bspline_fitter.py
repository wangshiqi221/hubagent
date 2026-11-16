"""有序截面点列的参数 B 样条逼近（NumPy + SciPy）。

本模块只处理已筛选的点列，不读取 STL、不识别轮辋、不调用 C++。
开放轮廓使用非周期三次样条，闭合光滑轮廓使用周期三次样条。
尖角/直线特征需要调用方预先分段，通过 fit_profile 分别处理。

验收指标为 max(||C(u_i)-P_i||)，u_i 是弦长参数。它是输入点到
曲线最短距离的上界，但不是整条原始折线的连续误差上界。
沿折线弧长积分位置残差，并加入二阶导数平滑和邻域方向软约束；
逐步减弱软约束、细化节点，并独立检查最大残差。原始输入不原地修改，所有结果均可 JSON 序列化。
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np
from scipy.interpolate import BSpline
from scipy.linalg import lstsq


def _positive(value, name):
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, float, np.number)):
        raise ValueError(f"{name} 必须是有限正数")
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} 必须是有限正数")
    return value


def _integer(value, name, minimum):
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, np.integer)) or value < minimum:
        raise ValueError(f"{name} 必须是 >= {minimum} 的整数")
    return int(value)


def _prepare_points(points, closed, duplicate_tolerance_mm):
    """仅去除相邻重复点；闭合轮廓的重复首点在参数化前统一处理。"""
    try:
        data = np.array(points, dtype=float, copy=True)
    except (TypeError, ValueError) as exc:
        raise ValueError("points 必须为 N×2 或 N×3 数值数组") from exc
    if data.ndim != 2 or data.shape[1] not in (2, 3) or len(data) < 4:
        raise ValueError("三次拟合需要至少四个二维或三维点")
    if not np.isfinite(data).all():
        raise ValueError("点坐标不能包含 NaN 或无穷值")
    count = len(data)
    kept = [0]
    for index in range(1, count):
        if np.linalg.norm(data[index] - data[kept[-1]]) > duplicate_tolerance_mm:
            kept.append(index)
    data = data[kept]
    # C++ 闭合点列通常不重复首点，也兼容显式重复首点的外部输入。
    if closed:
        while len(data) > 1 and np.linalg.norm(data[-1] - data[0]) <= duplicate_tolerance_mm:
            data = data[:-1]
            kept.pop()
    if len(data) < 4:
        raise ValueError("去重后三次拟合需要至少四个有效点")
    work = np.vstack((data, data[0])) if closed else data
    with np.errstate(over="ignore", invalid="ignore"):
        lengths = np.linalg.norm(np.diff(work, axis=0), axis=1)
        cumulative = np.r_[0.0, np.cumsum(lengths)]
    if not np.isfinite(cumulative).all() or cumulative[-1] <= 0 or np.any(lengths <= 0):
        raise ValueError("轮廓弧长无效或坐标尺度过大")
    u = cumulative / cumulative[-1]
    if np.any(np.diff(u) <= 0):
        raise ValueError("点距尺度差异过大，无法构造严格递增参数")
    return data, work, u, kept, count


def _quadrature(breaks, order=4):
    nodes, weights = np.polynomial.legendre.leggauss(order)
    half = np.diff(breaks) / 2
    middle = (breaks[1:] + breaks[:-1]) / 2
    return (middle[:, None] + half[:, None] * nodes).ravel(), (half[:, None] * weights).ravel()


def _reference_values(work, u, parameters, closed):
    parameters = np.mod(parameters, 1.0) if closed else parameters
    return np.column_stack([np.interp(parameters, u, work[:, j]) for j in range(work.shape[1])])


def _reference_directions(work, u, parameters, window, closed):
    """连续弧长邻域回归；闭合曲线的窗口跨越接缝，避免端点方向偏置。"""
    directions = np.zeros((len(parameters), work.shape[1]))
    boundaries = np.r_[u[:-1] - 1, u, u[1:] + 1] if closed else u
    for i, parameter in enumerate(parameters):
        a, b = parameter - window, parameter + window
        if not closed:
            a, b = max(0., a), min(1., b)
        center = (a + b) / 2
        partition = np.r_[a, boundaries[(boundaries > a) & (boundaries < b)], b]
        q, w = _quadrature(partition, 2)
        origin = _reference_values(work, u, np.array([center]), closed)[0]
        covariance = ((q - center) * w) @ (_reference_values(work, u, q, closed) - origin)
        magnitude = np.linalg.norm(covariance)
        if magnitude > 512 * np.finfo(float).eps * (b - a)**2:
            directions[i] = covariance / magnitude
    return directions


def _constrained_spline(work, u, breaks, closed, tolerance, window, weight, strength):
    """弧长位置积分 + 二阶导数积分 + 邻域方向软约束。"""
    length = np.linalg.norm(np.diff(work, axis=0), axis=1).sum()
    reference = (work - work[0]) / length
    spans = len(breaks) - 1
    if closed:
        # 将非均匀节点向两侧周期延伸，重复前三个系数，精确满足 C0/C1/C2 接缝。
        knots = np.r_[breaks[-4:-1] - 1, breaks, breaks[1:4] + 1]
        transform = np.eye(spans)[np.arange(spans + 3) % spans]
        fixed = np.zeros((spans + 3, work.shape[1]))
    else:
        knots = np.r_[np.zeros(3), breaks, np.ones(3)]
        count = len(knots) - 4
        transform = np.eye(count)[:, 1:-1]
        fixed = np.zeros((count, work.shape[1]))
        fixed[-1] = reference[-1]
    basis = BSpline(knots, np.eye(len(knots) - 4), 3)
    q, w = _quadrature(np.union1d(u, breaks))
    matrix = [basis(q) * np.sqrt(w[:, None])]
    targets = [_reference_values(reference, u, q, closed) * np.sqrt(w[:, None])]
    lam = (tolerance / length)**2 * 1.e-3 * strength
    smooth_q, smooth_w = _quadrature(breaks, 2)
    matrix.append(basis(smooth_q, nu=2) * np.sqrt(lam * smooth_w[:, None]))
    targets.append(np.zeros((len(smooth_q), work.shape[1])))
    if window > 0 and weight > 0:
        directions = _reference_directions(reference, u, q, min(window / length, .5 if closed else 1.), closed)
        valid = np.linalg.norm(directions, axis=1) > 0
        scale = min(window, 10 * tolerance, length) / length
        factors = np.sqrt(w[valid] * weight * strength)[:, None] * scale
        matrix.append(basis(q[valid], nu=1) * factors)
        targets.append(directions[valid] * factors)
    matrix, target = np.vstack(matrix), np.vstack(targets)
    coefficients, _, rank, _ = lstsq(matrix @ transform, target - matrix @ fixed, lapack_driver="gelsy")
    if rank < transform.shape[1]:
        raise ValueError("拟合系统秩不足")
    controls = (transform @ coefficients + fixed) * length + work[0]
    return BSpline(knots, controls, 3), lam


def fit_contour(points, *, closed: bool, tolerance_mm=0.08, degree=3,
                max_control_points=256, max_iterations=12, sample_count=500,
                duplicate_tolerance_mm=1e-9, direction_window_mm=1.0,
                direction_weight=1.0) -> dict[str, Any]:
    """拟合一条有序轮廓，返回曲线参数、误差、采样点和合格状态。

    参数错误抛 ValueError；数值求解失败、控制点超限或未达容差返回
    success=False/compliant=False，并在 error 中解释。调用方须检查状态。
    closed 必须显式指定；True 表示整个轮廓接缝应光滑，采用周期边界。
    开放曲线精确通过端点；闭合曲线保持周期 C2 连续。
    位置项、二阶导数平滑项和方向项联合求解，独立按最大对应距离验收。
    direction_window_mm 是方向回归的弧长半窗口，闭合时最多取半圈；
    direction_weight 是方向软约束权重，任一参数为零时关闭方向项。
    smoothing_factor 保留为 None；regularization_lambda 记录实际二阶惩罚系数，
    direction_weight 为请求权重，effective_direction_weight 为实际采用权重。
    """
    if type(closed) is not bool:
        raise ValueError("closed 必须为 bool")
    if _integer(degree, "degree", 1) != 3:
        raise ValueError("当前仅支持三次 B 样条 degree=3")
    tolerance = _positive(tolerance_mm, "tolerance_mm")
    max_controls = _integer(max_control_points, "max_control_points", 4)
    iterations = _integer(max_iterations, "max_iterations", 2)
    samples = _integer(sample_count, "sample_count", 2)
    if isinstance(duplicate_tolerance_mm, (bool, np.bool_)):
        raise ValueError("duplicate_tolerance_mm 必须为有限非负数")
    try:
        duplicate = float(duplicate_tolerance_mm)
    except (TypeError, ValueError) as exc:
        raise ValueError("duplicate_tolerance_mm 必须为有限非负数") from exc
    if not math.isfinite(duplicate) or duplicate < 0:
        raise ValueError("duplicate_tolerance_mm 必须为有限非负数")

    for name, value in (("direction_window_mm", direction_window_mm), ("direction_weight", direction_weight)):
        if isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, float, np.number)) or not math.isfinite(value) or value < 0:
            raise ValueError(f"{name} 必须为有限非负数")
    data, work, u, indices, original_count = _prepare_points(points, closed, duplicate)
    # 闭合参数化增加的末点仅用于周期求解。误差仍检查全部原始输入，
    # 避免较大的去重阈值将难拟合的点删掉后产生虚假的合格结果。
    evaluation_u = u[:-1] if closed else u
    original = np.asarray(points, dtype=float)
    assignments = np.searchsorted(indices, np.arange(original_count), side="right") - 1
    if closed:
        for index in range(indices[-1] + 1, original_count):
            if np.linalg.norm(original[index] - data[0]) <= duplicate:
                assignments[index] = 0
    validation_u = evaluation_u[assignments]
    result = {
        "schema_version": 1, "unit": "mm", "success": False, "compliant": False,
        "closed": closed, "degree": 3, "dimension": data.shape[1],
        "tolerance_mm": tolerance, "input_point_count": original_count,
        "retained_point_count": len(data), "retained_point_indices": indices,
        "error_metric": "chord_parameter_correspondence_distance",
        "continuous_error_bound": False, "iterations": 0,
        "maximum_deviation_mm": None, "rms_deviation_mm": None,
        "endpoint_deviations_mm": None, "bspline": None, "sample_points": [],
        "warnings": [], "error": None,
    }
    if closed:
        result["warnings"].append("周期拟合只适用于光滑接缝；含尖角的轮廓请先分段")
    initial_spans = min(8, max_controls - 3) if closed else 1
    if closed and initial_spans < 4:
        result["error"] = {"code": "CONTROL_POINT_LIMIT", "message": "周期三次拟合至少需要 7 个展开系数"}
        return result
    breaks = np.linspace(0, 1, initial_spans + 1)
    last_error = {"code": "TOLERANCE_NOT_MET", "message": "在迭代预算内未达到容差"}
    for attempt in range(iterations):
        result["iterations"] = attempt + 1
        # 先保持较强平滑，仅在容差不满足时减弱两个软约束。
        best = None
        for smoothing_attempt in range(5):
            try:
                candidate, lam = _constrained_spline(
                    work, u, breaks, closed, tolerance, direction_window_mm,
                    direction_weight, .1 ** smoothing_attempt)
                residuals = np.linalg.norm(candidate(validation_u) - original, axis=1)
                if not np.isfinite(residuals).all() or not np.isfinite(candidate.c).all():
                    last_error = {"code": "NONFINITE_RESULT", "message": "求解产生非有限数值"}
                    continue
                if best is None or residuals.max() < best[0]:
                    best = (float(residuals.max()), candidate, lam, direction_weight * .1 ** smoothing_attempt)
                if residuals.max() <= tolerance:
                    break
            except (ValueError, RuntimeError, np.linalg.LinAlgError) as exc:
                last_error = {"code": "SOLVER_FAILED", "message": str(exc)}
        if best is None:
            break
        _, spline, smoothing, effective_weight = best
        control_count = len(spline.t) - spline.k - 1
        controls = spline.c[:control_count]
        if control_count > max_controls:
            last_error = {"code": "CONTROL_POINT_LIMIT",
                          "message": f"需要 {control_count} 个系数，超过上限 {max_controls}"}
            continue
        fitted = np.asarray(spline(validation_u))
        if not np.isfinite(fitted).all() or not np.isfinite(controls).all():
            last_error = {"code": "NONFINITE_RESULT", "message": "求解产生非有限数值"}
            continue
        distances = np.linalg.norm(fitted - original, axis=1)
        if not np.isfinite(distances).all():
            last_error = {"code": "NONFINITE_RESULT", "message": "距离计算超出浮点范围"}
            continue
        maximum = float(np.max(distances))
        sample_u = np.linspace(0, 1, samples, endpoint=not closed)
        result.update({
            "maximum_deviation_mm": maximum,
            "rms_deviation_mm": float(np.linalg.norm(distances) / math.sqrt(len(distances))),
            "endpoint_deviations_mm": None if closed else [float(distances[0]), float(distances[-1])],
            "sample_points": np.asarray(spline(sample_u)).tolist(),
            "bspline": {
                "degree": 3, "periodic": closed, "control_point_count": control_count,
                "knots": spline.t.tolist(), "control_points": controls.tolist(),
                "weights": [1.0] * control_count, "parameter_range": [0.0, 1.0],
                "knot_format": "full_repeated", "smoothing_factor": None,
                "fitting_method": "arc_length_regularized_least_squares",
                "regularization_lambda": smoothing,
                "direction_window_mm": float(direction_window_mm),
                "direction_weight": float(direction_weight),
                "effective_direction_weight": float(effective_weight),
            },
        })
        # 严格按用户容差验收，不额外放宽阈值。
        if maximum <= tolerance:
            result.update(success=True, compliant=True, error=None)
            return result
        # 误差超限的节点区间优先二分；不改动周期接缝条件。
        available = max_controls - control_count
        if available <= 0:
            last_error = {"code": "CONTROL_POINT_LIMIT", "message": "达到控制点预算，仍未满足容差"}
            break
        failed = np.argsort(-distances)
        additions = []
        for index in failed:
            if distances[index] <= tolerance:
                break
            span = min(len(breaks) - 2, np.searchsorted(breaks, validation_u[index], side="right") - 1)
            middle = (breaks[span] + breaks[span + 1]) / 2
            if breaks[span] + 1e-12 < middle < breaks[span + 1] - 1e-12 and middle not in additions:
                additions.append(middle)
            if len(additions) >= min(32, available):
                break
        if not additions:
            break
        breaks = np.sort(np.r_[breaks, additions])
    result["error"] = last_error
    return result


def _exact_line(points, tolerance_mm, sample_count):
    """Represent explicitly declared lines exactly; no smoothing solver."""
    tolerance = _positive(tolerance_mm, "tolerance_mm")
    count = _integer(sample_count, "sample_count", 2)
    if points.ndim != 2 or points.shape[1] not in (2,3) or len(points) != 2 or not np.isfinite(points).all():
        raise ValueError("精确直线必须提供两个有限端点")
    if np.linalg.norm(points[1]-points[0]) <= 1e-12:
        raise ValueError("精确直线端点不能重合")
    return {"success": True, "compliant": True, "geometry_kind": "line", "dimension": points.shape[1],
        "closed": False, "degree": 1, "tolerance_mm": tolerance, "input_point_count": 2,
        "maximum_deviation_mm": 0., "rms_deviation_mm": 0., "endpoint_deviations_mm": [0.,0.],
        "continuous_error_bound": True, "error_metric": "exact_segment", "error": None,
        "sample_points": np.linspace(points[0],points[1],count).tolist(),
        "bspline": {"degree": 1, "periodic": False, "control_point_count": 2,
            "control_points": points.tolist(), "knots": [0.,0.,1.,1.], "weights": [1.,1.],
            "parameter_range": [0.,1.], "knot_format": "full_repeated", "fitting_method": "exact_line"}}


def _fit_items(items, *, tolerance_mm, options):
    """批量拟合时逐项记录错误，不丢弃其他已成功的曲线。"""
    results = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError("每个轮廓/片段必须是字典")
        try:
            points = np.asarray(item["points"], dtype=float)
            # 尖角分段可能仅有两个源点。只在线段内部加采样，不跨源边补线；
            # 来源索引仍引用原点列。开放三次曲线严格通过两端。
            if (item.get("geometry_kind") != "line" and not item["closed"] and points.ndim == 2 and len(points) in (2, 3)
                    and points.shape[1] in (2, 3) and np.isfinite(points).all()):
                points = np.vstack([a+(b-a)*np.linspace(0, 1, 4, endpoint=False)[:, None]
                                    for a, b in zip(points[:-1], points[1:])] + [points[-1:]])
            if item.get("geometry_kind") == "line":
                if item["closed"]:
                    raise ValueError("单条直线不能作为闭合轮廓")
                fitted = _exact_line(points, tolerance_mm, options.get("sample_count",500))
            else:
                fitted = fit_contour(points, closed=item["closed"],
                                     tolerance_mm=tolerance_mm, **options)
        except (ValueError, KeyError) as exc:
            fitted = {"success": False, "compliant": False,
                      "error": {"code": "INVALID_CONTOUR", "message": str(exc)}}
        # 只复制明确的来源标识，不将未知输入字段混进曲线结果。
        source = {key: item[key] for key in (
            "contour_index", "segment_id", "source_section_id", "source_contour_index",
            "source_point_indices", "source_kind", "reconstructed_point_indices", "geometry_kind", "line_role") if key in item}
        results.append({"item_index": index, "source": source, "fit": fitted})
    success = bool(results) and all(item["fit"]["success"] for item in results)
    error = None
    if not results:
        error = {"code": "EMPTY_INPUT", "message": "没有可拟合轮廓"}
    elif not success:
        error = {"code": "PARTIAL_FIT_FAILED", "message": "部分轮廓拟合失败，请检查各项 error"}
    return {"schema_version": 1, "unit": "mm", "success": success, "compliant": success,
            "items": results, "error": error}


def fit_section(section: dict, *, tolerance_mm=0.08, **options) -> dict:
    """拟合 C++ process_sections 的各条轮廓；本函数不会筛选轮辋。"""
    if not isinstance(section, dict) or section.get("unit") != "mm":
        raise ValueError("section 必须为毫米单位的截面字典")
    if not isinstance(section.get("contours"), list):
        raise ValueError("section 必须包含 contours 列表")
    result = _fit_items(section["contours"], tolerance_mm=tolerance_mm, options=options)
    result["source_section_id"] = section.get("section_id")
    return result


def fit_profile(profile: dict, *, tolerance_mm=0.08, **options) -> dict:
    """拟合筛选器返回的 segments，不自动跨缺口连接或宣称完整闭合。

    每段需含 points；段级 closed 缺省为 False。profile.closed 表示整个
    截面边界状态，不能用于给每个片段都施加周期边界。
    """
    if not isinstance(profile, dict) or not isinstance(profile.get("segments"), list):
        raise ValueError("profile 必须包含 segments 列表")
    if profile.get("unit", "mm") != "mm":
        raise ValueError("profile 必须使用毫米单位")
    items = []
    for segment in profile["segments"]:
        if not isinstance(segment, dict):
            raise ValueError("segment 必须是字典")
        items.append({"closed": False, **segment})
    result = _fit_items(items, tolerance_mm=tolerance_mm, options=options)
    result["source_profile_id"] = profile.get("profile_id", profile.get("candidate_id"))
    result["profile_complete"] = profile.get("complete")
    result["warnings"] = ["各段独立拟合，未验证段间连续性、自交或回转实体有效性"]
    return result


def evaluate_bspline(curve: dict, parameters) -> np.ndarray:
    """从 bspline 字典重建并求值；返回 N×D 点数组，参数限于有效区间。"""
    if not isinstance(curve, dict) or curve.get("knot_format") != "full_repeated":
        raise ValueError("需要完整重复节点格式的 bspline 字典")
    degree = _integer(curve["degree"], "degree", 1)
    knots = np.asarray(curve["knots"], dtype=float)
    controls = np.asarray(curve["control_points"], dtype=float)
    weights = np.asarray(curve["weights"], dtype=float)
    if (knots.ndim != 1 or controls.ndim != 2 or controls.shape[1] not in (2, 3)
            or len(knots) != len(controls) + degree + 1 or len(controls) <= degree
            or not np.isfinite(knots).all() or not np.isfinite(controls).all()
            or np.any(np.diff(knots) < 0) or knots[degree] >= knots[-degree-1]
            or weights.shape != (len(controls),) or not np.all(weights == 1)):
        raise ValueError("无效的非有理 B 样条参数")
    parameters = np.atleast_1d(np.asarray(parameters, dtype=float))
    if (parameters.ndim != 1 or not np.isfinite(parameters).all()
            or np.any(parameters < knots[degree]) or np.any(parameters > knots[-degree-1])):
        raise ValueError("参数必须位于曲线有效区间")
    return BSpline(knots, controls, degree, extrapolate=False)(parameters)


def save_fit_result(result: dict, path: str | Path) -> Path:
    """保存 JSON 曲线参数与诊断；allow_nan=False 防止输出无效数值。"""
    encoded = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(encoded, encoding="utf-8")
    return path
