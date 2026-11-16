"""选择中心孔至轮辋连续的材料半截面，绕轮毂中心轴生成连续轮盘。
不拼接分离材料，不跨空隙补线；跨角度比较只用于排序。
"""
from __future__ import annotations

import copy
import math
from pathlib import Path
import json

import numpy as np
from scipy.spatial import cKDTree


def _positive(value, name):
    if isinstance(value, (bool, np.bool_)):
        raise ValueError(f"{name} 必须是有限正数")
    try:
        value = float(value)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{name} 必须是有限正数") from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} 必须是有限正数")
    return value


def _vector(value):
    array = np.asarray(value, dtype=float)
    if array.shape != (3,) or not np.isfinite(array).all():
        raise ValueError("坐标系必须包含三个有限数值")
    return array


def _prepare(sections):
    if not isinstance(sections, list) or len(sections) < 1:
        raise ValueError("至少需要一个截面")
    records, angles, reference_origin, reference_axis = [], [], None, None
    for si, section in enumerate(sections):
        if section.get("unit") != "mm" or not isinstance(section.get("contours"), list):
            raise ValueError("输入必须是毫米单位的 process_sections 结果")
        raw_angle = section["angle_degrees"]
        if isinstance(raw_angle, bool) or not isinstance(raw_angle, (int, float)):
            raise ValueError("截面角度必须是数值")
        angle = float(raw_angle)
        if not math.isfinite(angle) or not 0 <= angle < 360 or any(abs(angle-a) < 1e-8 for a in angles):
            raise ValueError("角度必须在 [0,360) 内且不重复，重复角度不能增加支持率")
        angles.append(angle)
        frame = section["frame"]
        origin, axis, radial = (_vector(frame[k]) for k in ("origin", "axis_direction", "radial_direction"))
        if (abs(np.linalg.norm(axis)-1) > 1e-8 or abs(np.linalg.norm(radial)-1) > 1e-8
                or abs(axis @ radial) > 1e-8 or frame.get("half_plane") is not True):
            raise ValueError("需要正交单位轴向/径向和半平面截面")
        if reference_origin is None:
            reference_origin, reference_axis = origin, axis
            reference_radial = radial
            reference_angle = angle
        elif np.linalg.norm(origin-reference_origin) > 1e-7 or np.linalg.norm(axis-reference_axis) > 1e-8:
            raise ValueError("各截面必须使用同一原点和轴向")
        theta = math.radians(angle-reference_angle)
        expected = reference_radial*np.cos(theta) + np.cross(axis, reference_radial)*np.sin(theta)
        if np.linalg.norm(radial-expected) > 1e-7:
            raise ValueError("径向方向与截面角度不一致")
        for ci, contour in enumerate(section["contours"]):
            points = np.asarray(contour["points"], dtype=float)
            if (points.ndim != 2 or points.shape[1] != 3 or len(points) < 2
                    or not np.isfinite(points).all() or type(contour.get("closed")) is not bool):
                raise ValueError("轮廓需要至少两个有限 XYZ 点和布尔 closed")
            local = np.c_[(points-origin) @ axis, (points-origin) @ radial]
            plane_error = np.abs((points-origin) @ np.cross(axis, radial))
            epsilon = max(float(section.get("tolerances", {}).get("plane_mm", 1e-7))*2, 1e-7)
            if np.any(plane_error > epsilon) or np.any(local[:, 1] < -epsilon):
                raise ValueError("轮廓点不在声明的半平面中")
            records.append({"section": si, "section_id": section.get("section_id", f"section_{si}"),
                            "contour_index": contour.get("contour_index", ci),
                            "xyz": points, "local": local, "closed": contour["closed"]})
    return records, angles


def _edges(points, closed):
    first = points if closed else points[:-1]
    second = np.roll(points, -1, axis=0) if closed else points[1:]
    vectors = second-first
    lengths = np.linalg.norm(vectors, axis=1)
    mask = lengths > 1e-12
    return first[mask], vectors[mask], lengths[mask]


