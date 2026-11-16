"""可取消进程等待的边界检查；不依赖 Qt。"""
import sys
import tempfile
import threading
import subprocess
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from rim_reconstruction.adapters.cpp_worker_adapter import (
    CppWorkerAdapter, WorkerCancelledError, WorkerTimeoutError,
)
from rim_reconstruction.tool_handlers import rim_process_sections
from tests.test_rim_tool_handlers import check_real_worker_through_handler


class RimCancellationTests(unittest.TestCase):
    def test_cancel_at_process_exit_wins_over_success(self):
        event = threading.Event()
        process = MagicMock()
        def wait(**kwargs):
            event.set()
            return 0
        process.wait.side_effect = wait
        context = MagicMock()
        context.__enter__.return_value = process
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            adapter = CppWorkerAdapter(sys.executable, Path(directory) / "calls")
            request = {"task_id": "cancel_exit", "operation": "inspect_stl",
                       "stl_path": "unused.stl", "unit": "mm"}
            with patch("subprocess.Popen", return_value=context), self.assertRaises(WorkerCancelledError):
                adapter.run(request, cancel_event=event)

    def test_actual_running_process_is_stopped_and_reaped(self):
        """模拟长时间 C++ 计算，用实际子进程验证事件传递及操作系统回收。"""
        event = threading.Event()
        original = subprocess.Popen
        processes = []
        def launch(command, **options):
            process = original([sys.executable, "-c", "import time; time.sleep(30)"], **options)
            processes.append(process)
            return process
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            adapter = CppWorkerAdapter(sys.executable, Path(directory) / "calls")
            request = {"task_id": "real_cancel", "operation": "inspect_stl",
                       "stl_path": "unused.stl", "unit": "mm"}
            timer = threading.Timer(0.3, event.set)
            started = time.monotonic()
            timer.start()
            try:
                with patch("subprocess.Popen", side_effect=launch), self.assertRaises(WorkerCancelledError):
                    adapter.run(request, cancel_event=event)
            finally:
                timer.cancel()
                timer.join()
                for process in processes:
                    if process.poll() is None:
                        process.kill()
                        process.wait()
            self.assertEqual(len(processes), 1)
            self.assertIsNotNone(processes[0].returncode)
            self.assertLess(time.monotonic() - started, 5)

    def test_cancel_during_request_write_does_not_launch(self):
        event = threading.Event()
        original_write = Path.write_text
        def write(path, *args, **kwargs):
            result = original_write(path, *args, **kwargs)
            event.set()
            return result
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            adapter = CppWorkerAdapter(sys.executable, Path(directory) / "calls")
            request = {"task_id": "cancel_write", "operation": "inspect_stl",
                       "stl_path": "unused.stl", "unit": "mm"}
            with patch.object(Path, "write_text", write), patch("subprocess.Popen") as launch:
                with self.assertRaises(WorkerCancelledError):
                    adapter.run(request, cancel_event=event)
            launch.assert_not_called()

    def test_cancel_before_launch_is_json(self):
        event = threading.Event()
        event.set()
        adapter = CppWorkerAdapter(sys.executable, Path(__file__).parent / "unused_calls")
        with patch("subprocess.Popen") as launch:
            with self.assertRaises(WorkerCancelledError):
                adapter.run({}, cancel_event=event)
        launch.assert_not_called()

    def test_cancel_kills_and_reaps_process(self):
        event = threading.Event()
        self.check_abort(event, WorkerCancelledError, [0, 0], set_cancel=True)

    def test_timeout_kills_and_reaps_process(self):
        self.check_abort(threading.Event(), WorkerTimeoutError, [0, 121])

    def check_abort(self, event, error_type, clock_values, set_cancel=False):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            adapter = CppWorkerAdapter(sys.executable, Path(directory) / "calls")
            request = {"task_id": "cancel_test", "operation": "inspect_stl",
                       "stl_path": "sample.stl", "unit": "mm"}
            process = MagicMock()
            process.poll.return_value = None
            def enter():
                if set_cancel:
                    event.set()
                return process
            context = MagicMock()
            context.__enter__.side_effect = enter
            with patch("subprocess.Popen", return_value=context), patch(
                "rim_reconstruction.adapters.cpp_worker_adapter.time.monotonic", side_effect=clock_values
            ), self.assertRaises(error_type) as caught:
                adapter.run(request, cancel_event=event)
            process.kill.assert_called_once()
            process.wait.assert_called_once_with()
            self.assertTrue((caught.exception.call_directory / "request.json").is_file())

    def test_handler_cancel_status(self):
        with patch("rim_reconstruction.tool_handlers.process_sections",
                   side_effect=WorkerCancelledError("WORKER_CANCELLED", "已取消")):
            import json
            result = json.loads(rim_process_sections("{}", cancel_event=threading.Event()))
        self.assertEqual(result["status"], "cancelled")

    def test_real_worker_cancellable_path(self):
        original = CppWorkerAdapter.run
        def cancellable(adapter, request, **options):
            return original(adapter, request, cancel_event=threading.Event(), **options)
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory, patch.object(
            CppWorkerAdapter, "run", cancellable
        ):
            check_real_worker_through_handler(Path(directory))
