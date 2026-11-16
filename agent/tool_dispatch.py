"""Tool dispatch — routes tool calls to handler functions.

Pure routing logic with no FreeCAD dependencies. Tool implementations
register themselves via register_tool().
"""
from __future__ import annotations

import json
import traceback

from core.logger import log_info, log_warning


_TOOL_MAP: dict[str, callable] = {}


def register_tool(name: str, handler: callable) -> None:
    """Register a tool handler function by name."""
    _TOOL_MAP[name] = handler


def available_tools() -> list[str]:
    """Return names of all registered tools."""
    return list(_TOOL_MAP.keys())


# 轮辋逆向扩展：原工具使用 ERROR/FAIL 文本，新截面工具使用 JSON 状态。
# 统一判断供日志与界面复用，防止结构化错误被误报为成功。
def tool_result_is_error(result: str) -> bool:
    """兼容原文本工具和轮辋工具的 JSON 状态。"""
    if result.startswith(("ERROR", "FAIL")):
        return True
    try:
        payload = json.loads(result)
    except (ValueError, TypeError):
        return False
    return isinstance(payload, dict) and payload.get("status") in ("error", "cancelled")


def dispatch_tool(name: str, args_json: str) -> str:
    """Dispatch a tool call by name. Returns result string."""
    handler = _TOOL_MAP.get(name)
    if handler is None:
        return f"ERROR: Unknown tool '{name}'. Available: {available_tools()}"
    try:
        log_info(f"Tool call: {name}({args_json[:200]})")
        result = handler(args_json)
        # 轮辋逆向扩展：同时识别原文本错误和新增 JSON 错误信封。
        if tool_result_is_error(result):
            log_warning(f"Tool '{name}' returned failure: {result[:500]}")
        else:
            log_info(f"Tool '{name}' succeeded: {result[:200]}")
        return result
    except Exception as e:
        tb = traceback.format_exc()
        log_warning(f"Tool '{name}' threw exception: {type(e).__name__}: {e}\n{tb}")
        return f"ERROR in tool '{name}': {type(e).__name__}: {e}"
