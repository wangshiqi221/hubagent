"""Deterministic base-disc reconstruction from angular section evidence.

Central features are recovered; rims are copied only after angular checks.
Local branch events retain valid neighboring observations. Unobserved shoulders
and extended ambiguous layers require more evidence, never a material union.
"""
from __future__ import annotations

import copy
import math
import numpy as np

from .rim_profile_selector import _prepare, _simple_polygon, split_material_contour

ALGORITHM = "base_disc_v4"
TARGET = "closed_center_base_disc"


def _intersections(p, radii):
    """Return every axial material interval; no branch is discarded here."""
    q = np.roll(p, -1, axis=0)
    intersections = []
    for radius in radii:
        mask = ((p[:, 1] <= radius) & (radius < q[:, 1])) | ((q[:, 1] <= radius) & (radius < p[:, 1]))
        a, b = p[mask], q[mask]
        hits = np.sort(a[:, 0]+(radius-a[:, 1])*(b[:, 0]-a[:, 0])/(b[:, 1]-a[:, 1]))
        hits = hits[np.r_[True, np.diff(hits) > 1e-8]] if len(hits) else hits
        intersections.append(hits)
    return intersections


def _trace(p, radii):
    """Strict utility retained for checking completed material boundaries."""
    hits = _intersections(p, radii)
    if any(len(h) != 2 for h in hits):
        raise ValueError("截面含非单一材料区间")
    return np.asarray(hits).T


def _track_branches(intersections, radii, tolerance):
    """Keep unambiguous observations and mark bounded branch events locally.

    Multiple intervals are not unioned. Missing spans are reconstructed later
    only if other angular observations support the shoulder interpolation.
    """
    values = np.full((2, len(radii)), np.nan)
    events = []
    for i, hits in enumerate(intersections):
        if len(hits) == 2:
            values[:, i] = hits
    invalid = ~np.isfinite(values[0])
    for start, end in np.flatnonzero(np.diff(np.r_[False, invalid, False])).reshape(-1, 2):
        left, right = int(start)-1, int(end)
        if left < 2 or right > len(radii)-3:
            raise ValueError("轮盘跟踪区边缘缺少单一材料分支")
        if radii[right]-radii[left] > .12*(radii[-1]-radii[0]):
            raise ValueError("多层材料范围过长，不能作为局部特征处理")
        events.append({"start": int(start), "end": int(end),
            "radial_interval_mm": [float(radii[left]), float(radii[right])],
            "intersection_counts": [len(intersections[i]) for i in range(start,end)]})
    return values, events


def _tracking_start(intersections, radii):
    """Separate the small bore-transition band from the observed base disc.

    Require five consecutive single-interval observations in every selected
    angle. Never skip a large inner cavity to manufacture a solid envelope.
    """
    limit = max(radii[0]*.25, .05*(radii[-1]-radii[0]))
    counts = np.asarray([[len(h) for h in row] for row in intersections])
    for i in range(len(radii)-4):
        if radii[i]-radii[0] > limit:
            break
        if np.all(counts[:, i:i+5] == 2):
            return i
    raise ValueError("近孔过渡区之后没有共同的稳定材料分支，不能安全确定封孔起点")


def _angular_support(indices, angles):
    return any(min(abs(angles[i]-angles[j]), 360-abs(angles[i]-angles[j])) >= 15
               for i in indices for j in indices if i != j)


def _hermite(x, x0, x1, y0, y1, d0, d1):
    t = (x-x0)/(x1-x0)
    return ((2*t**3-3*t**2+1)*y0+(t**3-2*t**2+t)*(x1-x0)*d0
            +(-2*t**3+3*t**2)*y1+(t**3-t**2)*(x1-x0)*d1)


