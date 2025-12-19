"""
nodes.py 

Scalable LangGraph node implementations

Update goals:
- Remove hardcoded tool names from nodes; accept ToolingConfig injection from workflow.py.
- Make perception payload strategy explicit (hash-only vs b64) to support MCP vision_server.
- Standardize node inputs/outputs for extensibility (new servers/tools do not require node rewrites).
- Keep nodes framework-agnostic: no LangGraph imports.

This file assumes:
- state.py defines AgentState, ToolCall, ToolResult, etc.
- workflow.py provides:
    - ToolExecutor (call/has)
    - ToolingConfig (alias names)
    - Planner/Selector/Verifier/Recovery injected

Primary scalability decisions:
- Tool alias names are always used; executor resolves to local or MCP.
- Perception is modular: capture -> optional UIA tree -> optional vision parse.
- Action selection & recovery remain external functions; nodes only execute/record.
"""

from __future__ import annotations

import traceback
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple

from .state import (
    AgentState,
    ErrorCode,
    Plan,
    Step,
    ToolCall,
    ToolResult,
    ActionRecord,
    PerceptionSnapshot,
    fingerprint,
    now_ms,
    new_id,
)


# -----------------------------
# Executor + ToolingConfig protocols
# -----------------------------

class ToolExecutor(Protocol):
    def call(self, call: ToolCall) -> ToolResult: ...
    def has(self, tool_alias: str) -> bool: ...


class ToolingConfigLike(Protocol):
    # Vision
    screen_capture: str
    omniparser_v2_parse: str
    screenshot_diff: str

    # UIA
    focus_window: str
    uia_tree: str
    uia_click: str
    uia_set_text: str

    # Mouse/Keyboard
    click: str
    double_click: str
    right_click: str
    move: str
    drag: str
    scroll: str
    type_text: str
    hotkey: str
    key_down: str
    key_up: str

    # Local utility
    wait: str
    ping: str
    time_now_ms: str
    clipboard_get: str
    clipboard_set: str


# -----------------------------
# Planner / Selector / Verifier / Recovery contracts
# -----------------------------

PlannerFn = Callable[[AgentState], Plan]
ActionSelectorFn = Callable[[AgentState, ToolingConfigLike], List[ToolCall]]
VerifierFn = Callable[[AgentState, ToolingConfigLike], Tuple[bool, Dict[str, Any]]]
RecoveryFn = Callable[[AgentState, ToolingConfigLike], List[ToolCall]]


# -----------------------------
# Internal helpers
# -----------------------------

def _ensure_plan(state: AgentState) -> bool:
    return state.plan is not None and state.plan.is_valid


def _step_timeout_exceeded(state: AgentState) -> bool:
    if not _ensure_plan(state):
        return False
    if state.last_step_started_ms is None:
        return False
    return (now_ms() - state.last_step_started_ms) > state.plan.current.timeout_ms


def _terminal_fail(state: AgentState, reason: str, code: str) -> AgentState:
    state.set_terminal("FAILED", reason, code=code)
    return state


def _terminal_escalate(state: AgentState, reason: str, code: str = ErrorCode.ESCALATED) -> AgentState:
    state.set_terminal("ESCALATED", reason, code=code)
    return state


def _terminal_done(state: AgentState, reason: str) -> AgentState:
    state.set_terminal("DONE", reason, code=ErrorCode.DONE)
    return state


def _record_action(
    state: AgentState,
    *,
    tool: str,
    args: Dict[str, Any],
    idempotency_key: str,
    started_ms: int,
    ended_ms: int,
    ok: bool,
    error: Optional[str],
    effect_fingerprint: Optional[str],
) -> None:
    state.actions.append(
        ActionRecord(
            action_id=new_id("act"),
            tool=tool,
            args=args,
            idempotency_key=idempotency_key,
            started_ms=started_ms,
            ended_ms=ended_ms,
            ok=ok,
            error=error,
            effect_fingerprint=effect_fingerprint,
        )
    )


def _safe_err(e: Exception) -> str:
    return f"{type(e).__name__}: {e}"


