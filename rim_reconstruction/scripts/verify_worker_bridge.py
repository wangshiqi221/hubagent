"""Exercise the local Python/C++ bridge without an LLM or FreeCAD connection."""

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from adapters.cpp_worker_adapter import CppWorkerAdapter, WorkerError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker-config", type=Path, default=ROOT / "config/geometry_worker.json")
    parser.add_argument("--sample-config", type=Path, default=ROOT / "config/reconstruction_config.json")
    parser.add_argument("--test-frame-x", action="store_true", help="Also test sections with explicit X axis / Y radial; does not approve that axis")
    args = parser.parse_args()
    sample = json.loads(args.sample_config.read_text(encoding="utf-8"))
    client = CppWorkerAdapter.from_config(args.worker_config)
    source = Path(sample["stl_path"])
    if not source.is_absolute():
        source = client.base_directory / source
    common = {"task_id": sample["task_id"], "stl_path": str(source.resolve()), "unit": sample["unit"]}
    report = {"status": "success", "source_path": common["stl_path"], "calls": [],
              "reconstruction_verified": False, "formal_axis_configuration_changed": False}
    operations = [{**common, "operation": "inspect_stl"}, {**common, "operation": "estimate_axis"}]
    if args.test_frame_x:
        operations.append({**common, "operation": "compute_sections", "axis": {"origin": [0,0,0], "direction": [1,0,0]},
                           "radial_reference": [0,1,0], "angles_degrees": sample["inspection_angles_degrees"]})
    try:
        for request in operations:
            response = client.run(request)
            result = response.result
            report["calls"].append({"operation": request["operation"], "call_directory": str(response.call_directory),
                "returncode": response.returncode, "triangle_count": result["metrics"]["triangle_count"],
                "artifacts": result["artifacts"], "section_test_frame_only": request["operation"] == "compute_sections"})
    except WorkerError as error:
        report.update(status="error", error={"code": error.code, "message": str(error),
                       "call_directory": str(error.call_directory) if error.call_directory else None})
    client.call_root.mkdir(parents=True, exist_ok=True)
    destination = client.call_root / "step04_validation.json"
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
