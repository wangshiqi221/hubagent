"""Inspect a binary STL in chunks without changing the source model.

This standalone sample check is not an Agent tool or a geometry worker.
Requires NumPy. Unit is supplied explicitly because STL has no reliable unit.
"""

import argparse
import hashlib
import json
import struct
from pathlib import Path

import numpy as np


def inspect_binary_stl(path: Path, unit: str) -> dict:
    before = path.stat()
    digest = hashlib.sha256()
    dtype = np.dtype([
        ("normal", "<f4", (3,)),
        ("vertices", "<f4", (3, 3)),
        ("attribute", "<u2"),
    ])
    minimum = np.full(3, np.inf)
    maximum = np.full(3, -np.inf)
    nonfinite_triangles = 0
    zero_area_triangles = 0
    nonfinite_normals = 0
    with path.open("rb") as source:
        header = source.read(84)
        if len(header) != 84:
            raise ValueError("Missing binary STL header")
        count = struct.unpack_from("<I", header, 80)[0]
        if count == 0 or before.st_size != 84 + 50 * count:
            raise ValueError("Empty STL or file size inconsistent with binary STL count")
        digest.update(header)
        remaining = count
        while remaining:
            batch_count = min(remaining, 100_000)
            block = source.read(batch_count * 50)
            if len(block) != batch_count * 50:
                raise ValueError("Truncated triangle data")
            digest.update(block)
            triangles = np.frombuffer(block, dtype=dtype)
            vertices = triangles["vertices"].astype(np.float64)
            finite = np.isfinite(vertices).all(axis=(1, 2))
            nonfinite_triangles += int((~finite).sum())
            nonfinite_normals += int((~np.isfinite(triangles["normal"]).all(axis=1)).sum())
            valid = vertices[finite]
            if len(valid):
                minimum = np.minimum(minimum, valid.min(axis=(0, 1)))
                maximum = np.maximum(maximum, valid.max(axis=(0, 1)))
                cross = np.cross(valid[:, 1] - valid[:, 0], valid[:, 2] - valid[:, 0])
                zero_area_triangles += int((cross == 0).all(axis=1).sum())
            remaining -= batch_count
        if source.read(1):
            raise ValueError("Unexpected trailing data")
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("Input changed during inspection")
    if nonfinite_triangles:
        raise ValueError(f"Non-finite coordinates in {nonfinite_triangles} triangles")
    return {
        "source_path": path.resolve().as_posix(),
        "source_sha256": digest.hexdigest(),
        "format": "binary_stl",
        "unit": unit,
        "unit_source": "explicit_user_input",
        "file_size_bytes": before.st_size,
        "triangle_count": count,
        "bounding_box": {
            "minimum_xyz": minimum.tolist(),
            "maximum_xyz": maximum.tolist(),
            "size_xyz": (maximum - minimum).tolist(),
            "center_xyz": ((maximum + minimum) / 2).tolist(),
        },
        "basic_checks": {
            "binary_record_size_consistent": True,
            "nonfinite_coordinate_triangle_count": nonfinite_triangles,
            "nonfinite_normal_count": nonfinite_normals,
            "exact_zero_area_triangle_count": zero_area_triangles,
        },
        "not_checked": [
            "boundary_edges_and_holes", "non_manifold_edges", "duplicate_faces",
            "self_intersections", "normal_orientation", "near_degenerate_triangles",
            "noise", "rotation_axis", "rim_target_region", "section_contours",
        ],
        "note": "Bounding-box center is not a confirmed rotation-axis origin; basic checks do not establish mesh validity.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--unit", required=True, choices=["mm"])
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    if args.source.resolve() == args.report.resolve():
        parser.error("Report must not overwrite the source STL")
    report = inspect_binary_stl(args.source, args.unit)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