def _toolcall_key(state: AgentState, step: Step, tool: str, args: Dict[str, Any], suffix: str = "") -> str:
    base = f"{state.run_id}:{step.id}:{tool}:{fingerprint(args)}"
    return f"{base}:{suffix}" if suffix else base


# -----------------------------
# Nodes
# -----------------------------

def node_initialize(state: AgentState) -> AgentState:
    sp = state.telemetry.span("node_initialize")
    try:
        state.ensure_run_id()
        state.ensure_policy_defaults()
        state.status = "PLANNING"
        state.telemetry.event("initialized", run_id=state.run_id)
        return state
    finally:
        sp.close()


def node_plan(state: AgentState, planner: PlannerFn) -> AgentState:
    sp = state.telemetry.span("node_plan")
    try:
        if state.status not in ("PLANNING", "INIT"):
            state.telemetry.event("plan_skipped", status=state.status)
            return state

        plan = planner(state)
        if not isinstance(plan, Plan) or not plan.steps:
            return _terminal_fail(state, "Planner returned empty/invalid plan", ErrorCode.PLAN_INVALID)

        plan.finalize()
        state.plan = plan
        state.plan.current_step_idx = 0
        state.last_step_started_ms = now_ms()
        state.status = "PERCEIVING"

        state.telemetry.event(
            "plan_created",
            objective=plan.objective,
            step_count=len(plan.steps),
            plan_fingerprint=plan.plan_fingerprint,
        )
        return state
    except Exception as e:
        state.telemetry.event("plan_error", error=_safe_err(e))
        return _terminal_fail(state, f"Planner error: {_safe_err(e)}", ErrorCode.PLAN_ERROR)
    finally:
        sp.close()


