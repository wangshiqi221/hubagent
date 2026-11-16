"""Area-uniform sampled distances to triangle interiors (not mesh vertices)."""
import numpy as np
from scipy.spatial import cKDTree

from ..adapters.cpp_worker_adapter import check_cancelled


def triangles_array(triangles):
    tri = np.asarray(triangles, dtype=float)
    if tri.ndim != 3 or tri.shape[1:] != (3, 3) or not np.isfinite(tri).all():
        raise ValueError("表面必须为有限的三角形数组 (N,3,3)")
    area = np.linalg.norm(np.cross(tri[:, 1]-tri[:, 0], tri[:, 2]-tri[:, 0]), axis=1)/2
    valid = area > 0
    if not valid.any():
        raise ValueError("表面没有非退化三角形")
    return tri[valid], area[valid]


def sample_surface(tri, area, count, rng):
    selected = tri[rng.choice(len(tri), count, p=area/area.sum())]
    u, v = rng.random((2, count))
    u = np.sqrt(u)
    return ((1-u)[:, None]*selected[:, 0] + (u*(1-v))[:, None]*selected[:, 1]
            + (u*v)[:, None]*selected[:, 2])


def point_triangle_distances(point, tri):
    """Exact point-to-triangle distance, including edges and interiors."""
    a, b, c = tri[:, 0], tri[:, 1], tri[:, 2]
    ab, ac, ap = b-a, c-a, point-a
    normal = np.cross(ab, ac)
    normal /= np.linalg.norm(normal, axis=1)[:, None]
    height = np.einsum('ij,ij->i', ap, normal)
    projected = point-height[:, None]*normal
    inside = np.ones(len(tri), dtype=bool)
    edge_min = np.full(len(tri), np.inf)
    for start, end in ((a, b), (b, c), (c, a)):
        edge = end-start
        length2 = np.einsum('ij,ij->i', edge, edge)
        t = np.clip(np.einsum('ij,ij->i', point-start, edge)/length2, 0, 1)
        edge_min = np.minimum(edge_min, np.linalg.norm(point-(start+t[:, None]*edge), axis=1))
        side = np.einsum('ij,ij->i', np.cross(edge, projected-start), normal)
        inside &= side >= 0
    return np.where(inside, np.abs(height), edge_min)


def surface_distances(points, triangles, cancel_event=None):
    centers = triangles.mean(axis=1)
    radii = np.linalg.norm(triangles-centers[:, None, :], axis=2).max(axis=1)
    tree = cKDTree(centers)
    _, initial = tree.query(points)
    distances = np.empty(len(points))
    # A triangle can improve distance d only if its center lies within d+radius.
    # The global maximum radius makes this candidate search conservative.
    max_radius = float(radii.max())
    for i, (point, first) in enumerate(zip(points, initial)):
        if i % 64 == 0:
            check_cancelled(cancel_event)
        bound = point_triangle_distances(point, triangles[first:first+1])[0]
        ids = np.asarray(tree.query_ball_point(point, np.nextafter(bound+max_radius, np.inf)), dtype=int)
        lower = np.linalg.norm(centers[ids]-point, axis=1)-radii[ids]
        ids = ids[lower <= np.nextafter(bound, np.inf)]
        best = bound
        for offset in range(0, len(ids), 4096):
            check_cancelled(cancel_event)
            best = min(best, float(point_triangle_distances(point, triangles[ids[offset:offset+4096]]).min()))
        distances[i] = best
    return distances


def summarize(distances, points, tolerances):
    index = int(np.argmax(distances))
    return {"mae_mm": float(distances.mean()),
            "rms_mm": float(np.sqrt(np.mean(distances**2))),
            "p95_mm": float(np.percentile(distances, 95)),
            "sampled_max_mm": float(distances[index]),
            "sampled_max_location_mm": points[index].tolist(),
            "area_ratios": [{"tolerance_mm": float(tolerance),
                             "within_tolerance_area_percent": float(np.mean(distances <= tolerance)*100)}
                            for tolerance in tolerances],
            "sample_count": len(distances)}


def compare_surfaces(reference, reconstructed, *, sample_count=5000, tolerances_mm=(.2, 1., 5.),
                     seed=2026, cancel_event=None, region_spec=None):
    reference, ref_area = triangles_array(reference)
    reconstructed, model_area = triangles_array(reconstructed)
    rng = np.random.default_rng(seed)
    model_points = sample_surface(reconstructed, model_area, sample_count, rng)
    backward = surface_distances(model_points, reference, cancel_event)
    check_cancelled(cancel_event)
    result = {"direction": "model_to_reference",
            "model_to_reference": summarize(backward, model_points, tolerances_mm),
            "reference_area_mm2": float(ref_area.sum()),
            "reconstructed_mesh_area_mm2": float(model_area.sum())}
    if region_spec is not None:
        origin = np.asarray(region_spec["origin"], dtype=float)
        axis = np.asarray(region_spec["direction"], dtype=float)
        cut = region_spec["disc_radius_mm"]
        if (origin.shape != (3,) or axis.shape != (3,) or not np.isfinite(origin).all()
                or not np.isfinite(axis).all() or np.linalg.norm(axis) <= 1e-12
                or type(cut) not in (int, float) or not np.isfinite(cut) or cut <= 0):
            raise ValueError("区域评估需要有效轴线和轮盘半径")
        axis = axis/np.linalg.norm(axis)
        delta = model_points-origin
        radius = np.linalg.norm(delta-(delta @ axis)[:, None]*axis, axis=1)
        result["regions"] = {}
        for name, mask in (("protected_rim", radius >= cut), ("reconstructed_disc", radius < cut)):
            result["regions"][name] = {"sample_count": int(mask.sum()),
                "fraction_of_global_samples": float(mask.mean()),
                "metrics": summarize(backward[mask], model_points[mask], tolerances_mm) if mask.any() else None,
                "interpretation": "preserved_geometry_distance" if name == "protected_rim" else "includes_intentional_design_changes_not_fitting_error"}
    return result