def _recover_surfaces(traces, radii, angles, tolerance):
    """Recover local branch/feature events without requiring all angles to
    coincide axially. Smooth angular shape differences remain separate candidates.

    Each repair uses an observed smooth reference, adjusted by a C1 Hermite
    shoulder correction. It is never the union/envelope of material intervals.
    """
    original = np.asarray(traces)
    work = original.copy()
    n = len(radii)
    regions = [[None, None] for _ in angles]
    for i in range(len(angles)):
        for side in range(2):
            good = np.isfinite(work[i, side])
            if good.sum() < 5 or not good[0] or not good[-1]:
                raise ValueError("跟踪区端点缺少观测，不能建立局部修复肩部")
            # This interpolation is only for event detection, never accepted as
            # generated geometry without independent angular references below.
            y = np.interp(radii, radii[good], work[i, side, good])
            slope = np.diff(y)/np.diff(radii)
            jumps = np.abs(np.diff(np.arctan(slope)))
            sharp = np.flatnonzero(jumps > math.radians(35))+1
            missing = np.flatnonzero(~good)
            bad = np.union1d(sharp, missing).astype(int)
            work[i, side] = y
            if len(bad):
                left, right = int(bad[0])-3, int(bad[-1])+3
                if left < 2 or right > n-3 or radii[right]-radii[left] > .45*(radii[-1]-radii[0]):
                    raise ValueError("局部异常缺少两侧稳定肩部或跨度过大，不能恢复基础表面")
                baseline = np.interp(radii[left:right+1], radii[[left,right]], y[[left,right]])
                if not len(missing) and np.max(np.abs(y[left:right+1]-baseline)) <= tolerance:
                    continue
                regions[i][side] = (left, right)
    edits = [[] for _ in angles]
    recovered = work.copy()
    for i in range(len(angles)):
        for side in range(2):
            region = regions[i][side]
            if region is None:
                continue
            left, right = region
            # Sources must be observed, not themselves interpolated or repaired
            # over this shoulder window. Two neighboring samples are included
            # for tangent estimation.
            supported = [j for j in range(len(angles))
                if np.isfinite(original[j, side, left-2:right+3]).all()
                and (regions[j][side] is None or
                     regions[j][side][1] < left-2 or regions[j][side][0] > right+2)]
            if not _angular_support(supported, angles):
                raise ValueError("局部异常没有两个分散角度的平顺观测表面支持，需补充错相截面")
            def shoulder_curve(y):
                d0 = (y[left]-y[left-2])/(radii[left]-radii[left-2])
                d1 = (y[right+2]-y[right])/(radii[right+2]-radii[right])
                return _hermite(radii[left:right+1], radii[left], radii[right], y[left], y[right], d0, d1)
            def bending(j):
                return float(np.sum(np.abs(np.diff(np.diff(work[j,side,left-2:right+3]) /
                    np.diff(radii[left-2:right+3])))))
            reference = min(supported, key=lambda j: (bending(j), angles[j]))
            source = work[reference, side]
            corrected = source[left:right+1]-shoulder_curve(source)+shoulder_curve(work[i,side])
            recovered[i,side,left:right+1] = corrected
            edits[i].append({"side": side, "radial_interval_mm": [float(radii[left]),float(radii[right])],
                "reference_angle_degrees": angles[reference], "supporting_angles": [angles[j] for j in supported],
                "method": "observed_reference_with_c1_shoulder_correction",
                "maximum_shoulder_correction_mm": float(np.max(np.abs(corrected-source[left:right+1])))})
    # A lone clean angle cannot certify that its surface is the base rather
    # than another local feature: require the same evidence for clean candidates.
    for side in range(2):
        for region in [r[side] for r in regions if r[side] is not None]:
            left,right=region
            clean=[j for j in range(len(angles)) if regions[j][side] is None or
                regions[j][side][1] < left-2 or regions[j][side][0] > right+2]
            if not _angular_support(clean, angles):
                raise ValueError("基础表面缺少独立角度证据")
    if np.any(recovered[:,1]-recovered[:,0] <= 2*tolerance):
        raise ValueError("恢复后的基础表面相交或厚度不足")
    return recovered, edits


def _rim_path(p, cut):
    """Clip at r=cut; remove the artificial clipping edge, retain ordered rim."""
    clipped = []
    for a, b in zip(p, np.roll(p, -1, axis=0)):
        ia, ib = a[1] >= cut, b[1] >= cut
        if ia:
            clipped.append(a.copy())
        if ia != ib:
            hit = a+(b-a)*(cut-a[1])/(b[1]-a[1])
            hit[1] = cut
            clipped.append(hit)
    p = np.asarray(clipped)
    ids = np.flatnonzero(np.abs(p[:, 1]-cut) < 1e-8)
    if len(ids) != 2:
        raise ValueError("轮盘与轮辋连接处不是单一材料通道，需调整轮盘范围")
    low, high = sorted(ids, key=lambda i: p[i, 0])
    for step in (1, -1):
        path = [p[low]]
        i = (low+step) % len(p)
        while i != high:
            path.append(p[i]); i = (i+step) % len(p)
        path.append(p[high])
        if len(path) > 2:
            return np.asarray(path)
    raise ValueError("外围轮辋缺少可保留的路径")