def node_perceive(
    state: AgentState,
    executor: ToolExecutor,
    tooling: ToolingConfigLike,
    *,
    store_screenshot_b64: bool,
    prefer_uia_tree: bool = True,
    omniparser_enabled: bool = True,
) -> AgentState:
    """
    MCP-friendly perception.

    Payload strategy:
    - If store_screenshot_b64=False:
        screen_capture MUST return a stable screenshot_hash,
        and vision_server.omniparser_v2_parse MUST be able to resolve by image_hash
        OR you must set store_screenshot_b64=True.
    - If store_screenshot_b64=True:
        screen_capture returns b64; passed to omniparser directly.

    Gating:
    - state.scratch["need_vision"] can disable omniparser for steps solvable via UIA.
    - state.scratch["focus_hint"] can guide focus_window.
    """
    sp = state.telemetry.span("node_perceive")
    try:
        if not _ensure_plan(state):
            return _terminal_fail(state, "Perceive called without a valid plan", ErrorCode.NO_PLAN)
        if _step_timeout_exceeded(state):
            return _terminal_fail(state, "Step timeout exceeded (perceive)", ErrorCode.STEP_TIMEOUT)

        step = state.plan.current

        # Focus (optional)
        focus_hint = state.scratch.get("focus_hint")
        if focus_hint and executor.has(tooling.focus_window):
            args = {"hint": focus_hint}
            executor.call(
                ToolCall(
                    name=tooling.focus_window,
                    args=args,
                    idempotency_key=_toolcall_key(state, step, tooling.focus_window, args),
                    timeout_ms=15_000,
                )
            )

        # Capture (required)
        if not executor.has(tooling.screen_capture):
            return _terminal_fail(state, f"Missing required tool alias: {tooling.screen_capture}", ErrorCode.TOOL_MISSING)

        cap_args = {"return_b64": bool(store_screenshot_b64)}
        cap = executor.call(
            ToolCall(
                name=tooling.screen_capture,
                args=cap_args,
                idempotency_key=_toolcall_key(state, step, tooling.screen_capture, cap_args),
                timeout_ms=30_000,
            )
        )
        if not cap.ok or not isinstance(cap.data, dict):
            state.telemetry.event("perceive_capture_failed", error=cap.error)
            state.status = "RECOVERING"
            return state

        snap = PerceptionSnapshot(
            screenshot_hash=cap.data.get("hash"),
            screenshot_b64=cap.data.get("b64") if store_screenshot_b64 else None,
            focused_window=cap.data.get("focused_window"),
            ts_ms=cap.data.get("ts_ms", now_ms()),
        )

        # UIA tree (optional but preferred)
        if prefer_uia_tree and executor.has(tooling.uia_tree):
            uia_args = {"scope": "focused_window"}
            uia = executor.call(
                ToolCall(
                    name=tooling.uia_tree,
                    args=uia_args,
                    idempotency_key=_toolcall_key(state, step, tooling.uia_tree, uia_args, suffix=str(snap.screenshot_hash)),
                    timeout_ms=30_000,
                )
            )
            if uia.ok:
                snap.uia_tree = uia.data

        # OmniParser v2 (conditional)
        need_vision = bool(state.scratch.get("need_vision", True))
        if omniparser_enabled and need_vision and executor.has(tooling.omniparser_v2_parse):
            # Pass b64 if available, else rely on image_hash resolution server-side.
            omni_args: Dict[str, Any] = {
                "image_b64": snap.screenshot_b64,
                "image_hash": snap.screenshot_hash,
                "context": {
                    "goal": state.goal,
                    "step": {
                        "id": step.id,
                        "title": step.title,
                        "intent": step.intent,
                        "success_criteria": step.success_criteria,
                    },
                    "focused_window": snap.focused_window,
                },
            }
            omni_idem_args = {"image_hash": snap.screenshot_hash, "step_id": step.id}
            omni = executor.call(
                ToolCall(
                    name=tooling.omniparser_v2_parse,
                    args=omni_args,
                    idempotency_key=_toolcall_key(state, step, tooling.omniparser_v2_parse, omni_idem_args),
                    timeout_ms=60_000,
                )
            )
            if omni.ok:
                if isinstance(omni.data, dict):
                    snap.elements = omni.data.get("elements", []) or []
                elif isinstance(omni.data, list):
                    snap.elements = omni.data
                else:
                    snap.elements = []
            else:
                state.telemetry.event("omniparser_failed", error=omni.error)

        state.perception = snap
        state.status = "POLICY_CHECK"
        state.telemetry.event(
            "perceived",
            screenshot_hash=snap.screenshot_hash,
            elements=len(snap.elements),
            has_uia_tree=bool(snap.uia_tree),
            focused_window=snap.focused_window,
            store_screenshot_b64=store_screenshot_b64,
        )
        return state
    except Exception as e:
        state.telemetry.event("perceive_error", error=_safe_err(e), traceback=traceback.format_exc())
        state.status = "RECOVERING"
        return state
    finally:
        sp.close()


def node_policy_check(state: AgentState) -> AgentState:
    sp = state.telemetry.span("node_policy_check")
    try:
        if not _ensure_plan(state):
            return _terminal_fail(state, "Policy check called without a valid plan", ErrorCode.NO_PLAN)

        step = state.plan.current

        if state.policy.require_approval_for_high_risk and step.risk == "HIGH":
            if not state.approved:
                state.requires_human_approval = True
                state.status = "WAITING_APPROVAL"
                state.policy.last_decision = "REQUIRE_APPROVAL"
                state.telemetry.event("approval_required", step_id=step.id, risk=step.risk)
                return state

        state.requires_human_approval = False
        state.policy.last_decision = "ALLOW"
        state.status = "ACTING"
        state.telemetry.event("policy_allowed", step_id=step.id, risk=step.risk)
        return state
    finally:
        sp.close()