def _index(records, si):
    edges = [_edges(r["local"], r["closed"]) for r in records if r["section"] == si]
    if not edges or not any(len(e[0]) for e in edges):
        return None
    a, v, length = [np.concatenate([edge[i] for edge in edges]) for i in range(3)]
    return a, v, length, cKDTree(a+v/2)


def _matches(points, tangents, index, tolerance, cosine):
    """KD 树只做宽阶段搜索，最终使用精确点到线段距离和无向切向夹角。"""
    found = np.zeros(len(points), dtype=bool)
    if index is None:
        return found
    a, v, length, tree = index
    neighborhoods = tree.query_ball_point(points, tolerance+float(length.max())/2)
    for i, ids in enumerate(neighborhoods):
        if not ids:
            continue
        ids = np.asarray(ids)
        t = np.clip(np.sum((points[i]-a[ids])*v[ids], axis=1)/length[ids]**2, 0, 1)
        distances = np.linalg.norm(points[i]-(a[ids]+t[:, None]*v[ids]), axis=1)
        alignment = np.abs((v[ids]/length[ids, None]) @ tangents[i])
        found[i] = np.any((distances <= tolerance) & (alignment >= cosine))
    return found


def _sample(record, spacing):
    a, v, length = _edges(record["local"], record["closed"])
    if not len(a):
        return np.empty((0, 2)), np.empty((0, 2))
    cumulative = np.r_[0, np.cumsum(length)]
    # 按弧长采样而不是原始点密度投票，限制每条轮廓的分析开销。
    count = min(2000, max(4, int(np.ceil(cumulative[-1]/spacing))))
    distance = np.linspace(0, cumulative[-1], count, endpoint=False)
    ids = np.minimum(np.searchsorted(cumulative, distance, side="right")-1, len(a)-1)
    t = (distance-cumulative[ids])/length[ids]
    return a[ids]+t[:, None]*v[ids], v[ids]/length[ids, None]


def _simple_polygon(points):
    """拒绝非相邻边相交/接触和零长边，包括面积非零的自交轮廓。"""
    p = np.asarray(points)
    q = np.roll(p, -1, axis=0)
    v = q-p
    if np.any(np.linalg.norm(v, axis=1) <= 1e-10):
        return False
    def cross(a, b):
        return a[..., 0]*b[..., 1]-a[..., 1]*b[..., 0]
    for i in range(len(p)-2):
        ids = np.arange(i+2, len(p))
        if i == 0:
            ids = ids[ids != len(p)-1]
        # 包围盒宽阶段过滤，使密集 STL 点列的精确相交检查保持局部。
        mask = np.all(np.maximum(np.minimum(p[i], q[i]), np.minimum(p[ids], q[ids])) <=
                      np.minimum(np.maximum(p[i], q[i]), np.maximum(p[ids], q[ids]))+1e-10, axis=1)
        ids = ids[mask]
        a = cross(v[i], p[ids]-p[i]); b = cross(v[i], q[ids]-p[i])
        c = cross(v[ids], p[i]-p[ids]); d = cross(v[ids], q[i]-p[ids])
        if np.any((a*b <= 0) & (c*d <= 0)):
            return False
    return True


