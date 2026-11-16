"""
AgentPanel — chat-style dock panel for CadAgent.

Thread safety model:
  - LLM API calls run in a background QThread (slow, network I/O)
  - FreeCAD tools run in the main thread; rim section processing runs in a background QThread
  - State machine alternates between: call_llm (background) -> execute_tools (main) -> repeat
"""
from __future__ import annotations

import json
import os
import time
import traceback

import FreeCAD
import FreeCADGui

from PySide6 import QtCore, QtWidgets

from agent.controller import AgentController
from agent.loop import AgentLoop, LoopAction, LoopActionKind, ToolExecution
from core.session import ChatSession
from core.doc_analyzer import analyze_document
from core.token_budget import token_summary
from core.session_store import SessionStore
from agent.tools import dispatch_tool
# 轮辋逆向扩展：复用统一状态判断，兼容截面工具的 JSON 错误结果。
from agent.tool_dispatch import tool_result_is_error
import core.config as _config
from core.llm_client import call_llm_streaming
from core.logger import log_info, log_warning, log_error

from ui.panel_ui import _PanelUIMixin
from ui.panel_stream import _PanelStreamMixin
from ui.panel_session import _PanelSessionMixin
from ui.panel_status import _PanelStatusMixin


class _LlmCallThread(QtCore.QThread):
    """Background thread: streams LLM API response, emits incremental chunks."""
    chunkReady = QtCore.Signal(str)
    reasoningReady = QtCore.Signal(str)
    streamDone = QtCore.Signal(dict)
    error = QtCore.Signal(str)

    def __init__(self, messages, tools, parent=None):
        super().__init__(parent)
        self.messages = messages
        self.tools = tools

    def run(self):
        try:
            content = ""
            tc_map = {}  # index -> {id, function: {name, arguments}}
            finish_reason = None
            usage = None

            for chunk in call_llm_streaming(self.messages, tools=self.tools):
                if self.isInterruptionRequested():
                    break

                choices = chunk.get("choices", [])
                if not choices:
                    # usage-only chunks carry empty choices (include_usage)
                    if chunk.get("usage"):
                        usage = chunk["usage"]
                    continue
                choice = choices[0]
                delta = choice.get("delta", {})
                fr = choice.get("finish_reason")
                if fr:
                    finish_reason = fr

                # Text streaming
                c = delta.get("content")
                if c:
                    content += c
                    self.chunkReady.emit(c)

                # Reasoning content — disabled (not shown in UI)
                # rc = delta.get("reasoning_content")
                # if rc:
                #     self.reasoningReady.emit(rc)

                # Tool calls accumulation
                for tc in (delta.get("tool_calls") or []):
                    idx = tc.get("index", 0)
                    if idx not in tc_map:
                        tc_map[idx] = {
                            "id": "",
                            "type": "function",
                            "function": {"name": "", "arguments": ""},
                        }
                    entry = tc_map[idx]
                    if tc.get("id"):
                        entry["id"] = tc["id"]
                    fn = tc.get("function", {})
                    if fn.get("name"):
                        entry["function"]["name"] += fn["name"]
                    if fn.get("arguments"):
                        entry["function"]["arguments"] += fn["arguments"]

            # Assemble final message (same format as non-streaming response)
            tool_calls = [tc_map[i] for i in sorted(tc_map)] if tc_map else []

            # Validate tool_calls arguments — truncated SSE streams leave
            # incomplete JSON that would crash in dispatch_tool().
            for tc in tool_calls:
                try:
                    json.loads(tc["function"]["arguments"])
                except (json.JSONDecodeError, KeyError):
                    self.error.emit(
                        "Stream interrupted: incomplete tool call arguments"
                    )
                    return

            msg: dict = {"content": content or ""}
            if tool_calls:
                msg["tool_calls"] = tool_calls
            if finish_reason is None:
                finish_reason = "tool_calls" if tool_calls else "stop"

            # Hybrid token estimation: remember the provider-reported usage
            # so the next trim/calibration uses real numbers (S2-B4).
            if usage:
                total = (usage.get("prompt_tokens") or 0) + (
                    usage.get("completion_tokens") or 0
                )
                if total > 0:
                    try:
                        from core.token_budget import record_provider_usage
                        record_provider_usage(total, self.messages)
                    except Exception:
                        pass

            self.streamDone.emit({
                "choices": [{"message": msg, "finish_reason": finish_reason}]
            })

        except Exception as e:
            self.error.emit(f"{type(e).__name__}: {e}")