def node_act(
    state: AgentState,
    executor: ToolExecutor,
    tooling: ToolingConfigLike,
    selector: ActionSelectorFn,
    *,
    post_action_capture: bool = True,
) -> AgentState:
    """
    Executes ToolCall[] returned by selector(state, tooling).
    Post-action capture (hash) is optional; recommended for verification and audit.
    """
    sp = state.telemetry.span("node_act")
    try:
        if not _ensure_plan(state):
            return _terminal_fail(state, "Act called without a valid plan", ErrorCode.NO_PLAN)
        if state.perception is None:
            state.telemetry.event("act_missing_perception")
            state.status = "RECOVERING"
            return state
        if _step_timeout_exceeded(state):
            return _terminal_fail(state, "Step timeout exceeded (act)", ErrorCode.STEP_TIMEOUT)

        step = state.plan.current
        calls = selector(state, tooling)
        if not calls:
            state.telemetry.event("no_actions_selected", step_id=step.id)
            state.status = "RECOVERING"
            return state

        for call in calls:
            started = now_ms()
            res = executor.call(call)
            ended = now_ms()

            effect_fp = None
            if post_action_capture and executor.has(tooling.screen_capture):
                pc_args = {"return_b64": False}
                pc = executor.call(
                    ToolCall(
                        name=tooling.screen_capture,
                        args=pc_args,
                        idempotency_key=f"{call.idempotency_key}:postcap",
                        timeout_ms=20_000,
                    )
                )
                if pc.ok and isinstance(pc.data, dict):
                    effect_fp = pc.data.get("hash")

            _record_action(
                state,
                tool=call.name,
                args=call.args,
                idempotency_key=call.idempotency_key,
                started_ms=started,
                ended_ms=ended,
                ok=res.ok,
                error=res.error,
                effect_fingerprint=effect_fp,
            )

            if not res.ok:
                if res.error and str(res.error).startswith("POLICY_DENY"):
                    state.telemetry.event("policy_denied_runtime", step_id=step.id, tool=call.name, error=res.error)
                    return _terminal_escalate(state, f"Policy denied tool at runtime: {res.error}", ErrorCode.POLICY_DENY)

                state.telemetry.event("action_failed", step_id=step.id, tool=call.name, error=res.error)
                state.status = "RECOVERING"
                return state

        state.status = "VERIFYING"
        state.telemetry.event("actions_completed", step_id=step.id, action_count=len(calls))
        return state
    except Exception as e:
        state.telemetry.event("act_error", error=_safe_err(e), traceback=traceback.format_exc())
        state.status = "RECOVERING"
        return state
    finally:
        sp.close()


def node_verify(state: AgentState, tooling: ToolingConfigLike, verifier: VerifierFn) -> AgentState:
    sp = state.telemetry.span("node_verify")
    try:
        if not _ensure_plan(state):
            return _terminal_fail(state, "Verify called without a valid plan", ErrorCode.NO_PLAN)
        if state.perception is None:
            state.status = "RECOVERING"
            return state
        if _step_timeout_exceeded(state):
            return _terminal_fail(state, "Step timeout exceeded (verify)", ErrorCode.STEP_TIMEOUT)

        step = state.plan.current
        ok, details = verifier(state, tooling)
        state.scratch["verify_details"] = details
        state.telemetry.event("step_verified", step_id=step.id, ok=ok, details=details)

        if ok:
            finished = state.plan.advance()
            if finished:
                return _terminal_done(state, "All steps completed")
            state.last_step_started_ms = now_ms()
            state.status = "PERCEIVING"
            return state

        state.status = "RECOVERING"
        return state
    except Exception as e:
        state.telemetry.event("verify_error", error=_safe_err(e), traceback=traceback.format_exc())
        state.status = "RECOVERING"
        return state
    finally:
        sp.close()


