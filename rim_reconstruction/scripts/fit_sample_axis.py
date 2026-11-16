"""Fit a shared axis to two outer lip rings; retain held-out angular sectors.

This is a sample investigation, not a production rim extraction algorithm.
Requires NumPy, SciPy, Matplotlib and the existing basic inspection report.
"""

import argparse
import hashlib
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from scipy.optimize import least_squares

from preview_sample_sections import DTYPE


def extract_lip_points(source: Path, inspection: dict, radial_cut: float = 250.) -> tuple:
    """Pick min/max X vertices in 1-degree sectors outside a provisional radius.

    The sample is already approximately X aligned. This selection is explicitly
    sample-specific and does not identify the full rim material boundary.
    """
    before = source.stat()
    if source.resolve().as_posix() != inspection["source_path"]:
        raise ValueError("Source differs from basic report")
    with source.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != inspection["source_sha256"]:
        raise ValueError("Source hash changed")
    count = inspection["triangle_count"]
    if before.st_size != 84 + 50 * count:
        raise ValueError("Binary STL size mismatch")
    records = np.memmap(source, dtype=DTYPE, mode="r", offset=84, shape=(count,))
    minimum = np.full(360, np.inf)
    maximum = np.full(360, -np.inf)
    low_points = np.zeros((360, 3))
    high_points = np.zeros((360, 3))
    for start in range(0, count, 100_000):
        points = records["vertices"][start:start+100_000].reshape(-1, 3).astype(float)
        points = points[np.hypot(points[:, 1], points[:, 2]) >= radial_cut]
        if not len(points):
            continue
        bins = (np.floor(np.degrees(np.arctan2(points[:, 2], points[:, 1]))) % 360).astype(int)
        # Lexicographic sorting selects exact extrema, independent of chunk size.
        order = np.lexsort((points[:, 0], bins))
        sorted_bins = bins[order]
        first = np.r_[True, sorted_bins[1:] != sorted_bins[:-1]]
        last = np.r_[sorted_bins[1:] != sorted_bins[:-1], True]
        for selected, extrema, stored, is_min in [
            (order[first], minimum, low_points, True),
            (order[last], maximum, high_points, False),
        ]:
            ids = bins[selected]
            xs = points[selected, 0]
            better = xs < extrema[ids] if is_min else xs > extrema[ids]
            extrema[ids[better]] = xs[better]
            stored[ids[better]] = points[selected[better]]
    del records
    after = source.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("Source changed during extraction")
    if not np.isfinite(minimum).all() or not np.isfinite(maximum).all():
        raise ValueError("Some angular sectors lack lip points")
    return np.vstack((low_points, high_points)), np.repeat([0, 1], 360), np.tile(np.arange(360), 2), digest


def frame(parameters):
    axis = np.array([1., parameters[2], parameters[3]])
    axis /= np.linalg.norm(axis)
    origin = np.array([0., parameters[0], parameters[1]])
    return origin, axis


def residual_components(parameters, points, sides):
    origin, axis = frame(parameters)
    offsets = points - origin
    axial = offsets @ axis
    radial = np.linalg.norm(offsets - axial[:, None] * axis, axis=1)
    return np.column_stack((axial - parameters[4:6][sides], radial - parameters[6:8][sides]))


def solve(points, sides):
    # Plane x = c + b*y + d*z supplies a tilt initialization for each ring.
    slopes = []
    for side in [0, 1]:
        ring = points[sides == side]
        slopes.append(np.linalg.lstsq(np.column_stack((np.ones(len(ring)), ring[:, 1:])), ring[:, 0], rcond=None)[0])
    tilt = -np.mean(slopes, axis=0)[1:]
    initial = np.array([0., 0., *tilt, slopes[0][0], slopes[1][0], 262., 262.])
    result = least_squares(lambda p: residual_components(p, points, sides).ravel(), initial,
                           loss="soft_l1", f_scale=0.03, max_nfev=300, x_scale="jac")
    if not result.success or not np.isfinite(result.x).all():
        raise ValueError(f"Axis optimization failed: {result.message}")
    return result.x


def metrics(residuals, tolerance):
    distance = np.linalg.norm(residuals, axis=1)
    return {"point_count": len(distance), "rms_mm": float(np.sqrt(np.mean(distance**2))),
            "maximum_mm": float(distance.max()), "p95_mm": float(np.percentile(distance, 95)),
            "points_above_tolerance": int((distance > tolerance).sum())}