class AgentPanel(QtWidgets.QDockWidget, _PanelUIMixin, _PanelStreamMixin, _PanelSessionMixin, _PanelStatusMixin):
    """Chat-style dock panel for CadAgent."""

    # 轮辋逆向扩展：界面可显式指定业务工作流，默认保持通用 CAD 建模。
    def __init__(self, parent=None, *, workflow="cad"):
        super().__init__("CadAgent", parent)
        if workflow not in ("cad", "rim_sections", "rim_reconstruction", "spoke_reconstruction"):
            raise ValueError(f"Unknown workflow: {workflow}")
        self._workflow = workflow
        self._session = ChatSession()
        self._current_session_id = self._session.session_id
        self._sync_all_state(new_session_id=self._session.session_id)
        self._controller = None
        self._loop: AgentLoop | None = None
        self._mode = "auto"
        # True between _on_send and finish/recover — gates the steering path
        self._turn_active = False
        # True while an IME composition (preedit) is active in the input box
        self._ime_preedit = False
        # All live LLM threads. The list keeps the ONLY Python references so
        # a still-running thread is never garbage-collected (Qt fatal:
        # "Destroyed while thread is still running") — S1-D1/D2.
        self._llm_threads: list = []
        # 轮辋逆向扩展：单独持有几何线程，结果只在主线程处理。
        self._rim_threads = []
        self._tool_queue = []
        self._tool_executions = []
        self._streaming_text = ""
        self._stream_replace_start = 0
        self._stream_timer = None
        self._reasoning_text = ""
        self._theme_colors = None
        self._store = SessionStore()
        self._setup_ui()

    # 轮辋逆向扩展：允许在空闲时切换下一轮任务类型，不干扰正在执行的任务。
    def set_workflow(self, workflow):
        if workflow not in ("cad", "rim_sections", "rim_reconstruction", "spoke_reconstruction"):
            raise ValueError(f"Unknown workflow: {workflow}")
        if self._turn_active or self._rim_threads:
            raise RuntimeError("请等待当前任务结束后再切换模式")
        self._workflow = workflow
        # 轮辋逆向扩展：程序调用与下拉框保持一致，阻断信号避免递归触发。
        if hasattr(self, "workflow_combo"):
            previous = self.workflow_combo.blockSignals(True)
            try:
                self.workflow_combo.setCurrentIndex(self.workflow_combo.findData(workflow))
            finally:
                self.workflow_combo.blockSignals(previous)
            self._update_workflow_placeholder()

    # 轮辋逆向扩展：选择只影响下一轮任务，不清空现有会话或输入内容。
    def _on_workflow_changed(self, index):
        workflow = self.workflow_combo.itemData(index)
        if workflow == self._workflow:
            return
        try:
            self.set_workflow(workflow)
        except (ValueError, RuntimeError) as exc:
            previous = self.workflow_combo.blockSignals(True)
            try:
                self.workflow_combo.setCurrentIndex(self.workflow_combo.findData(self._workflow))
            finally:
                self.workflow_combo.blockSignals(previous)
            self._append_system_msg(str(exc))
            return
        self._append_system_msg(f"已切换模式：{self.workflow_combo.currentText()}。")

    # 轮辋逆向扩展：提示本阶段所需输入，避免将截面提取理解为完整重建。
    def _update_workflow_placeholder(self):
        if self._workflow in ("rim_sections", "rim_reconstruction", "spoke_reconstruction"):
            self.text_input.setPlaceholderText("提供轮毂 STL 路径，描述轮辋重建任务；程序自动解析模型和回转轴…")
        else:
            self.text_input.setPlaceholderText("Describe the part you want to design...")

    # ---- Theme ----

    def _get_colors(self):
        if self._theme_colors is None:
            from ui.theme import get_theme_colors
            self._theme_colors = get_theme_colors()
        return self._theme_colors

    def _refresh_theme(self):
        from ui.theme import get_theme_colors
        self._theme_colors = get_theme_colors()
        self._apply_dynamic_styles()

    def _reset_turn_state(self):
        """Reset per-turn loop and streaming state.

        Single source of truth (S3-D18): this block was copy-pasted in four
        places, and the missing per-turn reset on send caused the ReAct
        display wipe (S3-D6) — a stale _stream_replace_start deleted live
        chat content when the next turn finalized.
        """
        self._controller = None
        self._loop = None
        self._turn_active = False
        self._streaming_text = ""
        self._stream_replace_start = 0
        self._reasoning_text = ""
        if self._stream_timer and self._stream_timer.isActive():
            self._stream_timer.stop()

    # ---- Session lifecycle: unified state synchronization ----

    def _sync_all_state(self, new_session_id=None):
        """Synchronize all three state layers for a session transition.

        Clears persistent vars, param store, and snapshots in one place.
        Every lifecycle method (new, switch, delete, close) should call
        this instead of doing partial cleanup.
        """
        from agent.tools import clear_persistent_vars, set_param_store
        from core.snapshot import cleanup_all_snapshots, set_snapshot_session_id
        clear_persistent_vars()
        set_param_store({})
        cleanup_all_snapshots()
        if new_session_id:
            set_snapshot_session_id(new_session_id)

    # ---- Event filter for multi-line input ----

    def eventFilter(self, obj, event):
        if obj is self.text_input:
            if event.type() == event.Type.InputMethod:
                # Track IME composition (S3-D11): Enter while a preedit is
                # active must commit the composition, not send the message.
                self._ime_preedit = bool(event.preeditString())
                return False
            if event.type() == event.Type.KeyPress:
                if event.key() in (QtCore.Qt.Key.Key_Return, QtCore.Qt.Key.Key_Enter):
                    if event.modifiers() & QtCore.Qt.KeyboardModifier.ShiftModifier:
                        return False  # default newline behavior
                    if self._ime_preedit:
                        return False  # composing — let the IME handle the key
                    self._on_send()
                    return True  # event consumed
        return super().eventFilter(obj, event)

    # ---- State machine: send -> call LLM (bg) -> execute tools (main) -> repeat ----

    def _set_running(self, running):
        self.btn_send.setEnabled(not running)
        # Input stays enabled while running: typing + Enter queues a
        # steering message instead of starting a second turn (S3-D7).
        self.btn_stop.setEnabled(running)
        self.btn_new_session.setEnabled(not running)
        self.session_combo.setEnabled(not running)
        # 轮辋逆向扩展：任务执行期间锁定工具和提示词的选择。
        self.workflow_combo.setEnabled(not running)
        self.btn_delete_session.setEnabled(not running and self.session_combo.currentIndex() > 0)
        if running:
            # Remember focus ownership so finishing a turn never steals
            # focus from elsewhere in FreeCAD (S3-D15)
            self._had_input_focus = self.text_input.hasFocus()
        else:
            if getattr(self, "_had_input_focus", False):
                self.text_input.setFocus()
            self._update_undo_button_state()

    def _on_send(self):
        text = self.text_input.toPlainText().strip()
        if not text:
            return

        # 轮辋逆向扩展：停止请求不等于进程已退出，回收完成前禁止启动另一轮。
        if self._turn_active and self._loop is not None and self._loop.stopped:
            self._append_system_msg("正在停止任务，请等待后台进程退出后再发送。")
            return
        if self._rim_threads:
            self._append_system_msg("后台截面任务尚未回收，请稍后重试。")
            return

        # Steering (S3-D7): input during a running turn is queued and
        # injected at the next LLM call boundary — not a second turn.
        if self._turn_active and self._loop is not None and not self._loop.stopped:
            if self._loop.queue_steering(text):
                self._append_user_msg(text)
                self.text_input.clear()
                self._append_system_msg("Queued — sent to the model at the next step.")
                return

        # Per-turn reset FIRST — stale streaming cursor state from a previous
        # turn (ReAct mode especially) used to delete live content (S3-D6).
        self._reset_turn_state()

        self._append_user_msg(text)
        self.text_input.clear()
        self._set_running(True)

        # Everything past this point must never leave the panel stuck with
        # all controls disabled (S1-D5): no signal would ever fire to
        # restore them.
        try:
            # Gather document context (main thread — safe)
            context = ""
            doc = FreeCAD.ActiveDocument
            if doc:
                try:
                    # If multiple documents are open, include all of them
                    all_docs = FreeCAD.listDocuments()
                    if len(all_docs) > 1:
                        from core.doc_analyzer import analyze_all_documents
                        context = analyze_all_documents()
                    else:
                        context = analyze_document(doc)
                except Exception as e:
                    log_warning(f"Document analysis failed: {e}")
                    context = ""

            # Initialize controller and loop
            self._controller = AgentController(self._session)

            # 轮辋逆向扩展：将面板选择传入 Loop，协议模式仍由 Loop 独立管理。
            self._loop = AgentLoop(self._controller, context, self._session.last_mode,
                                   workflow=self._workflow)
            self._mode = self._loop.mode

            self._store.save_if_not_empty(self._session)

            action = self._loop.start(text)
            self._turn_active = True
            self._execute_action(action)
        except Exception as e:
            self._recover_after_internal_error(e)

    def _recover_after_internal_error(self, exc: Exception):
        """Restore the panel after an unexpected error inside the agent turn.

        Deliberately does NOT touch the streaming bubble via stale cursor
        state (that hazard is fixed separately in S3); it only neutralizes
        the streaming state so later finalize/remove calls are inert.
        """
        log_error(
            f"Internal error in agent turn: {type(exc).__name__}: {exc}\n"
            f"{traceback.format_exc()}"
        )
        # 轮辋逆向扩展：异常恢复也必须取消尚在运行的几何进程，不能仅恢复按钮。
        if self._loop:
            self._loop.request_stop()
        for worker in self._rim_threads:
            worker.requestInterruption()
        self._turn_active = False
        self._streaming_text = ""
        if self._stream_timer and self._stream_timer.isActive():
            self._stream_timer.stop()
        if self._controller is not None and self._loop is not None:
            try:
                self._finish_from_action(LoopAction(
                    kind=LoopActionKind.FINISH, success=False,
                    summary=f"Internal error: {type(exc).__name__}: {exc}",
                    iterations=self._loop.iteration,
                ))
                return
            except Exception:
                log_error("Recovery via _finish_from_action failed; "
                          "falling back to direct UI restore")
        try:
            self._append_system_msg(f"Internal error: {exc}")
        except Exception:
            pass
        self._set_running(False)
        try:
            self._status_reset()
        except Exception:
            pass

    def _update_token_label(self, used: int, budget: int, trimmed: bool = False):
        """Update the token budget status label with color coding."""
        c = self._get_colors()
        ratio = used / budget if budget else 0
        if ratio > 0.9:
            color = c.token_danger
        elif ratio > 0.7:
            color = c.token_warn
        else:
            color = c.token_ok
        tag = " [trimmed]" if trimmed else ""
        self.token_label.setText(f"Tokens: {used} / {budget}{tag}")
        self.token_label.setStyleSheet(f"color:{color}; font-size:10px;")

    def _execute_action(self, action: LoopAction):
        if action.kind == LoopActionKind.CALL_LLM:
            self._start_llm_call(action)
        elif action.kind == LoopActionKind.EXECUTE_TOOLS:
            if action.system_message:
                self._append_system_msg(action.system_message)
            self._mode = self._loop.mode
            self._run_tool_calls(action)
        elif action.kind == LoopActionKind.FINISH:
            self._finish_from_action(action)

    def _start_llm_call(self, action: LoopAction):
        """Kick off a background streaming LLM API call."""
        self._status_set(action.status_state)
        self._mode = self._loop.mode

        # Start streaming bubble (skip for react — needs full text before display)
        if self._mode != "react":
            self._start_streaming_bubble()

        self._update_token_label(action.token_used, action.token_budget, action.token_trimmed)
        thread = _LlmCallThread(action.messages, action.tools, parent=self)
        thread.chunkReady.connect(self._on_stream_chunk)
        thread.reasoningReady.connect(self._on_reasoning_chunk)
        thread.streamDone.connect(self._on_stream_done)
        thread.error.connect(self._on_llm_error)
        thread.finished.connect(lambda t=thread: self._forget_thread(t))
        self._llm_threads.append(thread)
        thread.start()

    # ---- LLM thread lifecycle (S1-D1/D2) ----

    def _live_threads(self) -> list:
        """Threads that are still running."""
        return [t for t in self._llm_threads + self._rim_threads if t.isRunning()]

    def _forget_thread(self, thread) -> None:
        """Drop a finished thread's reference (connected to finished)."""
        try:
            self._llm_threads.remove(thread)
        except ValueError:
            pass

    def _shutdown_threads(self, wait_ms: int) -> None:
        """Interrupt all live threads and wait (bounded) for them to end.

        A thread blocked inside urllib (up to LLM_TIMEOUT) can outlive the
        wait; it stays referenced in _llm_threads so it is never destroyed
        while running. True interruptible I/O is tracked as S3-D8.
        """
        live = self._live_threads()
        for t in live:
            t.requestInterruption()
        for t in live:
            if not t.wait(wait_ms):
                log_warning(
                    "Background thread did not stop within "
                    f"{wait_ms}ms; it will finish in the background"
                )
        self._llm_threads = [t for t in self._llm_threads if t.isRunning()]

    def _on_reasoning_chunk(self, text):
        """Accumulate reasoning content from models that support it."""
        self._reasoning_text += text

    def _on_stream_done(self, data):
        """Streaming complete — finalize display, then route response."""
        if self._controller is None:
            self._remove_streaming_bubble()
            return
        try:
            choices = data.get("choices", [])
            if not choices:
                self._remove_streaming_bubble()
                self._finish_from_action(LoopAction(
                    kind=LoopActionKind.FINISH, success=False,
                    summary="API returned empty response.",
                ))
                return
            content = choices[0].get("message", {}).get("content", "")

            # Save position so reasoning can be inserted before the bubble
            bubble_start = self._stream_replace_start

            # ReAct rounds stream raw <tool> XML as plain text — strip the
            # blocks so the bubble shows only prose (S3-D15)
            if self._streaming_text and "<tool" in self._streaming_text:
                from agent.react_parser import strip_tool_tags
                self._streaming_text = strip_tool_tags(self._streaming_text)

            # 轮辋逆向扩展修复：先取得最终动作，再渲染一次；失败摘要可能附带阶段原因。
            # 不能先固定流式全文，再把带失败原因的同一全文追加为第二个气泡。
            has_streaming_text = bool(self._streaming_text and self._streaming_text.strip())
            if not has_streaming_text:
                self._remove_streaming_bubble()
                bubble_start = None  # no bubble to insert before

            # Reasoning block — disabled
            # self._render_reasoning_block(insert_before_pos=bubble_start)
            self._reasoning_text = ""

            action = self._loop.handle_stream_done(data, has_streaming_text)
            if has_streaming_text:
                # 最终摘要（包括失败/停止）替换当前气泡，from_stream 仅表示已显示，
                # 不表示任务成功；success 仍由 AgentLoop 的几何阶段校验决定。
                if action.kind == LoopActionKind.FINISH:
                    self._streaming_text = action.summary
                    action.from_stream = True
                self._finalize_streaming_bubble()
            self._execute_action(action)
        except Exception as e:
            log_warning(f"Error in stream handler: {e}")
            self._remove_streaming_bubble()
            self._finish_from_action(LoopAction(
                kind=LoopActionKind.FINISH, success=False,
                summary=f"Internal error: {e}",
            ))

    def _on_llm_error(self, msg):
        log_warning(f"LLM API error: {msg}")
        self._remove_streaming_bubble()
        if self._controller is None:
            return
        action = self._loop.handle_error(msg)
        self._execute_action(action)

    # 轮辋逆向扩展：串行工具队列；仅截面工具在后台执行，CAD 操作仍在主线程。
    def _run_tool_calls(self, action: LoopAction):
        self._tool_queue = list(action.tool_calls)
        self._tool_executions = []
        self._run_next_tool()

    def _record_tool_result(self, tc, result):
        fn = tc.get("function", {})
        name = fn.get("name", "")
        arguments = fn.get("arguments", "{}")
        description = ""
        if name == "execute_code":
            try:
                description = json.loads(arguments).get("description", "")
            except (ValueError, TypeError):
                pass
        is_error = tool_result_is_error(result)
        self._append_tool_msg(self._loop.iteration, name, description, result, is_error)
        self._tool_executions.append(ToolExecution(
            tool_name=name, tool_args=arguments, tool_id=tc.get("id", ""),
            description=description, result=result, is_error=is_error))
        self._update_undo_button_state()

    def _run_next_tool(self):
        try:
            while self._tool_queue:
                tc = self._tool_queue.pop(0)
                if self._loop.stopped:
                    # 轮辋逆向扩展：补齐未执行调用的结果，避免保存不完整工具消息历史。
                    self._record_tool_result(tc, json.dumps({"status": "cancelled", "error": {
                        "code": "TASK_CANCELLED", "message": "任务已停止，工具未执行"}}, ensure_ascii=False))
                    continue
                fn = tc.get("function", {})
                name = fn.get("name", "")
                if name == "rim_evaluate_revolution":
                    # Enforce opt-in even if the model hallucinates an unadvertised tool.
                    if not getattr(self._loop, "_evaluation_requested", False):
                        self._record_tool_result(tc, json.dumps({"status": "error", "error": {
                            "code": "EVALUATION_NOT_REQUESTED", "message": "只有用户本轮明确要求时才允许评估"}}, ensure_ascii=False))
                        continue
                    try:
                        from rim_reconstruction.services.evaluation_service import prepare_evaluation
                        from rim_reconstruction.tool_handlers import _reject_constant, _unique_object
                        snapshot = prepare_evaluation(json.loads(fn.get("arguments", "{}"),
                            parse_constant=_reject_constant, object_pairs_hook=_unique_object))
                    except Exception as exc:
                        self._record_tool_result(tc, json.dumps({"status": "error", "error": {
                            "code": "EVALUATION_INPUT_INVALID", "message": str(exc)}}, ensure_ascii=False))
                        continue
                    from ui.rim_tool_worker import RimToolWorker
                    self._status_set("executing_tool", name)
                    worker = RimToolWorker(tc, self._loop, parent=self, evaluation_snapshot=snapshot)
                    worker.finished.connect(self._on_rim_tool_finished)
                    self._rim_threads.append(worker)
                    worker.start()
                    return
                # 轮辋逆向扩展：执行前再检查阶段和产物引用，防止模型越级或使用旧结果。
                pipeline = getattr(self._loop, "_rim_pipeline", None)
                if pipeline is not None:
                    reason = pipeline.validate_call(name, fn.get("arguments", "{}"))
                    if reason:
                        self._record_tool_result(tc, json.dumps({"status": "error", "error": {
                            "code": "RIM_STAGE_INVALID", "message": reason}}, ensure_ascii=False))
                        continue
                self._status_set("executing_tool", name)
                # 轮辋逆向扩展：模型选择后的 Python 拟合沿用后台线程和取消生命周期。
                from rim_reconstruction.window_tools import BACKGROUND as window_background
                if name in window_background or name in ("rim_repair_sections", "rim_process_sections", "rim_analyze_candidates", "rim_fit_profile",
                            "rim_inspect_stl", "rim_estimate_axis", "rim_process_sections_auto", "rim_transverse_sections"):
                    from ui.rim_tool_worker import RimToolWorker
                    worker = RimToolWorker(tc, self._loop, parent=self)
                    worker.finished.connect(self._on_rim_tool_finished)
                    self._rim_threads.append(worker)
                    worker.start()
                    return
                # 轮辋逆向扩展：rim_build_revolution 不进入后台队列；原生草图和实体在主线程创建。
                # FreeCAD 文档和 GUI API 必须继续在主线程执行。
                self._record_tool_result(tc, dispatch_tool(name, fn.get("arguments", "{}")))
            try:
                from agent.tools import get_param_store, get_persistent_vars
                self._controller.session.parameters = get_param_store()
                self._controller.session.persistent_vars = get_persistent_vars()
            except Exception:
                pass
            self._execute_action(self._loop.handle_tool_results(self._tool_executions))
        except Exception as exc:
            self._recover_after_internal_error(exc)

    # 轮辋逆向扩展：跨线程信号进入 QObject 的主线程槽，再更新聊天和推进 Loop。
    @QtCore.Slot()
    def _on_rim_tool_finished(self):
        worker = self.sender()
        self._complete_rim_tool(worker)

    # 轮辋逆向扩展：finished 信号和关闭时手动回收共用，确保每个结果只处理一次。
    def _complete_rim_tool(self, worker):
        if getattr(worker, "_result_consumed", False):
            return
        worker._result_consumed = True
        if worker in self._rim_threads:
            self._rim_threads.remove(worker)
        try:
            if worker.loop is not self._loop or not self._turn_active:
                return  # 已关闭/切换/恢复后的旧结果不得污染新任务。
            result = worker.result
            if worker.cancel_event.is_set() or self._loop.stopped:
                # 保留进程调用目录和退出码；取消后不暴露成功产物。
                try:
                    detail = json.loads(result)
                except (ValueError, TypeError):
                    detail = {}
                if not isinstance(detail, dict):
                    detail = {}
                cancelled = {"status": "cancelled", "error": {
                    "code": "WORKER_CANCELLED", "message": "用户取消了截面任务"}}
                for key in ("call_directory", "returncode"):
                    if key in detail:
                        cancelled[key] = detail[key]
                result = json.dumps(cancelled, ensure_ascii=False)
            self._record_tool_result(worker.tool_call, result)
            self._run_next_tool()
        except Exception as exc:
            self._recover_after_internal_error(exc)
        finally:
            worker.deleteLater()

    def _finish_from_action(self, action: LoopAction):
        """Agent loop complete — show results."""
        c = self._get_colors()
        log_info(f"Agent finished: success={action.success}, iterations={action.iterations}, summary={action.summary[:100]}")
        if not action.from_stream:
            self._append_agent_msg(action.summary)

        if action.success:
            self._controller.result.success = True
            self._controller.result.summary = action.summary
            self._controller.session.update_summary(action.summary)

            # Snapshot document state + fit view
            try:
                doc = FreeCAD.ActiveDocument
                if doc:
                    self._controller.session.update_document_state(analyze_document(doc))
            except Exception as e:
                log_warning(f"Failed to save document state: {e}")
            try:
                gui_doc = FreeCADGui.activeDocument()
                if gui_doc:
                    view = gui_doc.activeView()
                    if view:
                        view.viewIsometric()
                        view.fitAll()
            except Exception as e:
                log_warning(f"Failed to adjust 3D view: {e}")

        self._store.save_if_not_empty(self._session)

        self._status_timer.stop()
        total_elapsed = time.time() - self._loop.start_time if self._loop else 0
        elapsed_str = self._format_elapsed(total_elapsed)
        turns = self._controller.session.user_turn_count()
        iters = action.iterations or self._loop.iteration if self._loop else 0
        if action.success:
            self._status_finish(True, f"Done  {elapsed_str}  {iters} iters  Turn {turns}")
        else:
            self._status_finish(False, f"Failed  {elapsed_str}  {iters} iters  Turn {turns}")

        self._set_running(False)
        self._turn_active = False
        self._refresh_session_list()

    def _on_chat_anchor(self, url):
        """Open http/https chat links in the system browser (S3-D15).

        Other schemes (file:, javascript:...) are ignored — links were
        previously rendered blue but did nothing on click.
        """
        from PySide6.QtGui import QDesktopServices
        url_str = url.toString() if hasattr(url, "toString") else str(url)
        if url_str.startswith(("http://", "https://")):
            QDesktopServices.openUrl(url)

    # ---- Event handlers ----

    def _on_stop(self):
        # 轮辋逆向扩展：先禁止 Loop 推进，再通知所有后台任务；等待 finished 后恢复界面。
        if self._loop:
            self._loop.request_stop()
        for t in self._live_threads():
            t.requestInterruption()
        self._status_set("stopping")

    def _on_new_session(self):
        # 轮辋逆向扩展：未回收的几何任务仍绑定原会话，不允许切换丢失其取消结果。
        if self._rim_threads:
            self._on_stop()
            self._append_system_msg("正在停止截面任务，完成后可新建会话。")
            return
        # Confirm before discarding the view of a session with real turns
        # (it is saved and stays in the session list) — S3-D14
        if self._session.user_turn_count() > 0:
            from PySide6 import QtWidgets
            reply = QtWidgets.QMessageBox.question(
                self, "New Session",
                "Start a new session? The current conversation is saved "
                "and remains in the session list.",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No,
            )
            if reply != QtWidgets.QMessageBox.Yes:
                return
        # Stop the running threads and disconnect signals so stale
        # streamDone / chunkReady don't hit the cleared state below.
        if self._loop:
            self._loop.request_stop()
        # 轮辋逆向扩展：这些流式信号只属于 LLM 线程。
        for t in [thread for thread in self._llm_threads if thread.isRunning()]:
            for sig, slot in (
                (t.chunkReady, self._on_stream_chunk),
                (t.reasoningReady, self._on_reasoning_chunk),
                (t.streamDone, self._on_stream_done),
                (t.error, self._on_llm_error),
            ):
                try:
                    sig.disconnect(slot)
                except (TypeError, RuntimeError):
                    pass
        self._shutdown_threads(3000)
        self._store.save_if_not_empty(self._session)
        self._session = ChatSession()
        self._current_session_id = self._session.session_id
        self._sync_all_state(new_session_id=self._session.session_id)
        self._reset_turn_state()
        self.chat_display.clear()
        self._append_system_msg("New session started. Describe a part to begin.")
        self._status_reset()
        self._set_running(False)
        self._refresh_session_list()

    def _on_attach_image(self):
        """Open file dialog to select an image, copy to uploads, insert reference."""
        from PySide6 import QtWidgets
        import shutil
        from datetime import datetime

        file_path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Select Image",
            "",
            "Images (*.png *.jpg *.jpeg *.bmp);;All Files (*)"
        )
        if not file_path:
            return

        from core.paths import subdir
        uploads_dir = subdir("uploads")

        ext = os.path.splitext(file_path)[1]
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest_name = f"{timestamp}{ext}"
        dest_path = os.path.join(uploads_dir, dest_name)
        shutil.copy2(file_path, dest_path)

        current_text = self.text_input.toPlainText()
        marker = f" [image: {dest_path}]"
        self.text_input.setPlainText(current_text + marker)

        self._append_system_msg(f"Attached image: {dest_name}")

    def _on_undo(self):
        """User clicked Undo — restore document from most recent snapshot."""
        from core.snapshot import restore_latest_snapshot, snapshot_count, has_snapshot

        if not has_snapshot():
            self._append_system_msg("Nothing to undo — no snapshots available.")
            return

        if self._live_threads():
            self._append_system_msg("Cannot undo while agent is running.")
            return

        result = restore_latest_snapshot()
        is_error = result.startswith("ERROR")

        self._append_tool_msg(
            self._loop.iteration if self._loop else 0, "undo_last (user)", "",
            result, is_error,
        )

        if not is_error:
            self._append_system_msg(f"Undo successful. {snapshot_count()} snapshot(s) remaining.")
        else:
            self._append_system_msg(f"Undo failed: {result}")

        self._update_undo_button_state()

    def _update_undo_button_state(self):
        """Enable/disable undo button based on snapshot availability."""
        from core.snapshot import has_snapshot
        self.btn_undo.setEnabled(has_snapshot())

    def _on_settings(self):
        from ui.settings_dialog import SettingsDialog
        SettingsDialog(parent=self).exec()

    def closeEvent(self, event):
        # 轮辋逆向扩展：关闭时停止任务推进，丢弃迟到结果，协作取消几何进程。
        if self._loop:
            self._loop.request_stop()
        # Interrupt and (bounded) wait for LLM threads first — destroying a
        # running QThread is a hard Qt abort (S1-D1).
        self._shutdown_threads(5000)
        # 轮辋逆向扩展：线程仍运行时不销毁其 Qt 父对象；稍后可再次关闭。
        if self._live_threads():
            event.ignore()
            self._append_system_msg("后台任务正在停止，请等待退出后再关闭。")
            return
        # wait 会阻塞 finished 信号派送，先手动补齐取消结果并保存完整工具消息。
        for worker in list(self._rim_threads):
            self._complete_rim_tool(worker)
        self._turn_active = False
        self._store.save_current_on_close(self._session)
        from core.snapshot import cleanup_all_snapshots
        cleanup_all_snapshots()
        super().closeEvent(event)
