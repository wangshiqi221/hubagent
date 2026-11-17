"""Agent loop state machine — pure-logic routing extracted from UI.

Owns: iteration count, mode, stop flag, timing.
References: AgentController (session + result).
Does NOT own: threads, UI, FreeCAD.

整体工作流程
Usage:
    loop = AgentLoop(controller, context, last_mode)
    action = loop.start(user_text)
    # caller interprets action, starts LLM thread
    action = loop.handle_stream_done(llm_data, has_streaming_text)
    # caller interprets action...
    action = loop.handle_tool_results(executions)
    # caller interprets action...
"""
from __future__ import annotations

import time
import json  # 轮辋逆向扩展：解析截面工具的结构化状态。
from dataclasses import dataclass, field
from enum import Enum

from agent.react_parser import parse_react_tool_calls
# 轮辋逆向扩展：分别加载截面工具声明和专用提示词。
from agent.tool_defs import TOOL_DEFINITIONS, RIM_TOOL_DEFINITIONS, RIM_RECONSTRUCTION_TOOL_NAMES
from agent.prompts import AGENT_SYSTEM_PROMPT, REACT_SYSTEM_PROMPT, RIM_SECTIONS_SYSTEM_PROMPT, RIM_RECONSTRUCTION_SYSTEM_PROMPT
# 轮辋逆向扩展：完整重建按实际工具结果执行阶段门禁。
from agent.rim_pipeline import RimPipeline
from agent.tool_defs import RIM_EVALUATION_TOOL_DEFINITION
from agent.evaluation_intent import requests_evaluation, EVALUATION_PROMPT
from agent.references import (
    REF_FIRST_ITERATION, REF_QUALITY_FAILURE, REF_REPAIR_LOOP,
    REF_ITERATION_URGENCY, REF_QUALITY_PASSED_WARN, QUALITY_FIX_MAP,
)
from core.token_budget import trim_messages, token_summary
import core.config as _config
from core.logger import log_info

#LoopActionKind 规定 Agent 下一步做什么
class LoopActionKind(Enum):
    CALL_LLM = "call_llm"
    EXECUTE_TOOLS = "execute_tools"
    FINISH = "finish"

# @dataclass 是 Python 提供的一个装饰器,快速定义主要用于保存数据的类
@dataclass
# LoopAction 是一个数据类,用于表示代理循环中的操作。它包含了不同类型的操作所需的属性,如调用 LLM、执行工具和完成操作等。
class LoopAction:
    kind: LoopActionKind

    # CALL_LLM
    messages: list[dict] | None = None
    tools: list[dict] | None = None
    iteration: int = 0
    token_used: int = 0
    token_budget: int = 0
    token_trimmed: bool = False

    # EXECUTE_TOOLS
    #创建一个默认的空列表,每次创建时都是独立空列表
    tool_calls: list[dict] = field(default_factory=list)

    # FINISH
    success: bool = False
    summary: str = ""
    from_stream: bool = False
    iterations: int = 0

    # Display hints
    status_state: str = ""
    system_message: str = ""


@dataclass
class ToolExecution:
    tool_name: str
    tool_args: str
    tool_id: str
    description: str
    result: str
    is_error: bool