def node_recover(
    state: AgentState,
    executor: ToolExecutor,
    tooling: ToolingConfigLike,
    recovery: RecoveryFn,
) -> AgentState:
    sp = state.telemetry.span("node_recover")
    try:
        if not _ensure_plan(state):
            return _terminal_fail(state, "Recover called without a valid plan", ErrorCode.NO_PLAN)

        step = state.plan.current

        if not state.retry.can_retry_step(step.id, step.max_retries):
            state.telemetry.event(
                "retry_exhausted",
                step_id=step.id,
                total_used=state.retry.used,
                total_budget=state.retry.total_budget,
                step_used=state.retry.step_retry_counts.get(step.id, 0),
                step_max=step.max_retries,
            )
            return _terminal_fail(state, f"Retry exhausted for step {step.id}", ErrorCode.RETRY_EXHAUSTED)

        state.retry.consume(step.id)
        state.telemetry.event(
            "recover_attempt",
            step_id=step.id,
            total_used=state.retry.used,
            step_used=state.retry.step_retry_counts.get(step.id, 0),
        )

        calls = recovery(state, tooling)
        for call in calls:
            started = now_ms()
            res = executor.call(call)
            ended = now_ms()

            _record_action(
                state,
                tool=call.name,
                args=call.args,
                idempotency_key=call.idempotency_key,
                started_ms=started,
                ended_ms=ended,
                ok=res.ok,
                error=res.error,
                effect_fingerprint=None,
            )

            if not res.ok and res.error and str(res.error).startswith("POLICY_DENY"):
                return _terminal_escalate(state, f"Policy denied recovery tool: {res.error}", ErrorCode.POLICY_DENY)

        state.status = "PERCEIVING"
        return state
    except Exception as e:
        state.telemetry.event("recover_error", error=_safe_err(e), traceback=traceback.format_exc())
        return _terminal_fail(state, f"Recovery error: {_safe_err(e)}", ErrorCode.RECOVERY_ERROR)
    finally:
        sp.close()


def node_waiting_approval(state: AgentState) -> AgentState:
    sp = state.telemetry.span("node_waiting_approval")
    try:
        state.status = "WAITING_APPROVAL"
        state.telemetry.event("waiting_approval", approved=state.approved)
        return state
    finally:
        sp.close()


def node_finalize(state: AgentState) -> AgentState:
    sp = state.telemetry.span("node_finalize")
    try:
        state.telemetry.event(
            "finalize",
            status=state.status,
            done_reason=state.done_reason,
            action_count=len(state.actions),
            plan_fingerprint=getattr(state.plan, "plan_fingerprint", None),
        )
        return state
    finally:
        sp.close()


# -----------------------------
# Optional helper builders (selectors can use these)
# -----------------------------

def build_click_from_bbox(state: AgentState, tooling: ToolingConfigLike, bbox: List[int]) -> ToolCall:
    if not _ensure_plan(state):
        raise ValueError("No active plan")
    step = state.plan.current
    x1, y1, x2, y2 = bbox
    args = {"x": int((x1 + x2) / 2), "y": int((y1 + y2) / 2)}
    return ToolCall(
        name=tooling.click,
        args=args,
        idempotency_key=_toolcall_key(state, step, tooling.click, args),
        timeout_ms=15_000,
    )


def build_type(state: AgentState, tooling: ToolingConfigLike, text: str) -> ToolCall:
    if not _ensure_plan(state):
        raise ValueError("No active plan")
    step = state.plan.current
    args = {"text": text}
    return ToolCall(
        name=tooling.type_text,
        args=args,
        idempotency_key=_toolcall_key(state, step, tooling.type_text, args),
        timeout_ms=30_000,
    )


def build_hotkey(state: AgentState, tooling: ToolingConfigLike, keys: List[str]) -> ToolCall:
    if not _ensure_plan(state):
        raise ValueError("No active plan")
    step = state.plan.current
    args = {"keys": keys}
    return ToolCall(
        name=tooling.hotkey,
        args=args,
        idempotency_key=_toolcall_key(state, step, tooling.hotkey, args),
        timeout_ms=15_000,
    )


