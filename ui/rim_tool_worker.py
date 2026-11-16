"""轮辋逆向扩展：仅执行外部几何进程与 JSON 处理，不访问 FreeCAD 文档或控件。"""
import json
import threading

from PySide6 import QtCore

from rim_reconstruction.tool_handlers import rim_repair_sections, rim_process_sections, rim_analyze_candidates
# 轮辋逆向扩展：Python 拟合也在后台运行，避免阻塞 FreeCAD 界面。
from rim_reconstruction.tool_handlers import rim_fit_profile, rim_transverse_sections
from rim_reconstruction.tool_handlers import rim_evaluate_revolution
# 轮辋逆向扩展：C++ 模型解析与自动截面均在后台执行。
from rim_reconstruction.tool_handlers import rim_inspect_stl, rim_estimate_axis, rim_process_sections_auto


class RimToolWorker(QtCore.QThread):
    """结果在 finished 后由主线程读取，确保下一轮不与旧任务重叠。"""

    def __init__(self, tool_call, loop, parent=None, *, evaluation_snapshot=None):
        super().__init__(parent)
        self.tool_call = tool_call
        self.loop = loop
        self.cancel_event = threading.Event()
        self.result = ""
        self.evaluation_snapshot = evaluation_snapshot

    def requestInterruption(self):
        # 轮辋逆向扩展：Qt 中断标记与 Python 取消事件同步，可重复请求。
        self.cancel_event.set()
        super().requestInterruption()

    def run(self):
        try:
            name = self.tool_call.get("function", {}).get("name", "")
            from rim_reconstruction.window_tools import BACKGROUND, dispatch_window_tool
            if name in BACKGROUND:
                self.result = dispatch_window_tool(name, self.tool_call.get("function", {}).get("arguments", "{}"), cancel_event=self.cancel_event)
                return
            if name == "rim_evaluate_revolution":
                if self.evaluation_snapshot is None:
                    raise ValueError("评估缺少主线程几何快照")
                self.result = rim_evaluate_revolution(
                    self.tool_call.get("function", {}).get("arguments", "{}"),
                    snapshot=self.evaluation_snapshot, cancel_event=self.cancel_event)
                return
            handlers = {"rim_transverse_sections": rim_transverse_sections,"rim_repair_sections": rim_repair_sections, "rim_process_sections": rim_process_sections,
                        "rim_analyze_candidates": rim_analyze_candidates,
                        "rim_fit_profile": rim_fit_profile,
                        "rim_inspect_stl": rim_inspect_stl,
                        "rim_estimate_axis": rim_estimate_axis,
                        "rim_process_sections_auto": rim_process_sections_auto}
            self.result = handlers[name](
                self.tool_call.get("function", {}).get("arguments", "{}"),
                cancel_event=self.cancel_event,
            )
        except Exception as exc:
            self.result = json.dumps({"status": "error", "error": {
                "code": "RIM_BACKGROUND_FAILED", "message": str(exc)}}, ensure_ascii=False)