def reconstruct_base_profiles(analysis, sections, *, disc_radius_mm=None, feature_tolerance_mm=.08):
    """Return independent versioned candidates; never alter source artifacts."""
    if (type(feature_tolerance_mm) not in (int, float) or not math.isfinite(feature_tolerance_mm)
            or feature_tolerance_mm <= 0):
        raise ValueError("feature_tolerance_mm 必须为有限正数")
    if disc_radius_mm is not None and (type(disc_radius_mm) not in (int, float)
            or not math.isfinite(disc_radius_mm) or disc_radius_mm <= 0):
        raise ValueError("disc_radius_mm 必须为有限正数")
    result = copy.deepcopy(analysis)
    result["candidates"] = []
    result["reconstruction_version"] = ALGORITHM
    result["diagnostics"]["base_reconstruction"] = {"target": TARGET, "rejected": []}
    candidates = analysis.get("candidates", [])
    if not candidates:
        return result
    records, _ = _prepare(sections)
    source = {(r["section_id"], r["contour_index"]): r for r in records}
    selected = [source[(c["source_section_id"], c["source_contour_index"])] for c in candidates]
    outer = max(float(r["local"][:, 1].max()) for r in selected)
    cut = float(disc_radius_mm) if disc_radius_mm is not None else .65*outer
    params = {"disc_radius_mm": disc_radius_mm, "feature_tolerance_mm": float(feature_tolerance_mm)}
    result["diagnostics"]["base_reconstruction"].update(parameters=params, effective_disc_radius_mm=cut,
        region_policy="explicit_radius" if disc_radius_mm is not None else "conservative_inner_65_percent")
    from .rim_consistency import assess_rims
    rim_checks=assess_rims(selected,[float(c["angle_degrees"]) for c in candidates],cut,feature_tolerance_mm,_intersections)
    result["diagnostics"]["base_reconstruction"]["rim_consistency"]=rim_checks
    accepted=[(c,r,check) for c,r,check in zip(candidates,selected,rim_checks) if check["status"]=="consistent"]
    for c,r,check in zip(candidates,selected,rim_checks):
        if check["status"]!="consistent":
            result["diagnostics"]["base_reconstruction"]["rejected"].append({"angle_degrees":c["angle_degrees"],
                "stage":"rim_consistency","reason":"外围轮廓含其他角度不支持的局部形状，不能旋转为环槽","details":check})
    if not accepted:
        result.update(status="needs_review",radial_range_mm=None,reason="没有通过跨角度检查的轮辋路径")
        return result
    # A bad rim does not invalidate the same angle's independent central-disc
    # observations. Keep these as evidence; exclude only its final rim candidate.
    rim_evidence={c["candidate_id"]:check for c,r,check in accepted}
    eligible, traces, angles = [], [], []
    inner = max(float(r["local"][:, 1].min()) for r in selected)
    eps = max(outer*1e-8, 1e-8)
    if not inner+10*eps < cut < outer-eps:
        result.update(status="needs_review", radial_range_mm=None, reason="轮盘处理半径没有位于中心孔与轮辋之间")
        return result
    radii = np.linspace(inner+eps, cut, 129)
    intersections = [_intersections(record["local"], radii) for record in selected]
    try:
        start = _tracking_start(intersections, radii)
    except ValueError as exc:
        result.update(status="needs_review", radial_range_mm=None, reason=str(exc))
        return result
    result["diagnostics"]["base_reconstruction"]["tracking_start_radius_mm"] = float(radii[start])
    result["diagnostics"]["base_reconstruction"]["bore_transition_interval_mm"] = [float(inner),float(radii[start])]
    radii = radii[start:]
    branch_events = []
    for candidate, record, hits in zip(candidates, selected, intersections):
        try:
            trace, events = _track_branches(hits[start:], radii, feature_tolerance_mm)
            traces.append(trace)
            branch_events.append(events)
            eligible.append((candidate, record)); angles.append(float(candidate["angle_degrees"]))
        except ValueError as exc:
            result["diagnostics"]["base_reconstruction"]["rejected"].append({"angle_degrees": candidate["angle_degrees"], "reason": str(exc)})
    if not traces:
        result.update(status="needs_review", radial_range_mm=None, reason="缺少可跟踪的中心轮盘表面，请补充穿过轮辐的截面")
        return result
    try:
        bases, all_edits = _recover_surfaces(np.asarray(traces), radii, angles, feature_tolerance_mm)
    except ValueError as exc:
        result.update(status="needs_review", radial_range_mm=None, reason=str(exc))
        return result
    # Each cap is a radial line at constant axial position: its revolution is
    # planar. Force independent segments even at a tangent join with the body.
    for (candidate, record), base, edits, events in zip(eligible,bases,all_edits,branch_events):
        if candidate["candidate_id"] not in rim_evidence:
            continue
        try:
            if base[1,0]-base[0,0] <= 2*feature_tolerance_mm:
                raise ValueError("中心封孔延伸导致厚度不足，不能自动闭合")
            rim = _rim_path(record["local"], cut)
            if np.max(np.abs(rim[[0, -1], 0]-base[:, -1])) > feature_tolerance_mm:
                raise ValueError("基础轮盘与原轮辋连接不连续")
            low = np.vstack([[base[0,0],0.], np.c_[base[0,:-1],radii[:-1]]])
            high = np.vstack([[base[1,0],0.], np.c_[base[1,:-1],radii[:-1]]])
            polygon = np.vstack([low, rim, high[::-1]])
            if not _simple_polygon(polygon):
                raise ValueError("重建材料截面自交或退化")
            frame = sections[record["section"]]["frame"]
            xyz = (np.asarray(frame["origin"])+polygon[:, :1]*np.asarray(frame["axis_direction"])
                   +polygon[:, 1:]*np.asarray(frame["radial_direction"]))
            rebuilt = {**record, "local": polygon, "xyz": xyz}
            n = len(polygon)
            segments = split_material_contour(rebuilt, forced_break_indices=(0,1,n-2,n-1))
            line_roles = {(0,1): "center_cap_low", (n-2,n-1): "center_cap_high", (n-1,0): "axis_closure"}
            for segment in segments:
                role = line_roles.get(tuple(segment["source_point_indices"]))
                if role:
                    segment.update(geometry_kind="line", line_role=role)
            c = copy.deepcopy(candidate)
            c.update(target=TARGET, reconstruction={"algorithm": ALGORITHM, "parameters": params,
                "effective_disc_radius_mm": cut, "removed_local_features": edits,
                "tracking_start_radius_mm": float(radii[0]), "local_branch_events": events,
                "bore_transition_interval_mm": [float(inner),float(radii[0])],
                "reference_angles": angles, "center_policy": "planar_radial_lines_to_axis",
                "rim_policy": "angularly_checked_original_path", "rim_consistency":rim_evidence[candidate["candidate_id"]],
                "source_artifact_unchanged": True},
                segments=segments, minimum_radius_mm=0.,
                radial_span_mm=float(polygon[:, 1].max()), source_length_coverage=None,
                completeness_definition="base_surfaces_plus_original_rim_plus_axis_closure")
            for s in c["segments"]:
                s["source_kind"] = "deterministic_base_reconstruction"
                # These indices reference the rebuilt boundary, not STL vertices.
                s["reconstructed_point_indices"] = s.pop("source_point_indices")
            result["candidates"].append(c)
        except ValueError as exc:
            result["diagnostics"]["base_reconstruction"]["rejected"].append({"angle_degrees": candidate["angle_degrees"], "reason": str(exc)})
    result["warnings"].append("主体重建会主动去除局部特征并封中心孔；生成区域与原STL的差异不能作为保留区域的拟合误差。自动轮盘分区为启发式，存在歧义时需补充截面或明确轮盘半径。")
    if result["candidates"]:
        result["candidates"].sort(key=lambda c:(c["reconstruction"]["rim_consistency"]["shape_disagreement_rms_mm"],
            -(c["support_ratio"] or 0.),c["angle_degrees"]))
        result.update(status="candidates_found", reason=None, radial_range_mm=[0., outer])
    else:
        result.update(status="needs_review", reason="基础轮盘未形成有效闭合材料截面", radial_range_mm=None)
    return result


def analyze_base_candidates(sections, parameters):
    """Single replay entry point shared by analysis and source validation."""
    from .rim_profile_selector import analyze_candidates
    raw = analyze_candidates(sections, **{k: parameters[k] for k in
        ("consistency_tolerance_mm", "min_support_ratio", "tangent_angle_degrees") if k in parameters})
    return reconstruct_base_profiles(raw, sections, **{k: parameters[k] for k in
        ("disc_radius_mm", "feature_tolerance_mm") if k in parameters})