def run(config_path: Path, inspection_path: Path, output: Path):
    config = json.loads(config_path.read_text(encoding="utf-8"))
    inspection = json.loads(inspection_path.read_text(encoding="utf-8"))
    if config["unit"] != "mm" or inspection["unit"] != "mm":
        raise ValueError("Expected millimeter units")
    tolerance = config["tolerances"]["reconstruction"]
    points, sides, bins, digest = extract_lip_points(Path(config["stl_path"]), inspection)
    train = (bins // 15) % 2 == 0
    trained = solve(points[train], sides[train])
    validation = residual_components(trained, points[~train], sides[~train])
    fitted = solve(points, sides)
    full_residuals = residual_components(fitted, points, sides)
    origin, axis = frame(fitted)
    pca = json.loads(Path("docs/sample_review/axis_sections_report.json").read_text(encoding="utf-8"))
    pca_origin = np.array(pca["axis_candidate"]["origin"])
    pca_axis = np.array(pca["axis_candidate"]["direction"])
    offsets = points - pca_origin
    axial = offsets @ pca_axis
    radial = np.linalg.norm(offsets - axial[:, None] * pca_axis, axis=1)
    previous_residuals = np.column_stack([
        axial - np.array([np.median(axial[sides==s]) for s in [0,1]])[sides],
        radial - np.array([np.median(radial[sides==s]) for s in [0,1]])[sides],
    ])
    validation_metrics = metrics(validation, tolerance)
    report = {
        "source_path": config["stl_path"], "source_sha256": digest, "unit": "mm",
        "method": "Robust shared-axis two-circle fit to angular-bin lip extrema",
        "selection": {"provisional_global_yz_radius_minimum_mm": 250., "angular_bin_degrees": 1,
            "point_count_per_ring": 360, "rule": "minimum/maximum global X vertex in each bin; no interpolation"},
        "axis_candidate": {"origin": origin.tolist(), "direction": axis.tolist(), "confirmed": False,
            "method": "Shared-axis two-circle fit to outer lip extrema; all 720 selected vertices",
            "limitations": "Sample-specific lip selection; validation applies only to selected lip vertices"},
        "ring_axial_offsets_mm": fitted[4:6].tolist(), "ring_radii_mm": fitted[6:8].tolist(),
        "pca_axis_selected_ring_residuals": metrics(previous_residuals, tolerance),
        "full_fit_selected_ring_residuals": metrics(full_residuals, tolerance),
        "held_out_validation": {"split": "Alternating 15-degree sectors over 360 degrees; 360 train and 360 held-out vertices",
            "training_axis_origin": frame(trained)[0].tolist(), "training_axis_direction": frame(trained)[1].tolist(),
            "metrics": validation_metrics,
            "all_selected_held_out_points_within_0_08_mm": validation_metrics["points_above_tolerance"] == 0},
        "distance_definition": "Euclidean distance to each assigned 3D circle: sqrt(axial_plane_error^2 + radial_radius_error^2)",
        "limitations": ["No proof of true axis accuracy or final reconstruction tolerance",
            "Selected vertices are not a uniform-area surface sample",
            "Rim target boundary, full material profile and non-revolution features are not validated",
            "Full-data fit is a candidate; held-out metrics use separate training-only parameters"],
    }
    output.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(output / "lip_ring_points.npz", points_xyz=points, ring_side=sides, angle_bin=bins, training_mask=train)
    (output / "axis_fit_report.json").write_text(json.dumps(report, indent=2, allow_nan=False)+"\n", encoding="utf-8")
    fig, panels = plt.subplots(2, 1, figsize=(12, 7), constrained_layout=True)
    for side, label in [(0, "Negative-X lip"), (1, "Positive-X lip")]:
        mask = sides == side
        panels[0].plot(bins[mask]+0.5, np.linalg.norm(previous_residuals[mask], axis=1), label=label+" / PCA")
        panels[0].plot(bins[mask]+0.5, np.linalg.norm(full_residuals[mask], axis=1), label=label+" / ring fit")
    panels[0].set(ylabel="Distance to assigned circle (mm)", title="Same selected lip vertices: PCA axis vs shared circle-fit axis")
    for side, label in [(0, "Negative-X lip"), (1, "Positive-X lip")]:
        mask = sides[~train] == side
        panels[1].scatter(bins[~train][mask]+0.5, np.linalg.norm(validation[mask], axis=1), s=7, label=label)
    panels[1].axhline(tolerance, color="red", linestyle="--", label="0.08 mm target")
    panels[1].set(xlabel="Global YZ angular bin (degrees)", ylabel="Held-out circle distance (mm)", title="Held-out 15-degree sectors; training-only axis")
    for panel in panels:
        panel.grid(alpha=0.2)
        panel.legend(fontsize=8)
    fig.savefig(output / "axis_fit_validation.png", dpi=160)
    plt.close(fig)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config/reconstruction_config.json"))
    parser.add_argument("--inspection", type=Path, default=Path("docs/sample_inspection.json"))
    parser.add_argument("--output", type=Path, default=Path("docs/axis_refinement"))
    args = parser.parse_args()
    run(args.config, args.inspection, args.output)
