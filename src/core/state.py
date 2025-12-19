"""
state.py

Industrial-grade shared state + core data contracts for a Windows GUI Agent built with LangGraph.

This file is intentionally framework-agnostic:
- No LangGraph imports here.
- No tool implementations here.
- Only: State schema, plan schema, perception/action records, policy/retry/telemetry contracts.

Other files:
- nodes.py: node functions that mutate AgentState (planner/perception/action/verify/recover)
- edges.py: conditional routing functions (state -> next node key)
- workflow.py: graph wiring + tool/mcp registration + runtime composition

Design goals:
- Deterministic, auditable state transitions
- Idempotent action replay support
- Policy + approval gating support
- Observability: events + spans + error taxonomy
- Extensibility: supports both local tools and MCP tools
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional


  
# Basic Types
  

Status = Literal[
    "INIT",
    "PLANNING",
    "PERCEIVING",
    "POLICY_CHECK",
    "ACTING",
    "VERIFYING",
    "RECOVERING",
    "WAITING_APPROVAL",
    "DONE",
    "FAILED",
    "ESCALATED",
]

Risk = Literal["LOW", "MEDIUM", "HIGH"]


  
# Error Codes (centralized)
  

class ErrorCode:
    """
    Centralized error codes for consistent error handling across the agent.
    
    Categories:
    - PLAN_*: Planning phase errors
    - STEP_*: Step execution errors  
    - TOOL_*: Tool-related errors
    - POLICY_*: Policy enforcement errors
    - RETRY_*: Retry/recovery errors
    - RECOVERY_*: Recovery phase errors
    """
    # Terminal states
    DONE = "DONE"
    ESCALATED = "ESCALATED"
    
    # Plan errors
    PLAN_INVALID = "PLAN_INVALID"
    PLAN_ERROR = "PLAN_ERROR"
    NO_PLAN = "NO_PLAN"
    
    # Step errors
    STEP_TIMEOUT = "STEP_TIMEOUT"
    
    # Tool errors
    TOOL_MISSING = "TOOL_MISSING"
    TOOL_NOT_FOUND = "TOOL_NOT_FOUND"
    
    # Policy errors
    POLICY_DENY = "POLICY_DENY"
    POLICY_DENY_ALLOWLIST = "POLICY_DENY: Tool not in allowlist"
    POLICY_DENY_DENYLIST = "POLICY_DENY: Tool in denylist"
    
    # MCP errors
    MCP_NOT_CONFIGURED = "MCP_NOT_CONFIGURED"
    
    # Retry/Recovery errors
    RETRY_EXHAUSTED = "RETRY_EXHAUSTED"
    RECOVERY_ERROR = "RECOVERY_ERROR"


def now_ms() -> int:
    return int(time.time() * 1000)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def stable_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fingerprint(obj: Any) -> str:
    """
    Lightweight fingerprint for dedupe / effect tracking.
    Replace with sha256 for cryptographic needs.
    """
    return str(abs(hash(stable_json(obj))))


  
# Telemetry (events + spans)
  

@dataclass
class Span:
    name: str
    start_ms: int
    end_ms: Optional[int] = None
    attrs: Dict[str, Any] = field(default_factory=dict)

    def close(self) -> None:
        self.end_ms = now_ms()


@dataclass
class Telemetry:
    """
    Production expectations:
    - events: structured logs (append-only)
    - spans: trace-like timing blocks (append-only)
    - last_error/error_code: quick access to most recent terminal/soft failure
    """
    events: List[Dict[str, Any]] = field(default_factory=list)
    spans: List[Span] = field(default_factory=list)
    last_error: Optional[str] = None
    error_code: Optional[str] = None

    def event(self, type_: str, **kwargs: Any) -> None:
        self.events.append({"ts_ms": now_ms(), "type": type_, **kwargs})

    def span(self, name: str, **attrs: Any) -> Span:
        sp = Span(name=name, start_ms=now_ms(), attrs=dict(attrs))
        self.spans.append(sp)
        return sp


  
# Plan Contracts
  

@dataclass
class Step:
    """
    A single execution step. Keep it explicit.
    - success_criteria should be machine-verifiable (UI text present, element visible, etc.)
    - tools_allowed provides local policy hints (router enforces final policy)
    - risk used for approval gating
    """
    id: str
    title: str
    intent: str
    success_criteria: List[str] = field(default_factory=list)
    tools_allowed: List[str] = field(default_factory=list)
    risk: Risk = "LOW"
    max_retries: int = 2
    timeout_ms: int = 90_000


@dataclass
class Plan:
    objective: str
    steps: List[Step] = field(default_factory=list)
    current_step_idx: int = 0
    plan_fingerprint: Optional[str] = None

    def finalize(self) -> "Plan":
        if self.plan_fingerprint is None:
            self.plan_fingerprint = fingerprint(
                {
                    "objective": self.objective,
                    "steps": [
                        {
                            "id": s.id,
                            "title": s.title,
                            "intent": s.intent,
                            "success_criteria": s.success_criteria,
                            "tools_allowed": s.tools_allowed,
                            "risk": s.risk,
                            "max_retries": s.max_retries,
                            "timeout_ms": s.timeout_ms,
                        }
                        for s in self.steps
                    ],
                }
            )
        return self

    @property
    def is_valid(self) -> bool:
        return bool(self.steps) and 0 <= self.current_step_idx < len(self.steps)

    @property
    def current(self) -> Step:
        return self.steps[self.current_step_idx]

    def advance(self) -> bool:
        """
        Returns True if plan finished.
        """
        self.current_step_idx += 1
        return self.current_step_idx >= len(self.steps)


  
# Perception / UI Snapshot
  

@dataclass
class PerceptionSnapshot:
    """
    Normalize all perception outputs here so nodes & planner are model-agnostic.
    - screenshot_hash is required for dedupe/replay.
    - screenshot_b64 should be stored only when needed (debug/replay); prefer hash-only in production.
    - uia_tree: normalized UI Automation tree (pywinauto/UIA) or a compact projection.
    - elements: OmniParser v2 normalized candidates:
        [{role, name, bbox, confidence, selector/uia_path, text, ...}]
    """
    screenshot_hash: Optional[str] = None
    screenshot_b64: Optional[str] = None
    focused_window: Optional[str] = None
    uia_tree: Optional[Dict[str, Any]] = None
    elements: List[Dict[str, Any]] = field(default_factory=list)
    ts_ms: int = field(default_factory=now_ms)


  
# Actions (audit + idempotency)
  

@dataclass
class ActionRecord:
    """
    Append-only action log.
    - idempotency_key: stable key to prevent double execution on retries/replay
    - effect_fingerprint: post-action observable fingerprint (e.g., screenshot_hash) for verification
    """
    action_id: str
    tool: str
    args: Dict[str, Any]
    idempotency_key: str
    started_ms: int
    ended_ms: Optional[int] = None
    ok: Optional[bool] = None
    error: Optional[str] = None
    effect_fingerprint: Optional[str] = None


  
# Policy + Approval Gate
  

@dataclass
class PolicyContext:
    """
    Policy is enforced by ToolRouter + by nodes (approval gating).
    - tool_allowlist: if non-empty, only these tools can be invoked
    - tool_denylist: always denied
    - require_approval_for_high_risk: if True, Step.risk == HIGH requires external approval
    """
    tool_allowlist: List[str] = field(default_factory=list)
    tool_denylist: List[str] = field(default_factory=list)
    require_approval_for_high_risk: bool = True
    last_decision: Optional[str] = None
    deny_reason: Optional[str] = None


  
# Retry & Budgets
  

@dataclass
class RetryBudget:
    """
    Controls:
    - total_budget: total recovery attempts for the whole run
    - step_retry_counts: per-step retry count (compared with Step.max_retries)
    """
    total_budget: int = 8
    used: int = 0
    step_retry_counts: Dict[str, int] = field(default_factory=dict)

    def can_retry_step(self, step_id: str, step_max: int) -> bool:
        if self.used >= self.total_budget:
            return False
        return self.step_retry_counts.get(step_id, 0) < step_max

    def consume(self, step_id: str) -> None:
        self.used += 1
        self.step_retry_counts[step_id] = self.step_retry_counts.get(step_id, 0) + 1


  
# Tool Contracts (used by nodes/router)
  

@dataclass
class ToolCall:
    """
    Framework-neutral tool invocation contract.
    workflow.py will map these names to:
      - LangGraph tools
      - local python wrappers (pywinauto/pyautogui)
      - MCP tools (vision_server.*, mouse_server.*, etc.)
    """
    name: str
    args: Dict[str, Any]
    idempotency_key: str
    timeout_ms: int = 30_000


@dataclass
class ToolResult:
    ok: bool
    data: Any = None
    error: Optional[str] = None


  
# Agent State (single source of truth)
  

@dataclass
class AgentState:
    """
    The only mutable object flowing through LangGraph.

    Required fields:
    - run_id: unique execution id (set at init)
    - goal: user objective

    Execution:
    - status: state machine status
    - plan: current plan with step pointer
    - perception: latest snapshot
    - actions: append-only audit log

    Control:
    - requires_human_approval/approved: external gate for high-risk steps
    - last_step_started_ms: timeout guard
    - done_reason: terminal reason

    scratch:
    - transient working memory for planners/verifiers/action selectors (safe to clear between runs)
    """
    run_id: str
    goal: str

    status: Status = "INIT"

    plan: Optional[Plan] = None
    perception: Optional[PerceptionSnapshot] = None
    actions: List[ActionRecord] = field(default_factory=list)

    policy: PolicyContext = field(default_factory=PolicyContext)
    retry: RetryBudget = field(default_factory=RetryBudget)
    telemetry: Telemetry = field(default_factory=Telemetry)

    requires_human_approval: bool = False
    approved: bool = False

    last_step_started_ms: Optional[int] = None
    done_reason: Optional[str] = None

    scratch: Dict[str, Any] = field(default_factory=dict)

    def ensure_run_id(self) -> None:
        if not self.run_id:
            self.run_id = new_id("run")

    def ensure_policy_defaults(self) -> None:
        if not self.policy.tool_allowlist and not self.policy.tool_denylist:
            # Minimal safe defaults; extend in workflow.py
            self.policy.tool_denylist = ["file_delete", "process_kill", "registry_write"]
            self.policy.require_approval_for_high_risk = True

    def set_terminal(self, status: Status, reason: str, code: Optional[str] = None) -> None:
        self.status = status
        self.done_reason = reason
        self.telemetry.last_error = reason if status in ("FAILED", "ESCALATED") else None
        self.telemetry.error_code = code
        self.telemetry.event("terminal", status=status, reason=reason, code=code)



# (state.py) — “Tek Gerçek Kaynak” veri sözleşmeleri

# Bu dosya, ajanın tüm çalışma zamanı durumunu ve çekirdek veri tiplerini tanımlar.
# LangGraph, MCP, UIA, OmniParser gibi teknolojilere özel importlar burada yoktur;
# amaç: state şemasını framework bağımsız tutmak.
#
# 1) Status ve Risk
#    - Status: ajanın finite-state machine (FSM) durumları.
#      Örn: INIT → PLANNING → PERCEIVING → POLICY_CHECK → ACTING → VERIFYING
#           → (başarısızsa) RECOVERING → tekrar PERCEIVING
#      Terminal durumlar: DONE / FAILED / ESCALATED
#    - Risk: adım risk seviyesi. HIGH ise insan onayı kapısı devreye girebilir.
#
# 2) Yardımcı fonksiyonlar
#    - now_ms(): milisaniye cinsinden zaman damgası.
#    - new_id(prefix): kısa/okunabilir benzersiz id üretir.
#    - stable_json(obj): deterministik JSON string (sort_keys + compact).
#    - fingerprint(obj): stable_json üzerinde Python hash’i ile “hafif” parmak izi.
#      Not: Kriptografik değil; güvenlik yerine dedupe/izleme içindir.
#
# 3) Telemetry (events + spans)
#    - Telemetry.events: append-only yapılandırılmış olaylar listesi.
#      nodes.py/workflow.py her önemli adımda event yazar.
#    - Telemetry.spans: süre ölçümü için Span kayıtları.
#      span(name) ile başlatılır, close() ile kapanır.
#    - last_error / error_code: son terminal hata/eskalasyon hakkında hızlı erişim.
#
# 4) Plan ve Step
#    - Step: tek bir plan adımını temsil eder.
#      Alanlar:
#        id/title/intent: adımı tanımlar.
#        success_criteria: doğrulama için makinece kontrol edilebilir kriterler.
#        tools_allowed: bu adımda kullanılmasına izin verilen tool isimleri için ipucu.
#                      (nihai enforcement workflow.py’deki policy/router’dadır.)
#        risk: onay kapısı için risk seviyesidir.
#        max_retries/timeout_ms: toparlanma ve zaman aşımı kuralları.
#    - Plan:
#        objective: hedef.
#        steps: Step listesi.
#        current_step_idx: aktif adım index’i.
#        plan_fingerprint: planın deterministik özeti.
#      Metotlar:
#        finalize(): fingerprint’i üretir.
#        is_valid: plan/indeks geçerli mi?
#        current: aktif step.
#        advance(): bir sonraki step’e geçer; bitti mi (True) döndürür.
#
# 5) PerceptionSnapshot
#    - Algılama çıktılarının normalize edildiği tek yer.
#      screenshot_hash: tekrar oynatma/dedupe için esas kimlik.
#      screenshot_b64: debug/replay için opsiyonel; prod’da hash tercih edilir.
#      uia_tree: UI Automation ağacı veya projeksiyonu.
#      elements: OmniParser’ın normalize öğe adayları.
#
# 6) ToolCall ve ToolResult
#    - ToolCall: framework-nötr araç çağrısı sözleşmesi.
#      name/args/idempotency_key/timeout_ms.
#      Önemli: idempotency_key; tekrar/çökmeye dayanıklılık için kritiktir.
#    - ToolResult: ok/data/error.
#
# 7) ActionRecord
#    - Append-only eylem günlüğü.
#    - idempotency_key: “aynı şeyi bir daha yapma” garantisi için.
#    - effect_fingerprint: eylem sonrası gözlemlenen etki (örn screenshot hash).
#
# 8) PolicyContext
#    - tool_allowlist: doluysa sadece listede olanlar çalışabilir.
#    - tool_denylist: her zaman engelli.
#    - require_approval_for_high_risk: HIGH risk step’lerde insan onayı.
#
# 9) RetryBudget
#    - total_budget: run boyunca toplam toparlanma hakkı.
#    - step_retry_counts: step bazlı sayım.
#    - can_retry_step / consume: nodes.node_recover içinde kullanılır.
#
# 10) AgentState (ajanın “tek mutable” objesi)
#     - LangGraph boyunca taşınan tek state budur.
#     - Önemli alanlar:
#       run_id, goal
#       status
#       plan/perception/actions
#       policy/retry/telemetry
#       requires_human_approval/approved
#       last_step_started_ms: timeout guard.
#       done_reason: terminal gerekçe.
#       scratch: geçici çalışma alanı (planner/verifier/selector için).
#     - ensure_run_id(): run_id boşsa üretir.
#     - ensure_policy_defaults(): default denylist ve onay kuralını kurar.
#     - set_terminal(): status’u terminale çeker ve telemetry’ye event basar.
#
# Bu dosya doğru tasarlandığı için nodes.py ve workflow.py daha “ince” kalır:
# - nodes.py sadece state’i değiştirir.
# - workflow.py sadece bağlar/çalıştırır.

