"""C++ Worker 适配器：协议转换、进程调用、持久化结果及几何数据校验。"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
import time
import sys
import threading
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 与 C++ SectionProcessingOptions 的清洗默认值一致；不包含拟合或路径选择参数。
PROCESSING_DEFAULTS = {
    "duplicate_tolerance_mm": 0.0,
    "enable_micro_bump_cleaning": True,
    "bump_detection_tolerance_mm": 0.02,
    "maximum_bump_width_mm": 5.0,
    "maximum_bump_displacement_mm": 2.0,
    "bump_reference_length_mm": 3.0,
    "bump_support_search_length_mm": 6.0,
    "bump_support_residual_mm": 0.01,
    "bump_tangent_angle_degrees": 15.0,
}
SECTION_OPERATIONS = ("compute_sections", "process_sections")

# 轮辋逆向扩展：FreeCAD/Windows 的 SetDllDirectory 会被子进程继承。
# 仅在 CreateProcess 期间清空并立即恢复，不能在整个计算期间改变宿主搜索目录。
_DLL_LAUNCH_LOCK = threading.Lock()


def _start_worker(command, options):
    if os.name != "nt" or "FreeCAD" not in sys.modules:
        return subprocess.Popen(command, **options)
    import ctypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetDllDirectoryW.argtypes = [ctypes.c_uint, ctypes.c_wchar_p]
    kernel.GetDllDirectoryW.restype = ctypes.c_uint
    kernel.SetDllDirectoryW.argtypes = [ctypes.c_wchar_p]
    kernel.SetDllDirectoryW.restype = ctypes.c_int
    with _DLL_LAUNCH_LOCK:
        buffer = ctypes.create_unicode_buffer(32768)
        length = kernel.GetDllDirectoryW(len(buffer), buffer)
        if length >= len(buffer):
            raise OSError("DLL 搜索目录长度超过限制")
        previous = buffer.value if length else None
        if not kernel.SetDllDirectoryW(None):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return subprocess.Popen(command, **options)
        finally:
            if not kernel.SetDllDirectoryW(previous):
                raise ctypes.WinError(ctypes.get_last_error())


class WorkerError(RuntimeError):
    def __init__(self, code: str, message: str, *, call_directory: Path | None = None,
                 returncode: int | None = None, worker_result: dict | None = None):
        super().__init__(message)
        self.code = code
        self.call_directory = call_directory
        self.returncode = returncode
        self.worker_result = worker_result


class WorkerTimeoutError(WorkerError):
    pass


class WorkerCancelledError(WorkerError):
    """调用方取消；部分输出不能作为成功产物。"""


def check_cancelled(cancel_event, call_directory=None):
    """统一取消检查；保留已有调用目录，供界面诊断。"""
    if cancel_event is not None and cancel_event.is_set():
        raise WorkerCancelledError("WORKER_CANCELLED", "用户取消了截面任务",
                                   call_directory=call_directory)


def _kill_and_wait(process):
    """只终止本次直接启动的 Worker，并等待操作系统回收；容忍退出竞态。"""
    try:
        if process.poll() is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
    finally:
        process.wait()


class WorkerProcessError(WorkerError):
    pass


class WorkerProtocolError(WorkerError):
    pass


class WorkerOperationError(WorkerError):
    pass


@dataclass(frozen=True)
class WorkerResponse:
    result: dict
    call_directory: Path
    returncode: int


def _object_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _bad_constant(value):
    raise ValueError(f"Non-finite JSON number: {value}")


def _finite_float(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Non-finite JSON number")
    return result


def _read_json(path: Path, limit: int) -> dict:
    if path.stat().st_size > limit:
        raise ValueError(f"JSON exceeds {limit} bytes: {path}")
    result = json.loads(path.read_text(encoding="utf-8-sig"),
                        object_pairs_hook=_object_pairs, parse_constant=_bad_constant, parse_float=_finite_float)
    if not isinstance(result, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return result


def _number(value: Any) -> bool:
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _vector(value: Any) -> list[float]:
    if not isinstance(value, list) or len(value) != 3 or not all(_number(x) for x in value):
        raise ValueError("Expected three finite coordinates")
    return value


def _integer(value: Any, minimum: int) -> bool:
    return type(value) is int and value >= minimum


def _normalized(value):
    vector = _vector(value)
    length = math.hypot(*vector)
    if not math.isfinite(length) or length <= 1e-12:
        raise ValueError("Invalid direction length")
    return [x / length for x in vector]


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def _cross(a, b):
    return [a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]]


class CppWorkerAdapter:
    """Python 与 C++ 的桥接层：请求转换 → 子进程调用 → 结果和几何数据校验。"""
    def __init__(self, executable_path: str | Path | None = None, call_directory: str | Path | None = None, *,
                 config_path: str | Path = PROJECT_ROOT / "config/rim_geometry_worker.json",
                 base_directory: str | Path = PROJECT_ROOT, timeout_seconds: float = 120,
                 max_result_bytes: int = 2 * 1024 * 1024,
                 max_artifact_bytes: int = 128 * 1024 * 1024):
        if call_directory is None:
            configured = type(self).from_config(config_path)
            self.__dict__.update(configured.__dict__)
            if executable_path is not None:
                self.executable = self._resolve(executable_path)
            return
        self.base_directory = Path(base_directory).resolve()
        self.executable = self._resolve(executable_path)
        self.call_root = self._resolve(call_directory)
        self.timeout_seconds = self._timeout(timeout_seconds)
        for limit in (max_result_bytes, max_artifact_bytes):
            if type(limit) is not int or limit <= 0:
                raise ValueError("JSON size limits must be positive integers")
        self.max_result_bytes = max_result_bytes
        self.max_artifact_bytes = max_artifact_bytes

    def _resolve(self, value):
        path = Path(value)
        return (path if path.is_absolute() else self.base_directory / path).resolve()

    @staticmethod
    def _timeout(value):
        if not _number(value) or value <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        return float(value)

    @classmethod
    def from_config(cls, path: str | Path = PROJECT_ROOT / "config/rim_geometry_worker.json"):
        path = Path(path).resolve()
        config = _read_json(path, 64 * 1024)
        if type(config.get("schema_version")) is not int or config["schema_version"] != 1:
            raise ValueError("Unsupported worker configuration version")
        base = (path.parent / config.get("base_directory", "..")).resolve()
        return cls(config["executable"], config["call_directory"], base_directory=base,
                   timeout_seconds=config.get("timeout_seconds", 120),
                   max_result_bytes=config.get("max_result_bytes", 2 * 1024 * 1024),
                   max_artifact_bytes=config.get("max_artifact_bytes", 128 * 1024 * 1024))

    def run(self, request: dict, *, timeout_seconds: float | None = None,
            cancel_event=None) -> WorkerResponse:
        """执行 C++ 协议请求。输出目录由适配器分配，调用方不要传 output_directory。"""
        timeout = self.timeout_seconds if timeout_seconds is None else self._timeout(timeout_seconds)
        check_cancelled(cancel_event)
        # Copy via JSON, rejecting non-finite numbers and non-serializable objects.
        if not isinstance(request, dict):
            raise ValueError("Request must be a dict")
        payload = json.loads(json.dumps(request, ensure_ascii=False, allow_nan=False))
        for field in ("task_id", "operation", "stl_path", "unit"):
            if not isinstance(payload.get(field), str) or not payload[field].strip():
                raise ValueError(f"{field} must be a non-empty string")
        payload.setdefault("schema_version", 1)
        payload["stl_path"] = str(self._resolve(payload["stl_path"]))
        if "output_directory" in payload:
            raise ValueError("output_directory is owned by the client; omit it from requests")
        if not self.executable.is_file():
            raise WorkerProcessError("WORKER_NOT_FOUND", f"Worker executable not found: {self.executable}")
        try:
            self.call_root.mkdir(parents=True, exist_ok=True)
            call = Path(tempfile.mkdtemp(prefix="call-", dir=self.call_root))
            # 两种截面操作都输出独立 JSON 产物，统一隔离，避免读到上次调用的数据。
            if payload["operation"] in SECTION_OPERATIONS:
                payload["output_directory"] = str(call / "geometry")
            request_path, result_path = call / "request.json", call / "result.json"
            encoded = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
            if len(encoded.encode("utf-8")) > 1024 * 1024:
                raise ValueError("Request exceeds worker's 1 MiB limit")
            request_path.write_text(encoded, encoding="utf-8")
        except OSError as error:
            raise WorkerProcessError("CLIENT_IO_ERROR", str(error)) from error
        command = [str(self.executable), "--request", str(request_path), "--result", str(result_path)]
        # 写入请求期间也可能点击停止，此时不要再创建 C++ 进程。
        check_cancelled(cancel_event, call)
        try:
            with (call / "stdout.log").open("wb") as stdout, (call / "stderr.log").open("wb") as stderr:
                options = dict(cwd=self.executable.parent, stdout=stdout, stderr=stderr,
                    shell=False, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                if cancel_event is None and not (os.name == "nt" and "FreeCAD" in sys.modules):
                    completed = subprocess.run(command, timeout=timeout, check=False, **options)
                else:
                    # Qt 后台任务使用可取消进程等待；终止后回收进程，不接受部分结果。
                    with _start_worker(command, options) as process:
                        deadline = time.monotonic() + timeout
                        while True:
                            cancelled = cancel_event is not None and cancel_event.is_set()
                            expired = time.monotonic() >= deadline
                            if cancelled or expired:
                                _kill_and_wait(process)
                                if cancelled:
                                    raise WorkerCancelledError("WORKER_CANCELLED", "用户取消了截面任务",
                                                               call_directory=call)
                                raise WorkerTimeoutError("WORKER_TIMEOUT", f"Worker exceeded {timeout:g} seconds",
                                                         call_directory=call)
                            try:
                                returncode = process.wait(timeout=0.1)
                                break
                            except subprocess.TimeoutExpired:
                                continue
                        # wait 返回与点击停止可能同时发生；取消优先于成功结果。
                        check_cancelled(cancel_event, call)
                        completed = subprocess.CompletedProcess(command, returncode)
        except subprocess.TimeoutExpired as error:
            # subprocess.run kills and waits for the direct worker before raising.
            raise WorkerTimeoutError("WORKER_TIMEOUT", f"Worker exceeded {timeout:g} seconds",
                                     call_directory=call) from error
        except OSError as error:
            raise WorkerProcessError("WORKER_START_FAILED", str(error), call_directory=call) from error
        check_cancelled(cancel_event, call)
        try:
            output = _read_json(call / "stdout.log", self.max_result_bytes)
        except (OSError, ValueError, RecursionError) as error:
            exception = WorkerProcessError if completed.returncode else WorkerProtocolError
            raise exception("INVALID_WORKER_OUTPUT", str(error), call_directory=call,
                            returncode=completed.returncode) from error
        if completed.returncode != 0:
            if completed.returncode not in (2, 3, 4, 5, 6):
                raise WorkerProcessError("WORKER_EXIT_ERROR", "Worker exited abnormally", call_directory=call,
                                         returncode=completed.returncode, worker_result=output)
            try:
                self._identity(output, payload)
                detail = output.get("error")
                if output.get("status") != "error" or not isinstance(detail, dict):
                    raise ValueError("Nonzero exit did not return an error envelope")
                if not isinstance(detail.get("code"), str) or not isinstance(detail.get("message"), str):
                    raise ValueError("Malformed worker error")
                if output.get("artifacts") != []:
                    raise ValueError("Failed worker exposed partial artifacts")
                if result_path.exists() and _read_json(result_path, self.max_result_bytes) != output:
                    raise ValueError("Error file and stdout disagree")
            except (OSError, ValueError, RecursionError) as error:
                raise WorkerProcessError("WORKER_EXIT_ERROR", str(error), call_directory=call,
                                         returncode=completed.returncode, worker_result=output) from error
            raise WorkerOperationError(detail["code"], detail["message"], call_directory=call,
                                       returncode=completed.returncode, worker_result=output)
        try:
            result = _read_json(result_path, self.max_result_bytes)
            if result != output:
                raise ValueError("Result file and stdout disagree")
            self._validate_result(result, payload, call)
        except (OSError, ValueError, KeyError, TypeError, OverflowError, RecursionError) as error:
            raise WorkerProtocolError("INVALID_WORKER_RESULT", str(error), call_directory=call,
                                      returncode=completed.returncode, worker_result=output) from error
        check_cancelled(cancel_event, call)
        return WorkerResponse(result, call, completed.returncode)

    @staticmethod
    def _identity(result, request):
        if type(result.get("schema_version")) is not int or result["schema_version"] != 1:
            raise ValueError("Unsupported result schema")
        for key in ("task_id", "operation"):
            if result.get(key) != request[key]:
                raise ValueError(f"Result {key} does not match this request")

    def _validate_result(self, result, request, call):
        self._identity(result, request)
        if type(request.get("schema_version")) is not int or request["schema_version"] != 1 or request["unit"] != "mm":
            raise ValueError("Worker accepted an unsupported request schema/unit")
        if request["operation"] not in ("inspect_stl", "estimate_axis", *SECTION_OPERATIONS):
            raise ValueError("Unsupported success operation")
        if result.get("status") != "success" or result.get("error", "missing") is not None or result.get("unit") != request["unit"]:
            raise ValueError("Invalid success envelope")
        if not isinstance(result.get("warnings"), list) or not all(isinstance(x, str) for x in result["warnings"]):
            raise ValueError("Invalid warnings")
        metrics, artifacts = result.get("metrics"), result.get("artifacts")
        if not isinstance(metrics, dict) or not isinstance(artifacts, list):
            raise ValueError("Missing metrics/artifacts")
        for name in ("triangle_count", "node_count", "file_size_bytes"):
            if not _integer(metrics.get(name), 1):
                raise ValueError(f"Invalid {name}")
        if Path(metrics["source_path"]).resolve() != Path(request["stl_path"]).resolve():
            raise ValueError("Result refers to another STL")
        bounds = metrics["bounding_box"]
        low, high = _vector(bounds["minimum"]), _vector(bounds["maximum"])
        _vector(bounds["center"])
        if any(a > b for a, b in zip(low, high)) or not _number(bounds["diagonal_mm"]) or bounds["diagonal_mm"] < 0:
            raise ValueError("Invalid mesh bounds")
        if request["operation"] in SECTION_OPERATIONS:
            self._validate_sections(result, request, call)
        elif artifacts:
            raise ValueError("Unexpected artifacts for non-section operation")
        elif request["operation"] == "estimate_axis":
            axis = metrics["axis_candidate"]
            _vector(axis["origin"])
            direction = _vector(axis["direction"])
            if abs(math.hypot(*direction)-1) > 1e-8 or axis.get("confirmed") is not False:
                raise ValueError("Axis candidate must be unit length and unconfirmed")
            if not _number(axis["outer_radius_mm"]) or axis["outer_radius_mm"] <= 0:
                raise ValueError("Invalid outer radius")

    def _validate_sections(self, result, request, call):
        # 处理后的截面仍保留原始线段，先复用路径、身份和半平面的原始数据检查。
        processed = request["operation"] == "process_sections"
        angles, artifacts = request["angles_degrees"], result["artifacts"]
        if (not isinstance(angles, list) or not 1 <= len(angles) <= 360
            or not all(_number(x) and 0 <= x < 360 for x in angles) or len(set(angles)) != len(angles)):
            raise ValueError("Worker accepted invalid requested angles")
        summaries = result["metrics"]["sections"]
        run_id = result["run_id"]
        if not isinstance(run_id, str) or not run_id or Path(run_id).name != run_id or run_id in (".", ".."):
            raise ValueError("Invalid run_id")
        root = (call / "geometry" / run_id).resolve()
        if not root.is_relative_to((call / "geometry").resolve()) or not root.is_relative_to(call.resolve()):
            raise ValueError("Run directory escapes call directory")
        if not isinstance(summaries, list) or len(artifacts) != len(angles) or len(summaries) != len(angles):
            raise ValueError("Section count differs from requested angles")
        axis = _normalized(request["axis"]["direction"])
        radial = _normalized(request["radial_reference"])
        origin = _vector(request["axis"]["origin"])
        seen_ids, seen_paths = set(), set()
        for angle, artifact, summary in zip(angles, artifacts, summaries):
            if not _number(artifact["angle_degrees"]) or not _number(summary["angle_degrees"]):
                raise ValueError("Invalid section angle type")
            expected_type = "processed_section" if processed else "raw_section"
            if artifact["type"] != expected_type or artifact["angle_degrees"] != angle or summary["angle_degrees"] != angle:
                raise ValueError("Section angle/type mismatch")
            section_id, count = artifact["artifact_id"], artifact["segment_count"]
            if not isinstance(section_id, str) or not section_id or section_id in seen_ids or not _integer(count, 0):
                raise ValueError("Invalid or repeated section identity/count")
            seen_ids.add(section_id)
            if not _integer(summary["segment_count"], 0) or summary["section_id"] != section_id or summary["segment_count"] != count:
                raise ValueError("Section metrics and artifact disagree")
            path = Path(artifact["path"]).resolve()
            if not path.is_relative_to(root) or path in seen_paths or not path.is_file():
                raise ValueError("Section artifact missing, repeated, or outside this run")
            seen_paths.add(path)
            section = _read_json(path, self.max_artifact_bytes)
            if type(section.get("schema_version")) is not int or not _number(section.get("angle_degrees")):
                raise ValueError("Invalid section schema/angle types")
            for name, expected in (("schema_version", 1), ("task_id", request["task_id"]), ("run_id", run_id),
                                   ("section_id", section_id), ("unit", "mm"), ("angle_degrees", angle)):
                if section.get(name) != expected:
                    raise ValueError(f"Section {name} mismatch")
            segments, frame = section["segments"], section["frame"]
            if not isinstance(segments, list) or len(segments) != count or frame["half_plane"] is not True:
                raise ValueError("Invalid section data/frame")
            theta = math.radians(angle)
            cross = _cross(axis, radial)
            expected_radial = [d*math.cos(theta)+c*math.sin(theta) for d, c in zip(radial, cross)]
            for name, expected in (("origin", origin), ("axis_direction", axis), ("radial_direction", expected_radial)):
                if math.dist(_vector(frame[name]), expected) > 1e-8:
                    raise ValueError("Artifact frame differs from requested frame")
            tolerances = section["tolerances"]
            for name in ("plane_mm", "minimum_segment_mm", "duplicate_point_mm"):
                if not _number(tolerances[name]) or tolerances[name] <= 0:
                    raise ValueError("Invalid geometric tolerance metadata")
            epsilon = max(2*tolerances["plane_mm"], 1e-8)
            normal = _cross(axis, expected_radial)
            for segment in segments:
                index = segment["triangle_index"]
                if not _integer(index, 1) or index > result["metrics"]["triangle_count"]:
                    raise ValueError("Invalid source triangle index")
                for endpoint in ("first", "second"):
                    offset = [x-y for x, y in zip(_vector(segment[endpoint]), origin)]
                    if abs(_dot(offset, normal)) > epsilon or _dot(offset, expected_radial) < -epsilon:
                        raise ValueError("Section endpoint is outside the requested half-plane")

            if processed:
                self._validate_processed_section(section, summary, result, request)

    @staticmethod
    def _validate_processed_section(section, summary, result, request):
        """检查有序点列和处理统计；closed 是算法状态，不等同于无自交认证。"""
        supplied = request.get("processing", {})
        if not isinstance(supplied, dict) or supplied.keys() - PROCESSING_DEFAULTS.keys():
            raise ValueError("Worker accepted invalid processing options")
        expected = {**PROCESSING_DEFAULTS, **supplied}
        for key, value in expected.items():
            if key == "enable_micro_bump_cleaning":
                if type(value) is not bool:
                    raise ValueError("Invalid bump-cleaning switch")
            elif not _number(value) or (value < 0 if key == "duplicate_tolerance_mm" else value <= 0):
                raise ValueError("Invalid processing parameter")
        if (expected["bump_support_search_length_mm"] < expected["bump_reference_length_mm"]
                or expected["bump_tangent_angle_degrees"] >= 90):
            raise ValueError("Invalid processing parameter range")
        for actual in (section["processing"], result["metrics"]["processing"]):
            # 显式检查类型，避免 JSON 的 true 被 Python 当作数字 1 接受。
            if (not isinstance(actual, dict) or actual.keys() != expected.keys()
                    or any(type(actual[k]) is not bool if type(v) is bool else not _number(actual[k])
                           for k, v in expected.items()) or actual != expected):
                raise ValueError("Processing options differ from this request")
        if section["closure_policy"] != "existing_algorithm_safe_closure":
            raise ValueError("Unexpected closure policy")

        def check_contours(contours):
            if not isinstance(contours, list):
                raise ValueError("Contours must be a list")
            for index, contour in enumerate(contours):
                if (not isinstance(contour, dict) or not _integer(contour.get("contour_index"), 0)
                        or contour["contour_index"] != index or type(contour.get("closed")) is not bool):
                    raise ValueError("Invalid contour identity/closure flag")
                points = contour["points"]
                if (not isinstance(points, list) or not _integer(contour["point_count"], 0)
                        or contour["point_count"] != len(points)):
                    raise ValueError("Contour point count mismatch")
                for point in points:
                    _vector(point)
                # 闭合点列不重复保存首点；不能根据两端点距离擅自修改 closed。
                if contour["closed"] and (len(points) < 3 or points[0] == points[-1]):
                    raise ValueError("Invalid closed contour")

        check_contours(section["contours"])
        # 修复前快照不保证与输出轮廓一一对应，分别校验，不按索引配对。
        check_contours(section["before_bump_contours"])
        stats = section["processing_metrics"]
        counts = ("contour_count", "closed_contour_count", "open_contour_count", "duplicate_point_count",
                  "invalid_point_count", "repaired_bump_count", "moved_bump_point_count", "bump_support_rejected",
                  "bump_shape_rejected", "bump_budget_rejected", "bump_safety_rejected")
        if any(not _integer(stats.get(key), 0) for key in counts):
            raise ValueError("Invalid processing count")
        movement = stats["maximum_bump_movement_mm"]
        if not _number(movement) or movement < 0:
            raise ValueError("Invalid bump movement")
        closed = sum(c["closed"] for c in section["contours"])
        if (stats["contour_count"] != len(section["contours"]) or stats["closed_contour_count"] != closed
                or stats["open_contour_count"] != len(section["contours"]) - closed
                or stats != summary["processing_metrics"]):
            raise ValueError("Processing summary disagrees with contours")
        if not expected["enable_micro_bump_cleaning"] and (
                stats["repaired_bump_count"] or stats["moved_bump_point_count"] or movement):
            raise ValueError("Disabled bump cleaning modified points")

    def load_sections(self, response: WorkerResponse, *, cancel_event=None) -> list[dict]:
        """读取已返回的截面文件，返回包含 contours/points 的字典，供 Python 拟合使用。"""
        check_cancelled(cancel_event, response.call_directory)
        request = _read_json(response.call_directory / "request.json", 1024 * 1024)
        if request["operation"] not in SECTION_OPERATIONS or response.returncode != 0:
            raise ValueError("A successful section response is required")
        # 再次校验身份和产物路径，避免把其他任务或被改动的数据交给拟合模块。
        self._validate_result(response.result, request, response.call_directory)
        sections = []
        for item in response.result["artifacts"]:
            check_cancelled(cancel_event, response.call_directory)
            sections.append(_read_json(Path(item["path"]), self.max_artifact_bytes))
        check_cancelled(cancel_event, response.call_directory)
        return sections


    def execute(self, operation, parameters):
        # 公开操作名与已有 C++ v1 协议之间的转换只在这里维护。
        # extract_section 保留原始线段；process_sections 返回排序和清洗后的轮廓。
        if operation in ("extract_section", "process_sections"):
            request = {
                "operation": "compute_sections" if operation == "extract_section" else "process_sections",
                "task_id": parameters.get("task_id", "section_task"),
                "unit": "mm", "stl_path": parameters["stl_path"],
                "axis": {"origin": parameters["axis_origin"],
                         "direction": parameters["axis_direction"]},
                "radial_reference": parameters["radial_reference"],
                "angles_degrees": [parameters["angle_deg"]],
            }
            if operation == "process_sections":
                request["processing"] = parameters.get("processing", {})
        else:
            return {"status": "error", "error": {"code": "UNSUPPORTED_OPERATION",
                    "message": f"适配器尚未支持操作: {operation}"}}
        try:
            response = self.run(request)
        except WorkerError as exc:
            return {"status": "error", "error": {"code": exc.code, "message": str(exc)},
                    "call_directory": str(exc.call_directory) if exc.call_directory else None}
        return {**response.result, "call_directory": str(response.call_directory)}

def run_geometry_worker(request: dict, *, config_path: str | Path = PROJECT_ROOT / "config/rim_geometry_worker.json",
                        timeout_seconds: float | None = None) -> WorkerResponse:
    return CppWorkerAdapter.from_config(config_path).run(request, timeout_seconds=timeout_seconds)
