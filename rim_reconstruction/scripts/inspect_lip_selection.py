"""Diagnose global-X lip extrema against axial extrema in a fixed candidate frame.

No axis is approved here. Selection uses the earlier all-data axis candidate,
so any refit statistics are diagnostic, not an independent validation result.
"""

import argparse
import hashlib
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from fit_sample_axis import frame, metrics, residual_components, solve
from preview_sample_sections import DTYPE


def local_coordinates(points, origin, axis):
    delta = points - origin
    axial = delta @ axis
    radial = np.linalg.norm(delta - axial[:, None] * axis, axis=1)
    return np.column_stack((axial, radial))


def pick_extrema(points, bins, values, limits, stored, minimum):
    order = np.lexsort((values, bins))
    ordered_bins = bins[order]
    boundary = np.r_[True, ordered_bins[1:] != ordered_bins[:-1]] if minimum else np.r_[ordered_bins[1:] != ordered_bins[:-1], True]
    ids = order[boundary]
    b = bins[ids]
    better = values[ids] < limits[b] if minimum else values[ids] > limits[b]
    limits[b[better]] = values[ids[better]]
    stored[b[better]] = points[ids[better]]


def run(config_path, inspection_path, axis_report_path, old_points_path, output):
    config = json.loads(config_path.read_text(encoding="utf-8"))
    inspection = json.loads(inspection_path.read_text(encoding="utf-8"))
    axis_report = json.loads(axis_report_path.read_text(encoding="utf-8"))
    old_data = np.load(old_points_path)
    old = old_data["points_xyz"]
    source = Path(config["stl_path"])
    before = source.stat()
    with source.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != inspection["source_sha256"] or digest != axis_report["source_sha256"]:
        raise ValueError("Source fingerprints differ")
    if config["unit"] != "mm" or inspection["unit"] != "mm" or axis_report["unit"] != "mm":
        raise ValueError("Expected millimeters")
    count = inspection["triangle_count"]
    if before.st_size != 84 + 50 * count:
        raise ValueError("Incorrect binary STL length")
    origin = np.array(axis_report["axis_candidate"]["origin"])
    axis = np.array(axis_report["axis_candidate"]["direction"])
    records = np.memmap(source, dtype=DTYPE, mode="r", offset=84, shape=(count,))
    low, high = np.full(360, np.inf), np.full(360, -np.inf)
    low_points, high_points = np.zeros((360, 3)), np.zeros((360, 3))
    samples = []
    # Keep the same global-YZ bins and radial mask as the earlier extraction:
    # only the optimization quantity changes from global X to candidate-axis t.
    for start in range(0, count, 100_000):
        points = records["vertices"][start:start+100_000].reshape(-1, 3).astype(float)
        points = points[np.hypot(points[:, 1], points[:, 2]) >= 250.]
        if not len(points):
            continue
        bins = (np.floor(np.degrees(np.arctan2(points[:, 2], points[:, 1]))) % 360).astype(int)
        tr = local_coordinates(points, origin, axis)
        pick_extrema(points, bins, tr[:, 0], low, low_points, True)
        pick_extrema(points, bins, tr[:, 0], high, high_points, False)
        # Deterministic display sample, not a uniform-area surface sample.
        samples.append(np.column_stack((bins[::20], tr[::20])))
    if not np.isfinite(low).all() or not np.isfinite(high).all():
        raise ValueError("Missing angular bins")
    # A second full pass measures radial width near each sector's axial extreme.
    # Width is geometry in a 1-degree sector, not a measured planar face width.
    radial_low = np.full((2, 360), np.inf)
    radial_high = np.full((2, 360), -np.inf)
    near_counts = np.zeros((2, 360), dtype=int)
    for start in range(0, count, 100_000):
        points = records["vertices"][start:start+100_000].reshape(-1, 3).astype(float)
        points = points[np.hypot(points[:, 1], points[:, 2]) >= 250.]
        if not len(points):
            continue
        bins = (np.floor(np.degrees(np.arctan2(points[:, 2], points[:, 1]))) % 360).astype(int)
        tr = local_coordinates(points, origin, axis)
        for side, boundary in [(0, low), (1, high)]:
            near = np.abs(tr[:, 0] - boundary[bins]) <= 0.02
            ids, radii = bins[near], tr[near, 1]
            np.minimum.at(radial_low[side], ids, radii)
            np.maximum.at(radial_high[side], ids, radii)
            np.add.at(near_counts[side], ids, 1)
    del records
    after = source.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("Source changed during inspection")
    selected = np.vstack((low_points, high_points))
    sides = np.repeat([0, 1], 360)
    bins = np.tile(np.arange(360), 2)
    old_tr = local_coordinates(old, origin, axis)
    selected_tr = local_coordinates(selected, origin, axis)
    displacement = np.linalg.norm(selected-old, axis=1)
    width = radial_high - radial_low
    old_fit = solve(old, sides)
    selected_fit = solve(selected, sides)
    original_distance = np.linalg.norm(residual_components(old_fit, old, sides), axis=1)
    diagnostic = metrics(residual_components(selected_fit, selected, sides), config["tolerances"]["reconstruction"])
    surface = np.concatenate(samples)
    output.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(output / "selection_diagnostics.npz", original_points_xyz=old,
        axial_extreme_points_xyz=selected, ring_side=sides, global_angle_bin=bins,
        original_axial_radius=old_tr, axial_selection_axial_radius=selected_tr,
        near_extreme_radial_minimum=radial_low, near_extreme_radial_maximum=radial_high,
        near_extreme_vertex_occurrence_count=near_counts,
        surface_display_sample=surface)
    report = {
        "source_path": config["stl_path"], "source_sha256": digest, "unit": "mm",
        "selection_comparison": "Same global-YZ radial mask >=250 mm and 1-degree bins; compare extrema of global X vs fixed-candidate axial coordinate",
        "fixed_frame_axis": axis_report["axis_candidate"],
        "near_extreme_band_mm": 0.02,
        "sides": [{"side": side, "label": label,
            "selected_point_displacement_median_mm": float(np.median(displacement[sides==side])),
            "selected_point_displacement_maximum_mm": float(displacement[sides==side].max()),
            "points_changed_more_than_0_08_mm": int((displacement[sides==side]>0.08).sum()),
            "absolute_axial_change_median_mm": float(np.median(np.abs(selected_tr[sides==side,0]-old_tr[sides==side,0]))),
            "absolute_axial_change_maximum_mm": float(np.max(np.abs(selected_tr[sides==side,0]-old_tr[sides==side,0]))),
            "absolute_radial_change_median_mm": float(np.median(np.abs(selected_tr[sides==side,1]-old_tr[sides==side,1]))),
            "absolute_radial_change_maximum_mm": float(np.max(np.abs(selected_tr[sides==side,1]-old_tr[sides==side,1]))),
            "radial_width_near_extreme_median_mm": float(np.median(width[side])),
            "radial_width_near_extreme_maximum_mm": float(width[side].max()),
            "bins_with_width_above_0_08_mm": int((width[side]>0.08).sum()),
            "largest_original_circle_residual_bins": [
                {"global_angle_bin": int(b), "distance_mm": float(original_distance[side*360+b])}
                for b in np.argsort(original_distance[side*360:(side+1)*360])[-5:][::-1]]}
            for side, label in [(0, "negative_X_lip"), (1, "positive_X_lip")]],
        "original_selected_ring_full_fit": metrics(residual_components(old_fit, old, sides), config["tolerances"]["reconstruction"]),
        "axial_selected_ring_full_fit_diagnostic_only": diagnostic,
        "diagnostic_refit_axis": {"origin": frame(selected_fit)[0].tolist(), "direction": frame(selected_fit)[1].tolist(), "confirmed": False},
        "surface_display_sample_occurrence_count": len(surface),
        "limitations": ["Fixed frame was fitted using the earlier full selection; this is not independent held-out validation",
            "Near-extreme width includes angular variation within each 1-degree sector; does not prove planar faces",
            "Raw triangle vertices include repeated occurrences; display sampling is not area uniform",
            "No topology or final reconstruction error is measured"],
    }
    (output / "selection_report.json").write_text(json.dumps(report, indent=2, allow_nan=False)+"\n", encoding="utf-8")
    fig, panels = plt.subplots(2, 2, figsize=(13, 8), constrained_layout=True)
    for side, label in [(0,"Negative-X lip"), (1,"Positive-X lip")]:
        mask = sides==side
        panels[side,0].plot(np.arange(360)+0.5, old_tr[mask,1], linewidth=0.9, label="Global-X extrema")
        panels[side,0].plot(np.arange(360)+0.5, selected_tr[mask,1], linewidth=0.9, label="Axial extrema")
        panels[side,0].set(title=label+": selected radius", ylabel="Radius (mm)")
        panels[side,1].fill_between(np.arange(360)+0.5, radial_low[side], radial_high[side], alpha=0.5, label="Vertices within 0.02 mm of axial extreme")
        panels[side,1].set(title=label+": radial span near axial extreme", ylabel="Radius (mm)")
    for panel in panels.flat:
        panel.set_xlabel("Global YZ angular bin (degrees)")
        panel.legend(fontsize=7)
        panel.grid(alpha=0.2)
    fig.savefig(output / "selection_comparison.png", dpi=150)
    plt.close(fig)
    # Local t/r windows expose the surface surrounding selected points.
    fig, panels = plt.subplots(2,4,figsize=(16,8),constrained_layout=True)
    for side in [0,1]:
        for col, angle in enumerate([0,105,180,285]):
            panel = panels[side,col]
            near_angle = np.abs((surface[:,0]-angle+180)%360-180)<=2
            near_side = surface[:,1]<-100 if side==0 else surface[:,1]>100
            background = surface[near_angle & near_side]
            panel.scatter(background[:,1], background[:,2], s=2, color="#94a3b8", label="Surface vertices, +/-2 deg")
            ids = (sides==side) & (np.abs((bins-angle+180)%360-180)<=2)
            panel.scatter(old_tr[ids,0], old_tr[ids,1], s=20, marker="x", color="#ef4444", label="Global-X extrema")
            panel.scatter(selected_tr[ids,0], selected_tr[ids,1], s=20, marker="+", color="#2563eb", label="Axial extrema")
            center = float(np.median(selected_tr[ids,0]))
            panel.set(xlim=(center-1.5,center+0.8), ylim=(250,264), xlabel="Candidate axial coordinate (mm)", ylabel="Radius (mm)", title=f"{'Negative' if side==0 else 'Positive'} lip / {angle} deg")
            panel.grid(alpha=0.2)
            if side==0 and col==0:
                panel.legend(fontsize=6)
    fig.suptitle("Selected extrema against nearby outer surface vertices\nFixed unconfirmed frame; sampled vertex display, not fitted contours")
    fig.savefig(output / "surface_selection_windows.png",dpi=150)
    plt.close(fig)
    print(json.dumps(report,indent=2))


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config",type=Path,default=Path("config/reconstruction_config.json"))
    parser.add_argument("--inspection",type=Path,default=Path("docs/sample_inspection.json"))
    parser.add_argument("--axis-report",type=Path,default=Path("docs/axis_refinement/axis_fit_report.json"))
    parser.add_argument("--old-points",type=Path,default=Path("docs/axis_refinement/lip_ring_points.npz"))
    parser.add_argument("--output",type=Path,default=Path("docs/lip_selection_review"))
    args=parser.parse_args()
    run(args.config,args.inspection,args.axis_report,args.old_points,args.output)
