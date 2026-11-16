"""Sample inspection only: PCA axis candidate and raw half-plane sections.

Requires NumPy and Matplotlib. Does not modify STL or build CAD geometry.
"""

import argparse
import hashlib
import json
import struct
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.collections import LineCollection
import numpy as np

DTYPE = np.dtype([("normal", "<f4", (3,)), ("vertices", "<f4", (3, 3)), ("attribute", "<u2")])
ANGLES = [0, 30, 60, 90, 120, 150]


def analyze(source: Path, inspection: dict, output: Path) -> dict:
    if source.resolve().as_posix() != inspection["source_path"]:
        raise ValueError("Source path does not match inspection report")
    before = source.stat()
    digest = hashlib.sha256()
    with source.open("rb") as stream:
        header = stream.read(84)
        if len(header) != 84:
            raise ValueError("Truncated STL header")
        count = struct.unpack_from("<I", header, 80)[0]
        digest.update(header)
        if before.st_size != 84 + count * 50:
            raise ValueError("Invalid binary STL length")
        total = np.zeros(3)
        second = np.zeros((3, 3))
        for _ in range(0, count, 100_000):
            block = stream.read(min(100_000, count - _) * 50)
            digest.update(block)
            points = np.frombuffer(block, dtype=DTYPE)["vertices"].reshape(-1, 3).astype(float)
            if not np.isfinite(points).all():
                raise ValueError("Non-finite coordinates")
            total += points.sum(axis=0)
            second += points.T @ points
    if digest.hexdigest() != inspection["source_sha256"]:
        raise ValueError("Source hash differs from the basic inspection")
    mean = total / (3 * count)
    covariance = second / (3 * count) - np.outer(mean, mean)
    values, vectors = np.linalg.eigh(covariance)
    axis = vectors[:, 0]
    if axis[np.argmax(np.abs(axis))] < 0:
        axis = -axis
    origin = np.array(inspection["bounding_box"]["center_xyz"])
    reference = np.array([0., 1., 0.])
    if abs(reference @ axis) > 0.9:
        reference = np.array([0., 0., 1.])
    reference -= (reference @ axis) * axis
    reference /= np.linalg.norm(reference)
    tangent = np.cross(axis, reference)
    records = np.memmap(source, dtype=DTYPE, mode="r", offset=84, shape=(count,))
    segments = [[] for _ in ANGLES]
    coplanar = [0 for _ in ANGLES]
    for start in range(0, count, 100_000):
        vertices = records["vertices"][start:start + 100_000].astype(float) - origin
        for i, angle in enumerate(ANGLES):
            theta = np.deg2rad(angle)
            radial = np.cos(theta) * reference + np.sin(theta) * tangent
            normal = np.cross(axis, radial)
            distance = vertices @ normal
            distance[np.abs(distance) <= 1e-9] = 0
            edges_in_plane = np.zeros(len(vertices), dtype=bool)
            for j, k in [(0, 1), (1, 2), (2, 0)]:
                edges_in_plane |= (distance[:, j] == 0) & (distance[:, k] == 0)
            coplanar[i] += int(edges_in_plane.sum())
            selected = ((distance > 0).any(axis=1)
                        & (distance <= 0).any(axis=1) & ~edges_in_plane)
            tri, dist = vertices[selected], distance[selected]
            if not len(tri):
                continue
            hits = np.zeros((len(tri), 2, 3))
            hit_count = np.zeros(len(tri), dtype=int)
            for j, k in [(0, 1), (1, 2), (2, 0)]:
                mask = (dist[:, j] > 0) != (dist[:, k] > 0)
                ids = np.flatnonzero(mask)
                weight = dist[ids, j] / (dist[ids, j] - dist[ids, k])
                hits[ids, hit_count[ids]] = tri[ids, j] + weight[:, None] * (tri[ids, k] - tri[ids, j])
                hit_count[ids] += 1
            if not np.all(hit_count == 2):
                raise ValueError("Unexpected triangle-plane intersection count")
            local = np.stack((hits @ axis, hits @ radial), axis=-1)
            local = local[local[:, :, 1].max(axis=1) > 1e-9]
            for endpoint in [0, 1]:
                ids = np.flatnonzero(local[:, endpoint, 1] < 0)
                other = 1 - endpoint
                weight = -local[ids, endpoint, 1] / (local[ids, other, 1] - local[ids, endpoint, 1])
                local[ids, endpoint] += weight[:, None] * (local[ids, other] - local[ids, endpoint])
            local = local[np.linalg.norm(local[:, 1] - local[:, 0], axis=1) > 1e-9]
            if len(local):
                segments[i].append(local)
    del records
    after = source.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("Source changed during analysis")
    arrays = [np.concatenate(parts) if parts else np.empty((0, 2, 2)) for parts in segments]
    if any(not len(item) for item in arrays):
        raise ValueError("Empty section; review the candidate axis")
    output.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(output / "raw_sections.npz", **{f"angle_{angle}": arr for angle, arr in zip(ANGLES, arrays)})
    fig, panels = plt.subplots(2, 3, figsize=(14, 10), constrained_layout=True)
    limits = np.concatenate([arr.reshape(-1, 2) for arr in arrays])
    low, high = limits.min(axis=0), limits.max(axis=0)
    for panel, angle, arr in zip(panels.flat, ANGLES, arrays):
        panel.add_collection(LineCollection(arr, colors="#155e75", linewidths=0.6))
        panel.set(xlim=(low[0]-5, high[0]+5), ylim=(max(0, low[1]-5), high[1]+5),
                  xlabel="Axial coordinate (mm)", ylabel="Radius (mm)",
                  title=f"{angle} degrees | {len(arr):,} raw segments")
        panel.set_aspect("equal")
        panel.grid(alpha=0.2)
    fig.suptitle("Wheel sample: raw half-plane sections about the PCA axis candidate\nUnconfirmed axis; no contour repair, rim selection or fitting", fontsize=14)
    fig.savefig(output / "sections_preview.png", dpi=160)
    plt.close(fig)
    fig, panel = plt.subplots(figsize=(10, 6), constrained_layout=True)
    colors = plt.get_cmap("tab10")
    for i, (angle, arr) in enumerate(zip(ANGLES, arrays)):
        panel.add_collection(LineCollection(arr, colors=[colors(i)], linewidths=0.7, alpha=0.7, label=f"{angle} deg"))
    panel.set(xlim=(low[0]-5, high[0]+5), ylim=(180, high[1]+5),
              xlabel="Axial coordinate (mm)", ylabel="Radius (mm)",
              title="Outer-region overlay (r >= 180 mm for display only)\nNot an accepted rim boundary")
    panel.set_aspect("equal")
    panel.legend(loc="lower right", ncol=3)
    panel.grid(alpha=0.2)
    fig.savefig(output / "outer_region_overlay.png", dpi=160)
    plt.close(fig)
    report = {
        "source_path": source.resolve().as_posix(), "source_sha256": digest.hexdigest(), "unit": "mm",
        "axis_candidate": {"origin": origin.tolist(), "direction": axis.tolist(), "confirmed": False,
            "method": "Smallest PCA eigenvector of triangle-vertex occurrences; origin is bounding-box center",
            "limitations": "Vertex repetition and tessellation density affect PCA; not a fitted rotational surface axis",
            "eigenvalues_mm2": values.tolist(),
            "angle_to_positive_x_degrees": float(np.rad2deg(np.arccos(np.clip(axis[0], -1, 1))))},
        "section_frame": {"zero_angle_radial_direction": reference.tolist(),
            "positive_angle_tangent": tangent.tolist(),
            "definition": "Half-plane through axis; r >= 0; coordinates [axial, radius] relative to origin",
            "plane_zero_tolerance_mm": 1e-9},
        "sections": [{"angle_degrees": angle, "segment_count": len(arr),
            "skipped_triangles_with_coplanar_edges": skipped,
            "axial_range_mm": [float(arr[:,:,0].min()), float(arr[:,:,0].max())],
            "radial_range_mm": [float(arr[:,:,1].min()), float(arr[:,:,1].max())]}
            for angle, arr, skipped in zip(ANGLES, arrays, coplanar)],
        "artifacts": ["raw_sections.npz", "sections_preview.png", "outer_region_overlay.png"],
        "limitations": ["Raw segments only; no closure, connectivity or self-intersection validation",
            "Coplanar-edge triangles are counted and skipped; preview is not production intersection output",
            "The overlay crop at radius 180 mm is only for display, not rim extraction",
            "No axis accuracy or reconstruction error of 0.08 mm has been established"],
    }
    (output / "axis_sections_report.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config/reconstruction_config.json"))
    parser.add_argument("--inspection", type=Path, default=Path("docs/sample_inspection.json"))
    parser.add_argument("--output", type=Path, default=Path("docs/sample_review"))
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    inspection = json.loads(args.inspection.read_text(encoding="utf-8"))
    if config["unit"] != "mm" or inspection["unit"] != "mm":
        raise ValueError("Expected explicit millimeter units")
    result = analyze(Path(config["stl_path"]), inspection, args.output)
    print(json.dumps(result, indent=2))