def split_material_contour(record, corner_angle_degrees=35., forced_break_indices=()):
    """在尖角处分段；共享端点和原始索引，不创建额外材料边。"""
    p = record["local"]
    incoming = p-np.roll(p, 1, axis=0)
    outgoing = np.roll(p, -1, axis=0)-p
    cosine = np.sum(incoming*outgoing, axis=1)/(np.linalg.norm(incoming, axis=1)*np.linalg.norm(outgoing, axis=1))
    corners = np.flatnonzero(cosine < math.cos(math.radians(corner_angle_degrees))).tolist()
    corners = sorted(set(corners).union(forced_break_indices))
    # 单尖角也用开放曲线保留接缝，但避免重复索引，增加一个源点分割。
    if len(corners) == 1:
        corners = sorted([corners[0], (corners[0]+len(p)//2) % len(p)])
    paths = []
    if not corners:
        paths = [(list(range(len(p))), True)]
    else:
        for start, stop in zip(corners, corners[1:]+corners[:1]):
            count = (stop-start) % len(p)
            paths.append(([(start+j) % len(p) for j in range(count+1)], False))
    return [{"segment_id": f"segment_{i+1}", "closed": closed,
             "points": record["xyz"][ids].tolist(), "points_ar": p[ids].tolist(),
             "source_section_id": record["section_id"],
             "source_contour_index": record["contour_index"], "source_point_indices": ids}
            for i, (ids, closed) in enumerate(paths)]


def analyze_candidates(sections, *, consistency_tolerance_mm=None, min_support_ratio=0.7,
                       tangent_angle_degrees=35.0):
    """选择覆盖全部截面内外径的单一连续闭合材料轮廓。

    匹配参数只影响跨角度验证分数，不决定是否保留候选或删除边界。
    """
    ratio = _positive(min_support_ratio, "min_support_ratio")
    angle_limit = _positive(tangent_angle_degrees, "tangent_angle_degrees")
    if ratio > 1 or angle_limit >= 90:
        raise ValueError("支持率必须 <=1，切向容差必须 <90 度")
    records, angles = _prepare(sections)
    scale = float(np.linalg.norm(np.ptp(np.concatenate([r["local"] for r in records]), axis=0))) if records else 0.
    tolerance = max(scale*.001, 1e-7) if consistency_tolerance_mm is None else _positive(consistency_tolerance_mm, "consistency_tolerance_mm")
    radial_min = min((r["local"][:, 1].min() for r in records), default=0.)
    radial_max = max((r["local"][:, 1].max() for r in records), default=0.)
    # 完整性阈值独立于跨角度匹配容差，不能通过调大匹配容差接纳分离轮辋。
    # 网格和初始轴使各角度内外径略有差异；各端允许全径向跨度的 2%。
    # 这是覆盖启发式，不能作为轴精度或工程语义已验证的证明。
    coverage_tolerance = max((radial_max-radial_min)*.02, 1e-7)
    output = {"schema_version": 1, "unit": "mm", "status": "needs_review",
        "radial_range_mm": None, "candidates": [], "reason": None,
        "warnings": ["目标为连续轮盘；径向覆盖是材料完整性的几何判据，不代表工程语义或实体误差已验证。跨角度支持仅排序，不裁剪或拼接边界。"],
        "diagnostics": {"selection_method": "connected_hub_material_section",
            "target": "continuous_wheel_disc", "required_radial_range_mm": [float(radial_min), float(radial_max)],
            "coverage_tolerance_mm": coverage_tolerance,
            "radius_metric": "arc_length_weighted_mean_radius",
            "angles_degrees": angles, "consistency_tolerance_mm": tolerance,
            "min_support_ratio": ratio, "tangent_angle_degrees": angle_limit,
            "sections": []}}
    selected = []
    for si, section in enumerate(sections):
        valid = []
        for r in records:
            if r["section"] != si or not r["closed"]:
                continue
            p = r["local"]
            a, v, lengths = _edges(p, True)
            area = abs(float(np.sum(p[:, 0]*np.roll(p[:, 1], -1)-p[:, 1]*np.roll(p[:, 0], -1))))/2
            if len(lengths) < 3 or area <= 1e-12 or not _simple_polygon(p):
                continue
            mean = float(np.sum((a[:, 1]+v[:, 1]/2)*lengths)/sum(lengths))
            valid.append((mean, r))
        diagnostic = {"section_id": section.get("section_id", f"section_{si}"),
            "angle_degrees": angles[si], "valid_closed_contour_count": len(valid),
            "open_contour_count": sum(not r["closed"] for r in records if r["section"] == si)}
        output["diagnostics"]["sections"].append(diagnostic)
        complete = [(mean, r) for mean, r in valid
                    if r["local"][:, 1].min() <= radial_min+coverage_tolerance
                    and r["local"][:, 1].max() >= radial_max-coverage_tolerance]
        diagnostic["connected_material_contour_count"] = len(complete)
        if not complete:
            diagnostic["reason"] = "没有从中心孔侧连续覆盖到轮辋的有效闭合材料截面；请选穿过轮辐的角度或修补，不拼接分离轮廓"
            continue
        if len(complete) != 1:
            diagnostic["reason"] = "存在多条覆盖内外径的闭合轮廓，材料区域不唯一"
            continue
        mean, record = complete[0]
        selected.append(record)
        p = record["local"]
        diagnostic["selected_contour_index"] = record["contour_index"]
        candidate = {"candidate_id": f"rim_{len(selected):03d}", "unit": "mm",
            "source_section_id": record["section_id"], "source_section_index": si,
            "source_contour_index": record["contour_index"], "angle_degrees": angles[si],
            "closed": True, "complete": True, "semantic_complete": None,
            "completeness_definition": "single_closed_material_contour_spans_hub_to_rim",
            "target": "continuous_wheel_disc",
            "axis": {"origin": list(section["frame"]["origin"]), "direction": list(section["frame"]["axis_direction"])},
            "fit_ready": True, "source_length_coverage": 1.0,
            "mean_radius_mm": mean, "minimum_radius_mm": float(p[:, 1].min()),
            "maximum_radius_mm": float(p[:, 1].max()), "radial_span_mm": float(np.ptp(p[:, 1])),
            "support_ratio": None, "supporting_angles": [],
            "issues": [], "segments": split_material_contour(record)}
        output["candidates"].append(candidate)
    # Compare selected whole contours bidirectionally; never turn matches into an edge mask.
    indices = [_index(selected, r["section"]) for r in selected]
    samples = [_sample(r, max(tolerance, scale/1000, 1e-7)) for r in selected]
    scores = [[] for _ in selected]
    cosine = math.cos(math.radians(angle_limit))
    for i in range(len(selected)):
        for j in range(i+1, len(selected)):
            score = min(float(np.mean(_matches(*samples[i], indices[j], tolerance, cosine))),
                        float(np.mean(_matches(*samples[j], indices[i], tolerance, cosine))))
            scores[i].append(score); scores[j].append(score)
            if score >= ratio:
                output["candidates"][i]["supporting_angles"].append(angles[selected[j]["section"]])
                output["candidates"][j]["supporting_angles"].append(angles[selected[i]["section"]])
    for candidate, values in zip(output["candidates"], scores):
        candidate["support_ratio"] = float(np.mean(values)) if values else None
        if not values:
            candidate["issues"].append("仅一个角度可提取，尚无跨角度验证")
    if selected:
        output["status"] = "candidates_found"
        output["radial_range_mm"] = [min(c["minimum_radius_mm"] for c in output["candidates"]),
                                      max(c["maximum_radius_mm"] for c in output["candidates"])]
        output["candidates"].sort(key=lambda c: (c["fit_ready"], c["support_ratio"] if c["support_ratio"] is not None else -1), reverse=True)
    else:
        output["reason"] = "没有唯一、连续覆盖中心孔至轮辋的闭合材料截面；请增加穿过轮辐的截面角度或修补"
    return output


def select_profile(analysis, candidate_id=None):
    """返回可交给 fit_profile 的数据；未指定编号时选排序第一的候选，不补全缺口。"""
    if analysis.get("status") != "candidates_found" or not analysis.get("candidates"):
        raise ValueError(analysis.get("reason") or "没有可选轮辋候选")
    candidate = next((c for c in analysis["candidates"]
                      if candidate_id is None or c["candidate_id"] == candidate_id), None)
    if candidate is None:
        raise ValueError("候选编号不存在")
    profile = copy.deepcopy(candidate)
    profile["profile_id"] = profile["candidate_id"]
    profile["radial_range_mm"] = list(analysis["radial_range_mm"])
    profile["warnings"] = list(analysis["warnings"])
    return profile


def save_selection_result(result, path):
    """保存可审阅的半径估计、候选来源和点列，不输出 NaN/Infinity。"""
    encoded = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)+"\n"
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(encoded, encoding="utf-8")
    return path