class AgentLoop:
    """Pure-logic agent loop state machine.

    Returns LoopAction instructions for the caller (UI layer) to interpret.
    No Qt or FreeCAD dependencies.
    """

    # 轮辋逆向扩展：workflow 表示任务类型，与模型调用协议 mode 独立。
    def __init__(self, controller, context: str, last_mode: str = "auto", *, workflow: str = "cad"):
        if workflow not in ("cad", "rim_sections", "rim_reconstruction", "spoke_reconstruction"):
            raise ValueError(f"Unknown workflow: {workflow}")
        self._workflow = workflow
        # 轮辋逆向扩展：兼容原截面模式，新增完整重建模式。
        self._rim_pipeline = RimPipeline(require_spokes=workflow == "spoke_reconstruction") if workflow in ("rim_reconstruction", "spoke_reconstruction") else None
        # 轮辋逆向扩展：只对无工具调用的提前结束做一次推进提醒，避免无限循环。
        self._rim_continuation_prompts = 0
        self._rim_build_completed = None
        self._rim_sections_succeeded = False
        self._rim_analysis_completed = None
        # 轮辋逆向扩展：一旦尝试拟合，阶段成功必须由拟合工具实际结果决定。
        self._rim_fit_completed = None
        self._controller = controller
        self._context = context
        self._mode: str = last_mode or "auto"
        # 轮辋逆向扩展：截面流程暂只支持原生 Tool Calling，不沿用历史 ReAct 协议。
        if workflow in ("rim_sections", "rim_reconstruction", "spoke_reconstruction"):
            self._mode = "tool_calling"
        self._iteration: int = 0
        self._stopped: bool = False
        self._start_time: float = time.time()
        self._recent_errors: list[str] = []
        self._execute_code_called: bool = False
        self._last_quality_passed: bool | None = None
        self._last_quality_summary: str = ""
        self._quality_gate_blocks: int = 0  # escape hatch counter
        # Steering queue (Pi-style, decision point 7): user input received
        # while a turn is running, injected as user messages at the next
        # LLM call boundary.
        self._steering: list[str] = []
        self._evaluation_requested = False
        self._evaluation_completed = False
        self._evaluation_attempted = False

    @property
    # 轮辋逆向扩展：供界面和测试读取当前任务类型。
    def workflow(self) -> str:
        return self._workflow

    @property
    def iteration(self) -> int:
        return self._iteration

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def stopped(self) -> bool:
        return self._stopped

    @property
    def start_time(self) -> float:
        return self._start_time

    def request_stop(self) -> None:
        self._stopped = True

    #把用户指令加入队列,在下一次 LLM 调用边界注入
    def queue_steering(self, text: str) -> bool:
        """Queue user input for injection at the next LLM call boundary.

        Returns False (input not accepted) when the loop has been stopped —
        the caller should treat it as a normal follow-up message instead.
        """
        if not text or self._stopped:
            return False
        self._steering.append(text)
        return True

    #将队列中的第一条指令加入到当前对话消息
    #self._steering.pop(0)取出列表第一个元素并从列表中删除
    def _drain_steering(self) -> None:
        """Inject queued steering messages as user messages (in order)."""
        while self._steering:
            self._controller.session.add_user_message(self._steering.pop(0))

    #准备用户指令和系统提示词后调用prepare_llm_call
    def start(self, user_text: str) -> LoopAction:
        self._evaluation_requested = requests_evaluation(user_text)
        if self._evaluation_requested:
            self._mode = "tool_calling"
            # A follow-up evaluation reads the existing document; no reconstruction gate.
            self._rim_pipeline = None
        ctx = self._build_context()
        # 轮辋逆向扩展：按任务选择提示词，避免截面阶段被引导生成 CAD 代码。
        prompt = RIM_SECTIONS_SYSTEM_PROMPT if self._workflow in ("rim_sections", "rim_reconstruction", "spoke_reconstruction") else AGENT_SYSTEM_PROMPT
        # 轮辋逆向扩展：完整重建模式必须一直推进到原生回转实体。
        if self._workflow in ("rim_reconstruction", "spoke_reconstruction"):
            prompt = RIM_RECONSTRUCTION_SYSTEM_PROMPT
        if self._evaluation_requested:
            prompt = EVALUATION_PROMPT + "\n当前文档上下文：\n{context}"
        system_content = prompt.replace("{context}", ctx)
        self._controller.session.set_system_prompt(system_content)
        self._controller.session.add_user_message(user_text)
        # 轮辋逆向扩展：日志区分业务工作流和实际调用协议。
        log_info(f"Agent loop started: workflow={self._workflow}, mode={self._mode}, context={len(self._context)} chars")
        return self.prepare_llm_call()

    #收集当前 Agent 需要提供给大模型的上下文信息
    def _build_context(self) -> str:
        """Build full context string including parameters, document state,
        and phase-aware reference snippets injected based on agent state.

        Inspired by text-to-cad's progressive documentation pattern:
        instead of sending the full reference every turn, inject only
        what's relevant for the current phase. This saves tokens (24K budget)
        and focuses the LLM's attention.
        """
        parts: list[str] = []

        # 轮辋逆向扩展：不注入通用 CQ 建模检查清单、参数和布尔修复建议。
        if self._workflow in ("rim_sections", "rim_reconstruction", "spoke_reconstruction"):
            return self._context

        #获取设计参数
        # --- Always: parameters ---
        try:
            from agent.tools import get_param_store
            params = get_param_store()
        except ImportError:
            params = {}
        if params:
            lines = ["CURRENT DESIGN PARAMETERS:"]
            for name, value in sorted(params.items()):
                lines.append(f"  {name} = {value}")
            parts.append("\n" + "\n".join(lines))

        # --- Always: document context ---
        if self._context:
            parts.append(f"\nCURRENT DOCUMENT CONTEXT:\n{self._context}")

        # --- Phase-aware: first iteration checklist ---
        if self._iteration == 0:
            parts.append(REF_FIRST_ITERATION)

        # --- Phase-aware: quality failure guidance ---
        if self._last_quality_passed is False:
            parts.append(REF_QUALITY_FAILURE)

        # --- Phase-aware: repeated errors ---
        if len(self._recent_errors) >= 2:
            parts.append(REF_REPAIR_LOOP)

        # --- Phase-aware: quality passed with warnings ---
        if self._last_quality_passed is True and self._last_quality_summary:
            if "warn" in self._last_quality_summary.lower():
                parts.append(REF_QUALITY_PASSED_WARN)

        # --- Phase-aware: approaching iteration limit ---
        if self._iteration >= _config.MAX_ITERATIONS - 2 and self._iteration > 0:
            parts.append(
                REF_ITERATION_URGENCY.format(
                    max_iter=_config.MAX_ITERATIONS,
                    current=self._iteration,
                )
            )

        return "".join(parts)

    def prepare_llm_call(self) -> LoopAction:
        if self._stopped:
            return LoopAction(
                kind=LoopActionKind.FINISH,
                success=False,
                summary="Agent stopped.",
                iterations=self._iteration,
            )

        if self._iteration >= _config.MAX_ITERATIONS:
            return LoopAction(
                kind=LoopActionKind.FINISH,
                success=False,
                summary=(self._rim_pipeline.limit_summary(_config.MAX_ITERATIONS)
                         if self._rim_pipeline else "Max iterations reached."),
                iterations=self._iteration,
            )

        # Safe boundary: queued steering input joins the next request
        self._drain_steering()

        self._iteration += 1

        # 轮辋逆向扩展：按工作流提供工具，通用建模保持原有工具范围。
        rim_names = {t["function"]["name"] for t in RIM_TOOL_DEFINITIONS}
        workflow_tools = (RIM_TOOL_DEFINITIONS if self._workflow in ("rim_sections", "rim_reconstruction", "spoke_reconstruction") else
                          [t for t in TOOL_DEFINITIONS if t["function"]["name"] not in rim_names])
        # 轮辋逆向扩展修复：完整工具集始终声明，前置条件由执行门禁校验。
        if self._rim_pipeline is not None:
            workflow_tools = [t for t in workflow_tools
                              if t["function"]["name"] in RIM_RECONSTRUCTION_TOOL_NAMES]
        tools = workflow_tools if self._mode != "react" else None
        # Not advertised on ordinary reconstruction turns; no automatic seventh step.
        if self._evaluation_requested:
            tools = [RIM_EVALUATION_TOOL_DEFINITION]
        elif tools is not None:
            tools = [t for t in tools if t["function"]["name"] != "rim_evaluate_revolution"]

        request_messages = self._controller.session.get_messages()
        if self._rim_pipeline is not None:
            # 轮辋逆向扩展：当前请求附上实际工具列表及可执行步骤，不使用模型记忆推断。
            request_messages.append({"role": "system", "content":
                "本次请求确实提供以下完整工具：" + ", ".join(RIM_RECONSTRUCTION_TOOL_NAMES)
                + "。阶段未就绪不等于工具未安装。前置步骤成功后继续调用下一步。当前程序状态："
                + json.dumps(self._rim_pipeline.request_context(), ensure_ascii=False)})
        original_count = len(request_messages)
        #trim_messages 会根据 token 预算限制对消息进行裁剪,以确保不会超过模型的最大 token 限制
        messages = trim_messages(request_messages)
        used, budget = token_summary(messages)
        trimmed = len(messages) < original_count

        log_info(
            f"LLM call iteration {self._iteration}/{_config.MAX_ITERATIONS}, "
            f"mode={self._mode}, {len(messages)} messages"
        )

        #通知外部调用者启动大模型请求
        return LoopAction(
            kind=LoopActionKind.CALL_LLM,
            messages=messages,
            tools=tools,
            iteration=self._iteration,
            token_used=used,
            token_budget=budget,
            token_trimmed=trimmed,
            status_state="thinking",
        )

    #解析大模型返回的数据
    def handle_stream_done(self, data: dict, has_streaming_text: bool) -> LoopAction:
        choices = data.get("choices") or []
        if not choices:
            # Malformed/empty API response — never index blindly (S2-A10)
            return LoopAction(
                kind=LoopActionKind.FINISH,
                success=False,
                summary="API returned a malformed or empty response.",
                iterations=self._iteration,
            )
        choice = choices[0]
        assistant_msg = choice.get("message", {})
        finish_reason = choice.get("finish_reason", "")
        #content 是大模型返回的文本内容,has_tools 表示是否有工具调用
        content = assistant_msg.get("content", "")
        has_tools = bool(assistant_msg.get("tool_calls"))
        log_info(
            f"LLM response: finish_reason={finish_reason}, "
            f"has_tool_calls={has_tools}, content_len={len(content)}"
        )

        self._controller.session.add_assistant_message(assistant_msg)

        # User pressed stop during this stream: keep the partial answer in
        # history but finish as an interrupted turn — never present the
        # truncated text as a successful final answer (S3-D7).
        if self._stopped:
            return LoopAction(
                kind=LoopActionKind.FINISH,
                success=False,
                summary="Stopped by user.",
                from_stream=False,
                iterations=self._iteration,
            )

        if self._evaluation_requested:
            calls = assistant_msg.get("tool_calls") or []
            if calls:
                if len(calls) != 1 or calls[0].get("function", {}).get("name") != "rim_evaluate_revolution":
                    return self.handle_error("评估任务仅允许调用一个 rim_evaluate_revolution")
                return LoopAction(kind=LoopActionKind.EXECUTE_TOOLS, tool_calls=calls)
            if not self._evaluation_attempted and self._rim_continuation_prompts < 1:
                self._rim_continuation_prompts += 1
                self._controller.session.add_user_message(
                    "本轮明确要求实际评估，尚未调用 rim_evaluate_revolution；请先调用工具读取当前模型，不要估计指标。")
                return self.prepare_llm_call()
            return LoopAction(kind=LoopActionKind.FINISH, success=self._evaluation_completed,
                              summary=content, from_stream=has_streaming_text, iterations=self._iteration)

        # 轮辋逆向扩展：统一处理所有截面结束分支，禁止首轮纯文本绕过阶段校验。
        if self._workflow in ("rim_sections", "rim_reconstruction", "spoke_reconstruction"):
            calls = assistant_msg.get("tool_calls") or []
            if calls:
                allowed_names = {t["function"]["name"] for t in RIM_TOOL_DEFINITIONS}
                if any(tc.get("function", {}).get("name") not in allowed_names for tc in calls):
                    return self.handle_error("rim_sections 工作流只允许已声明的轮辋工具")
                if self._rim_pipeline is not None and len(calls)>1:
                    ids=[tc.get("id") for tc in calls]
                    if any(not isinstance(i,str) or not i for i in ids) or len(set(ids))!=len(ids):
                        return self.handle_error("批量工具调用必须包含不同的有效tool_call_id")
                    # The panel already executes a queue serially. Only permit
                    # operations whose inputs were available before this reply;
                    # upstream reconstruction changes still require a new turn.
                    independent={"rim_fit_surface_patch","rim_build_window_cutting_solid",
                        "rim_import_window_cutter","rim_preview_window_pattern","rim_validate_window_pattern",
                        "rim_create_cutter_pattern","rim_list_spoke_objects","rim_analyze_window_pattern"}
                    if len(calls)>8 or any(tc['function']['name'] not in independent for tc in calls):
                        results=[ToolExecution(tc['function']['name'],tc['function'].get('arguments','{}'),tc['id'],'',
                            json.dumps({'status':'error','error':{'code':'BATCH_REQUIRES_SEPARATE_ROUNDS',
                            'message':'本批未执行。最多批量提交8个已有输入的独立曲面/窗口工具；检查、轴线、截面、候选、回转及最终切割须分别等待真实结果。'}},ensure_ascii=False),True) for tc in calls]
                        return self.handle_tool_results(results)
                return LoopAction(kind=LoopActionKind.EXECUTE_TOOLS, tool_calls=calls)
            if finish_reason != "stop":
                return self.handle_error("截面响应未正常完成，请检查模型的 Tool Calling 支持")
            allowed, reason = self._check_quality_gate()
            # 轮辋逆向扩展修复：有已就绪后续工具却提前纯文本结束时，提醒一次再请求。
            if (not allowed and self._rim_pipeline is not None
                    and self._rim_pipeline.next_step() is not None
                    and self._rim_continuation_prompts < 1):
                self._rim_continuation_prompts += 1
                self._controller.session.add_user_message(
                    "程序状态提示：任务尚未完成，后续工具已声明且当前可执行下一步 "
                    + self._rim_pipeline.next_step()
                    + "。请使用实际产物引用继续调用，不要将阶段切换误判为工具不可用。")
                return self.prepare_llm_call()
            # 轮辋逆向扩展：完整重建的失败说明不能仍称为“截面阶段”。
            stage = "重建未完成" if self._workflow == "rim_reconstruction" else "截面阶段未完成"
            return LoopAction(kind=LoopActionKind.FINISH, success=allowed,
                summary=content if allowed else f"{content}\n\n{stage}：{reason}",
                from_stream=has_streaming_text if allowed else False, iterations=self._iteration)

        # --- Mode auto-detection (first iteration) ---
        if self._mode == "auto" and finish_reason == "stop" and self._iteration == 1:
            parsed = parse_react_tool_calls(content)
            if parsed:
                self._mode = "react"
                self._controller.session.last_mode = "react"
                ctx = self._build_context()
                react_prompt = REACT_SYSTEM_PROMPT.replace("{context}", ctx)
                self._controller.session.set_system_prompt(react_prompt)
                return LoopAction(
                    kind=LoopActionKind.EXECUTE_TOOLS,
                    tool_calls=parsed,
                    system_message="Model does not support tool calling, switched to ReAct mode.",
                )
            else:
                self._mode = "tool_calling"
                self._controller.session.last_mode = "tool_calling"
                return LoopAction(
                    kind=LoopActionKind.FINISH,
                    success=True,
                    summary=content,
                    from_stream=has_streaming_text,
                    iterations=self._iteration,
                )

        #识别并返回工具调用请求
        # --- Tool calling mode (native API) ---
        if finish_reason == "tool_calls":
            self._mode = "tool_calling"
            self._controller.session.last_mode = "tool_calling"
            return LoopAction(
                kind=LoopActionKind.EXECUTE_TOOLS,
                tool_calls=assistant_msg.get("tool_calls", []),
            )

        # --- ReAct mode: check for <tool> tags ---
        if self._mode == "react":
            parsed = parse_react_tool_calls(content)
            if parsed:
                return LoopAction(
                    kind=LoopActionKind.EXECUTE_TOOLS,
                    tool_calls=parsed,
                )
            allowed, reason = self._check_quality_gate()
            if not allowed:
                return self._quality_gate_block(reason)
            return LoopAction(
                kind=LoopActionKind.FINISH,
                success=True,
                summary=content,
                iterations=self._iteration,
            )

        # --- finish_reason == "stop" with no tools — agent done ---
        allowed, reason = self._check_quality_gate()
        if not allowed:
            return self._quality_gate_block(reason)
        return LoopAction(
            kind=LoopActionKind.FINISH,
            success=True,
            summary=content,
            from_stream=has_streaming_text,
            iterations=self._iteration,
        )

    #接受工具执行结果,并根据结果更新状态,返回下一步操作，即决定是否继续请求LLM
    def handle_tool_results(self, executions: list[ToolExecution]) -> LoopAction:
        for ex in executions:
            result_text = ex.result
            if ex.tool_name == "rim_evaluate_revolution":
                self._evaluation_attempted = True
                try:
                    evaluation = json.loads(ex.result)
                    self._evaluation_completed = (not ex.is_error and evaluation.get("status") == "success"
                        and evaluation.get("operation") == "evaluate_revolution" and bool(evaluation.get("metrics")))
                except (ValueError, TypeError, AttributeError):
                    self._evaluation_completed = False

            # 轮辋逆向扩展：处理工具结果后更新数据引用和下游失效状态。
            if self._rim_pipeline is not None:
                self._rim_pipeline.record(ex)
            if ex.tool_name in ("rim_repair_sections", "rim_process_sections", "rim_analyze_candidates", "rim_fit_profile"):
                self._rim_build_completed = None
            elif ex.tool_name == "rim_build_revolution":
                try:
                    built = json.loads(ex.result)
                    self._rim_build_completed = (not ex.is_error and built.get("status") == "success"
                        and built.get("operation") == "build_rim_revolution"
                        and built.get("solid_count") == 1 and built.get("volume_mm3", 0) > 0)
                except (ValueError, TypeError, AttributeError):
                    self._rim_build_completed = False

            # 轮辋逆向扩展：不以模型文字或此前的截面成功掩盖拟合失败。
            if self._workflow in ("rim_sections", "rim_reconstruction", "spoke_reconstruction"):
                if ex.tool_name in ("rim_repair_sections", "rim_process_sections", "rim_analyze_candidates"):
                    self._rim_fit_completed = None
                elif ex.tool_name == "rim_fit_profile":
                    self._rim_fit_completed = False
                    try:
                        fit = json.loads(ex.result)
                        self._rim_fit_completed = (
                            not ex.is_error and isinstance(fit, dict)
                            and fit.get("status") == "success" and fit.get("operation") == "fit_profile"
                            and fit.get("fit_success") is True and fit.get("compliant") is True
                            and bool(fit.get("result_path")))
                    except (ValueError, TypeError):
                        pass

            # 轮辋逆向扩展：只信任本轮实际工具的有效摘要；失败会撤销此前成功状态。
            if self._workflow in ("rim_sections", "rim_reconstruction", "spoke_reconstruction") and ex.tool_name == "rim_analyze_candidates":
                self._rim_analysis_completed = False
                try:
                    analysis = json.loads(ex.result)
                    self._rim_analysis_completed = (
                        not ex.is_error and isinstance(analysis, dict)
                        and analysis.get("status") == "success"
                        and analysis.get("operation") == "analyze_rim_candidates"
                        and analysis.get("analysis_status") in ("candidates_found", "needs_review")
                        and bool(analysis.get("analysis_id")) and bool(analysis.get("analysis_path"))
                    )
                except (ValueError, TypeError):
                    pass
            if self._workflow in ("rim_sections", "rim_reconstruction", "spoke_reconstruction") and ex.tool_name == "rim_process_sections":
                self._rim_analysis_completed = None
                self._rim_sections_succeeded = False
                try:
                    result = json.loads(ex.result)
                    self._rim_sections_succeeded = (
                        not ex.is_error and isinstance(result, dict)
                        and result.get("status") == "success"
                        and result.get("operation") == "process_sections"
                        and result.get("unit") == "mm"
                        and bool(result.get("run_id"))
                        and isinstance(result.get("sections"), list) and bool(result["sections"])
                        and isinstance(result.get("section_artifacts"), list)
                        and len(result["section_artifacts"]) == len(result["sections"])
                    )
                except (ValueError, TypeError):
                    pass

            if self._workflow == "rim_sections" and ex.tool_name == "rim_transverse_sections":
                self._rim_analysis_completed = self._rim_fit_completed = self._rim_build_completed = None
                self._rim_sections_succeeded = False
                try:
                    transverse = json.loads(ex.result)
                    self._rim_sections_succeeded = (not ex.is_error and transverse.get("status") == "success"
                        and transverse.get("operation") == "transverse_sections" and transverse.get("unit") == "mm"
                        and bool(transverse.get("result_path")) and bool(transverse.get("sections")))
                except (ValueError, TypeError, AttributeError):
                    pass

            # Error dedup: warn if same error keeps recurring
            if ex.is_error:
                err_sig = ex.result[:120]
                is_repeated = any(
                    err_sig[:80] == prev[:80] for prev in self._recent_errors
                )
                if is_repeated:
                    # Inject targeted repair hint from error_hint() if available
                    repair_hint = self._get_error_repair_hint(ex)
                    if repair_hint:
                        result_text = (
                            ex.result
                            + "\n\nWARNING: REPEATED ERROR.\n"
                            + repair_hint
                            + "\nYou MUST change your approach. "
                            "Do NOT resubmit similar code."
                        )
                    else:
                        result_text = (
                            ex.result
                            + "\n\nWARNING: REPEATED ERROR. "
                            "You MUST change your approach. "
                            "Do NOT resubmit similar code."
                        )
                self._recent_errors.append(err_sig)
                if len(self._recent_errors) > 5:
                    self._recent_errors.pop(0)

            if self._mode == "react":
                self._controller.session.add_user_message(
                    f"[Tool Result for {ex.tool_name}]:\n{result_text}"
                )
            else:
                self._controller.session.add_tool_result(ex.tool_id, result_text)

            if ex.is_error:
                self._controller.result.errors.append(
                    f"[Iter {self._iteration}] {ex.tool_name}: {ex.result[:100]}"
                )
            self._controller.result.tool_calls_log.append({
                "iteration": self._iteration,
                "name": ex.tool_name,
                "description": ex.description,
                "result_preview": ex.result[:200],
                "is_error": ex.is_error,
            })

            # Phase 1.3: Track quality state from execute_code results
            if ex.tool_name == "execute_code":
                self._execute_code_called = True
                first_line = ex.result.split("\n", 1)[0]
                if ex.result.startswith("OK:"):
                    self._last_quality_passed = True
                    # A passing quality check resets the escape-hatch counter:
                    # the hatch fires on 3 CONSECUTIVE blocks, not 3 total
                    # across the whole turn (S2-A11).
                    self._quality_gate_blocks = 0
                elif ex.result.startswith("FAIL:") or ex.result.startswith("ERROR:"):
                    self._last_quality_passed = False
                elif ex.result.startswith("AUTO-FIX APPLIED:"):
                    # Legacy auto-fix header (pre-S1 tools.py): judge by the
                    # embedded quality report lines instead of failing blind.
                    self._last_quality_passed = not any(
                        line.startswith(("FAIL:", "ERROR:"))
                        for line in ex.result.split("\n")
                    )
                else:
                    self._last_quality_passed = False
                self._last_quality_summary = first_line
                self._controller.result.last_quality_passed = self._last_quality_passed
                self._controller.result.last_quality_summary = self._last_quality_summary

        #工具执行完毕后不是直接结束任务，而是重新准备一次 LLM 请求
        return self.prepare_llm_call()

    def _check_quality_gate(self) -> tuple[bool, str]:
        """Check whether the quality gate allows FINISH with success=True.

        Returns (allowed, reason). allowed=True if gate passes or is not
        applicable (no execute_code has run yet AND no document context).

        Escape hatch: after 3 consecutive quality gate blocks, allow FINISH
        with success=False to prevent infinite retry loops.
        """
        if self._evaluation_requested:
            return self._evaluation_completed, "本轮尚未成功执行模型评估。"
        # 轮辋逆向扩展：完整工作流必须有有效回转实体；截面模式保持阶段兼容。
        if self._rim_pipeline is not None:
            return self._rim_pipeline.completion()
        if self._workflow == "rim_sections" and self._rim_build_completed is not None:
            return (self._rim_build_completed, "本轮回转实体创建未通过。")
        # 轮辋逆向扩展：截面阶段不要求活动文档、execute_code 或有效实体。
        if self._workflow in ("rim_sections", "rim_reconstruction", "spoke_reconstruction"):
            # 轮辋逆向扩展：拟合结果优先于截面与分析的完成状态。
            if self._rim_fit_completed is not None:
                return (self._rim_fit_completed, "本轮拟合未通过，请检查拟合工具错误。")
            if self._rim_analysis_completed is not None:
                return (self._rim_analysis_completed, "本轮候选分析未完成，请检查分析工具错误。")
            return (self._rim_sections_succeeded,
                    "本轮尚无有效的截面成功结果；请补齐参数或检查工具错误。")
        if not self._execute_code_called and self._context:
            return False, (
                "No execute_code was called. When there is an active document, "
                "you must use execute_code to create or modify geometry before finishing."
            )
        if self._last_quality_passed is None or self._last_quality_passed:
            return True, ""
        return False, self._last_quality_summary

    def _quality_gate_block(self, reason: str) -> LoopAction:
        """Block FINISH, inject quality feedback with targeted fix suggestion.

        Inspired by text-to-cad's classified repair loop: instead of a generic
        "fix geometry" message, extract the specific QualityIssue code from the
        failure reason and inject a targeted repair instruction from
        QUALITY_FIX_MAP.

        Escape hatch: after 3 consecutive blocks, allow FINISH with
        success=False to prevent infinite retry loops where the agent
        cannot fix the quality issue.
        """
        self._quality_gate_blocks += 1

        # Escape hatch — stop forcing retries after 3 failed quality gates
        if self._quality_gate_blocks >= 3:
            log_info(
                f"Quality gate escape hatch: {self._quality_gate_blocks} blocks, "
                f"allowing FINISH with failure"
            )
            return LoopAction(
                kind=LoopActionKind.FINISH,
                success=False,
                summary=(
                    f"Agent stopped after quality gate blocked {self._quality_gate_blocks} "
                    f"attempts. Last issue: {reason}"
                ),
                iterations=self._iteration,
            )

        fix_hint = self._get_quality_fix_suggestion()
        if fix_hint:
            msg = (
                f"CAD QUALITY GATE: {reason}\n"
                f"Fix: {fix_hint}\n\n"
                "You MUST use execute_code to fix the issue before finishing. "
                "Do NOT respond with a summary — write code to fix the geometry."
            )
        else:
            msg = (
                f"CAD QUALITY GATE: {reason}\n\n"
                "You MUST use execute_code to fix the issue before finishing. "
                "Do NOT respond with a summary — write code to create or fix the geometry."
            )
        self._controller.session.add_user_message(msg)
        log_info(f"Quality gate blocked FINISH: {reason[:100]}")
        return self.prepare_llm_call()

    def _get_quality_fix_suggestion(self) -> str:
        """Extract targeted fix suggestion from the last quality summary.

        Maps known QualityIssue codes (e.g., NO_SOLID, MULTI_SOLID) to
        concrete repair instructions from QUALITY_FIX_MAP.

        Matches both the exact code (NO_SOLID) and natural language variants
        (no solid, separate solids, compound shape, invalid shape, etc.)
        that appear in quality report messages.
        """
        summary = self._last_quality_summary
        if not summary:
            return ""

        # Exact code match first (e.g., "NO_SOLID" in quality JSON)
        for code, hint in QUALITY_FIX_MAP.items():
            if code in summary:
                return hint

        # Natural language fallback — match key phrases from quality messages
        summary_lower = summary.lower()
        nl_map = {
            "no solid": "NO_SOLID",
            "separate solid": "MULTI_SOLID",
            "multiple object": "MULTIPLE_OBJECTS",
            "compound shape": "COMPOUND_SHAPE",
            "invalid shape": "INVALID_SHAPE",
            "negative volume": "NEGATIVE_VOLUME",
            "dimension": "DIMENSION_SUSPICIOUS",
            "no active document": "NO_DOCUMENT",
        }
        for phrase, code in nl_map.items():
            if phrase in summary_lower and code in QUALITY_FIX_MAP:
                return QUALITY_FIX_MAP[code]

        return ""

    def _get_error_repair_hint(self, ex: ToolExecution) -> str:
        """Extract a targeted repair hint for a repeated tool execution error.

        Inspired by text-to-cad's classified repair loop. Uses error_hint()
        from code_fixes.py to classify the error and return a specific fix
        instruction. This runs in-process (no FreeCAD import needed) since
        error_hint() only does regex/string matching on the error text.

        Falls back to generic guidance if error_hint() returns nothing useful.
        """
        # 轮辋逆向扩展：几何服务错误不能套用布尔运算和 execute_code 修复建议。
        if self._workflow in ("rim_sections", "rim_reconstruction", "spoke_reconstruction"):
            return "检查截面错误码、输入路径和明确提供的坐标系；缺失信息时询问用户，不猜测几何参数。"
        try:
            from agent.code_fixes import error_hint

            # Build a lightweight exception object from the error text
            # error_hint() matches on exception type name and message string
            err_text = ex.result
            # Try to extract exception type from common error prefixes
            # e.g., "NameError: name 'x' is not defined"
            exc_type = None
            exc_msg = err_text
            for known_type in ("NameError", "AttributeError", "TypeError",
                               "ValueError", "RuntimeError", "KeyError",
                               "IndexError", "ImportError"):
                if err_text.startswith(known_type):
                    exc_type = known_type
                    # Extract message after "TypeName: "
                    if ":" in err_text:
                        exc_msg = err_text.split(":", 1)[1].strip()
                    break

            if exc_type:
                # Create a minimal exception-like object for error_hint()
                class _FakeExc(Exception):
                    def __init__(self):
                        self.__class__.__name__ = exc_type
                        Exception.__init__(self, exc_msg)

                hint_text, _ = error_hint(_FakeExc(), ex.description or "")
                if hint_text:
                    return f"Repair hint: {hint_text}"
        except Exception:
            pass  # Non-critical — fall back to generic guidance

        # Generic fallback for repeated errors
        return ("Repair hint: Change ONE thing at a time — overlap distance, "
                "boolean order, or construction method. If stuck, use "
                "undo_last and try a different approach.")

    def handle_error(self, error_msg: str) -> LoopAction:
        return LoopAction(
            kind=LoopActionKind.FINISH,
            success=False,
            summary=f"API error: {error_msg}",
            iterations=self._iteration,
        )
