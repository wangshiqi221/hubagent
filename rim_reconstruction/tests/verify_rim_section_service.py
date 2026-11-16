"""独立验证截面服务：不依赖 FreeCAD、Qt 和 DeepSeek。"""

import copy
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch


# 让 Python 可以找到 CadAgent 内的 rim_reconstruction 包。
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from rim_reconstruction.adapters.cpp_worker_adapter import (
    CppWorkerAdapter,
)
from rim_reconstruction.services.section_service import process_sections


def check(condition, message):
    """失败时立即终止，成功时输出检查结果。"""
    if not condition:
        raise RuntimeError(f"检查失败：{message}")
    print(f"[通过] {message}")


def create_tetrahedron_stl(path):
    """创建已知坐标的小型四面体 STL。"""
    vertices = [
        (0, 0, 0),
        (1, 0, 0),
        (0, 1, 0),
        (0, 0, 1),
    ]

    faces = [
        (0, 1, 2),
        (0, 3, 1),
        (0, 2, 3),
        (1, 3, 2),
    ]

    lines = ["solid service_test"]

    for face in faces:
        lines.extend(["facet normal 0 0 0", "outer loop"])

        for index in face:
            x, y, z = vertices[index]
            lines.append(f"vertex {x} {y} {z}")

        lines.extend(["endloop", "endfacet"])

    lines.append("endsolid service_test")
    path.write_text("\n".join(lines), encoding="utf-8")


def contains_points_key(value):
    """递归检查摘要中是否包含完整点列字段。"""
    if isinstance(value, dict):
        return (
            "points" in value
            or any(contains_points_key(v) for v in value.values())
        )

    if isinstance(value, list):
        return any(contains_points_key(v) for v in value)

    return False


def main():
    # 读取当前 CadAgent 的 Worker 配置。
    configured_adapter = CppWorkerAdapter.from_config()

    check(
        configured_adapter.executable.is_file(),
        f"Worker 存在：{configured_adapter.executable}",
    )

    # 测试数据放在项目 runtime 下，退出时自动清理。
    test_root = PROJECT_ROOT / "runtime" / "service_verification"
    test_root.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(
        prefix="test-",
        dir=test_root,
    ) as directory:
        temporary_root = Path(directory).resolve()
        check(
            temporary_root.is_relative_to(test_root.resolve()),
            "测试临时目录位于指定测试目录内",
        )

        stl_path = temporary_root / "tetrahedron.stl"
        create_tetrahedron_stl(stl_path)

        # 使用真实 Worker，但把本次输出隔离到临时目录。
        adapter = CppWorkerAdapter(
            executable_path=configured_adapter.executable,
            call_directory=temporary_root / "calls",
            base_directory=PROJECT_ROOT,
            timeout_seconds=configured_adapter.timeout_seconds,
            max_result_bytes=configured_adapter.max_result_bytes,
            max_artifact_bytes=configured_adapter.max_artifact_bytes,
        )

        parameters = {
            "task_id": "section_service_verification",
            "stl_path": str(stl_path),
            "unit": "mm",

            # 已知测试轴：经过 (0.2, 0.2, 0)，沿 Z 方向。
            # 这里只验证截面调用链，不做回转体识别。
            "axis_origin": [0.2, 0.2, 0],
            "axis_direction": [0, 0, 1],
            "radial_reference": [1, 0, 0],

            "angles_degrees": [0, 47, 103],

            "processing": {
                "enable_micro_bump_cleaning": False
            },
        }

        original_parameters = copy.deepcopy(parameters)

        # wraps 表示仍然执行真实函数，只额外记录调用次数。
        with (
            patch(
                "rim_reconstruction.adapters.cpp_worker_adapter."
                "subprocess.run",
                wraps=subprocess.run,
            ) as process_spy,
            patch.object(
                adapter,
                "load_sections",
                wraps=adapter.load_sections,
            ) as load_spy,
        ):
            summary = process_sections(
                parameters,
                adapter=adapter,
            )

        check(
            summary["status"] == "success",
            "服务层返回成功状态",
        )

        # 1. 检查实际写给 C++ 的请求。
        request_path = Path(summary["request_path"])
        request = json.loads(
            request_path.read_text(encoding="utf-8-sig")
        )

        check(
            request["operation"] == "process_sections"
            and request["angles_degrees"] == [0, 47, 103],
            "一个请求包含三个指定角度",
        )

        # 2. 检查真实进程启动次数。
        check(
            process_spy.call_count == 1,
            "只启动一次 Worker",
        )

        # 3. 检查三个处理截面产物。
        artifacts = summary["section_artifacts"]

        check(
            len(artifacts) == 3
            and all(
                item["type"] == "processed_section"
                for item in artifacts
            ),
            "返回三个 processed_section 产物",
        )

        check(
            all(Path(item["path"]).is_file() for item in artifacts),
            "三个截面产物文件均存在",
        )

        # 4. 服务层内部必须已调用 load_sections()。
        # 若它读取或校验失败，前面的服务调用就会抛出异常。
        check(
            load_spy.call_count == 1,
            "服务层已通过 load_sections() 读取并校验截面",
        )

        sections = [
            json.loads(
                Path(item["path"]).read_text(
                    encoding="utf-8-sig"
                )
            )
            for item in artifacts
        ]

        check(
            [item["angle_degrees"] for item in sections]
            == [0, 47, 103],
            "截面文件角度和请求一致",
        )

        # 避免空轮廓导致后面的点数检查没有实际意义。
        check(
            all(section["contours"] for section in sections),
            "每个测试截面都有轮廓",
        )

        # 5. 检查所有轮廓点数。
        check(
            all(
                contour["point_count"] == len(contour["points"])
                for section in sections
                for contour in section["contours"]
            ),
            "每条轮廓的 point_count 与点列长度一致",
        )

        # 6. 检查服务没有修改调用方参数。
        check(
            parameters == original_parameters,
            "输入参数没有被原地修改",
        )

        # 7. 检查返回给上层的摘要不包含 points。
        check(
            not contains_points_key(summary),
            "返回摘要不包含 points",
        )

        check(
            len(summary["sections"]) == 3,
            "摘要包含三个角度的统计",
        )

        print("\n实际截面统计：")
        print(
            json.dumps(
                summary["sections"],
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            )
        )

    print("\n全部检查通过，测试临时文件已清理。")


if __name__ == "__main__":
    main()