# -----------------------------------------------------------------------------
# DERS NOTU (nodes.py) — Node iş mantığı (ToolingConfig injection ile)
# -----------------------------------------------------------------------------
# Bu dosya, LangGraph “node” fonksiyonlarını framework bağımsız şekilde uygular.
# Yani burada LangGraph import edilmez; StateGraph/END gibi kavramlar workflow.py’de.
#
# Temel prensip:
# - AgentState tek mutable nesnedir.
# - Her node fonksiyonu AgentState’i okur/değiştirir ve geri döndürür.
# - Akış kararı edges.py’deki router fonksiyonları ile (state.status üzerinden) verilir.
#
# Bu sürümde önceki tasarıma göre ana fark:
# - Tool isimleri hardcode edilmez.
# - workflow.py’den “ToolingConfig” benzeri bir nesne enjekte edilir.
# - Nodes, sadece “alias” ile çalışır; local/mcp ayrımını executor çözer.
#
# 1) Protocol’ler (sözleşmeler)
#    - ToolExecutor:
#      call(ToolCall) -> ToolResult
#      has(tool_alias) -> bool
#      Bu executor, tool alias’ını ya local python fonksiyonuna ya da MCP
#      fully-qualified tool’a yönlendirir.
#
#    - ToolingConfigLike:
#      nodes.py’nin ihtiyaç duyduğu tüm tool alias alanlarını listeler.
#      Bu, “hangi tool’lar var?” sorusunun tek yerden tanımlanmasını sağlar.
#
# 2) Dış bağımlılıklar (injected fonksiyonlar)
#    - PlannerFn: state -> Plan
#    - ActionSelectorFn: (state, tooling) -> list[ToolCall]
#    - VerifierFn: (state, tooling) -> (ok, details)
#    - RecoveryFn: (state, tooling) -> list[ToolCall]
#
# 3) İç yardımcılar
#    - _ensure_plan: plan var mı/geçerli mi?
#    - _step_timeout_exceeded: step timeout guard.
#    - _terminal_fail/_terminal_escalate/_terminal_done: terminal durum set eder.
#    - _record_action: ActionRecord append-only log.
#    - _toolcall_key: deterministik idempotency_key üretir:
#      run_id:step_id:tool_alias:fingerprint(args)[:suffix]
#
# 4) Node akışı (yüksek seviye)
#    4.1 node_initialize
#      - run_id üretir, policy default’larını kurar.
#      - status -> PLANNING
#
#    4.2 node_plan
#      - planner(state) çağrılır.
#      - plan.finalize() ile fingerprint üretilir.
#      - status -> PERCEIVING, last_step_started_ms set edilir.
#
#    4.3 node_perceive (MCP-friendly)
#      - focus_window: scratch["focus_hint"] varsa (opsiyonel).
#      - screen_capture: zorunlu (alias: tooling.screen_capture).
#      - uia_tree: prefer_uia_tree True ve tool varsa.
#      - omniparser_v2_parse: scratch["need_vision"] ve omniparser_enabled ile kontrol edilir.
#
#      “Payload stratejisi” özellikle önemli:
#      - store_screenshot_b64=False ise:
#        screen_capture en azından hash döndürmeli ve omniparser server-side hash ile
#        görseli resolve edebilmelidir.
#      - store_screenshot_b64=True ise:
#        b64 doğrudan omniparser’a gönderilir (debug/replay için daha pahalı).
#
#      Çıktı:
#      - state.perception = PerceptionSnapshot
#      - status -> POLICY_CHECK
#
#    4.4 node_policy_check
#      - HIGH risk adımlarda require_approval_for_high_risk True ise:
#        approved değilken WAITING_APPROVAL’a geçirir.
#      - aksi halde status -> ACTING
#
#    4.5 node_act
#      - selector(state, tooling) -> ToolCall[] üretir.
#      - executor.call ile çalıştırır, ActionRecord ekler.
#      - post_action_capture True ise her aksiyondan sonra screen_capture hash alıp
#        effect_fingerprint’e yazar.
#      - runtime POLICY_DENY olursa ESCALATED.
#      - başarı -> status -> VERIFYING
#
#    4.6 node_verify
#      - verifier(state, tooling) -> (ok, details)
#      - ok ise plan.advance(); bitti -> DONE, değil -> PERCEIVING
#      - ok değil -> RECOVERING
#
#    4.7 node_recover
#      - RetryBudget + Step.max_retries ile guard.
#      - recovery(state, tooling) toolcall’larını çalıştırır.
#      - sonra status -> PERCEIVING
#
#    4.8 node_waiting_approval / node_finalize
#      - waiting_approval dış sistemin approved=True yapmasını bekler.
#      - finalize son telemetri snapshot’ı içindir.
#
# 5) Helper builder’lar
#    - build_click_from_bbox / build_type / build_hotkey
#      Selector/Recovery yazarken ToolCall üretimini standartlaştırır.

