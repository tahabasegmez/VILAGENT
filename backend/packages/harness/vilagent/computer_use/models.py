"""Typed contracts shared by computer-use planners, providers, and executors."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


class Size(BaseModel):
    width: int = Field(ge=1)
    height: int = Field(ge=1)


class Point(BaseModel):
    x: int
    y: int


class Rect(BaseModel):
    x: int
    y: int
    width: int = Field(ge=1)
    height: int = Field(ge=1)

    def contains(self, point: Point) -> bool:
        return self.x <= point.x < self.x + self.width and self.y <= point.y < self.y + self.height


class MonitorRef(BaseModel):
    monitor_id: str
    bounds: Rect
    primary: bool = False
    dpi_scale: float = Field(default=1.0, gt=0)


class WindowRef(BaseModel):
    window_id: str
    title: str = ""
    process_name: str | None = None
    process_id: int | None = Field(default=None, ge=0)
    bounds: Rect | None = None


class UIAQuery(BaseModel):
    automation_id: str | None = None
    name: str | None = None
    control_type: str | None = None
    process_id: int | None = Field(default=None, ge=0)
    window_title: str | None = None
    max_results: int = Field(default=20, ge=1, le=500)

    @model_validator(mode="after")
    def require_selector(self) -> UIAQuery:
        if not any((self.automation_id, self.name, self.control_type, self.process_id, self.window_title)):
            raise ValueError("UIA query requires at least one selector")
        return self


class UIAElementRef(BaseModel):
    element_id: str
    name: str = ""
    automation_id: str = ""
    control_type: str = ""
    process_id: int | None = None
    bounds: Rect | None = None
    enabled: bool | None = None
    visible: bool | None = None


class BlobRef(BaseModel):
    blob_id: str
    media_type: str
    size_bytes: int = Field(ge=0)
    sha256: str = Field(min_length=64, max_length=64)


class BrowserStateSummary(BaseModel):
    url: str | None = None
    title: str | None = None
    tab_id: str | None = None
    allowed_domain: bool | None = None


class Observation(BaseModel):
    """Small, checkpoint-friendly observation metadata.

    Screenshot bytes and full accessibility trees live in an ObservationStore
    and are referenced through BlobRef values.
    """

    observation_id: str
    session_id: str
    previous_observation_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    active_window: WindowRef | None = None
    screenshot_ref: BlobRef | None = None
    ui_tree_ref: BlobRef | None = None
    browser_state: BrowserStateSummary | None = None
    monitor: MonitorRef
    screen_size: Size
    diff_from_previous: float | None = Field(default=None, ge=0, le=1)
    redaction_applied: bool = False
    summary: str | None = None


class TargetStrategy(StrEnum):
    app = "app"
    browser = "browser"
    uia = "uia"
    vision = "vision"
    coordinate = "coordinate"


class TargetRef(BaseModel):
    strategy: TargetStrategy
    selector: dict[str, Any] = Field(default_factory=dict)
    bounds: Rect | None = None
    confidence: float = Field(ge=0, le=1)
    observation_id: str
    expected_window: WindowRef | None = None


class TargetQuery(BaseModel):
    """Provider-neutral request for resolving one target from an observation."""

    description: str = Field(min_length=1)
    selector_hints: dict[str, Any] = Field(default_factory=dict)
    allowed_strategies: list[TargetStrategy] = Field(
        default_factory=lambda: [
            TargetStrategy.app,
            TargetStrategy.browser,
            TargetStrategy.uia,
            TargetStrategy.vision,
        ]
    )
    minimum_confidence: float = Field(default=0.8, ge=0, le=1)

    @model_validator(mode="after")
    def require_strategy(self) -> TargetQuery:
        if not self.allowed_strategies:
            raise ValueError("Target query requires at least one allowed strategy")
        if len(set(self.allowed_strategies)) != len(self.allowed_strategies):
            raise ValueError("Target query allowed strategies must be unique")
        return self


class TargetResolutionOutcome(StrEnum):
    resolved = "resolved"
    not_found = "not_found"
    rejected = "rejected"
    error = "error"


class TargetResolutionAttempt(BaseModel):
    provider_name: str
    strategy: TargetStrategy
    outcome: TargetResolutionOutcome
    confidence: float | None = Field(default=None, ge=0, le=1)
    error_code: str | None = None


class TargetResolutionResult(BaseModel):
    target: TargetRef | None = None
    attempts: list[TargetResolutionAttempt] = Field(default_factory=list)


class ConditionOperator(StrEnum):
    equals = "equals"
    not_equals = "not_equals"
    exists = "exists"
    not_exists = "not_exists"
    contains = "contains"
    changed = "changed"


class Condition(BaseModel):
    kind: str
    operator: ConditionOperator = ConditionOperator.equals
    selector: dict[str, Any] = Field(default_factory=dict)
    expected: Any = None
    description: str | None = None


class RiskLevel(StrEnum):
    low = "low"
    medium = "medium"
    high = "high"
    critical = "critical"


class RiskAssessment(BaseModel):
    level: RiskLevel = RiskLevel.low
    reasons: list[str] = Field(default_factory=list)
    consequences: list[str] = Field(default_factory=list)


class ActionKind(StrEnum):
    click = "click"
    double_click = "double_click"
    right_click = "right_click"
    type_text = "type_text"
    hotkey = "hotkey"
    scroll = "scroll"
    drag = "drag"
    focus_window = "focus_window"
    launch_app = "launch_app"
    close_window = "close_window"
    browser_action = "browser_action"
    integration_action = "integration_action"


class ActionCommand(BaseModel):
    action_id: str
    session_id: str
    kind: ActionKind
    target: TargetRef | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    preconditions: list[Condition] = Field(default_factory=list)
    postconditions: list[Condition] = Field(default_factory=list)
    risk: RiskAssessment = Field(default_factory=RiskAssessment)
    auto_approve_risk_threshold: RiskLevel | None = None
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)
    timeout_seconds: float = Field(default=30, gt=0, le=600)

    @model_validator(mode="after")
    def validate_target_requirement(self) -> ActionCommand:
        # scroll may be targetless (scroll the focused page/window); the executor then
        # scrolls at the current cursor / viewport.
        target_optional = {
            ActionKind.type_text,
            ActionKind.hotkey,
            ActionKind.launch_app,
            ActionKind.browser_action,
            ActionKind.integration_action,
            ActionKind.scroll,
        }
        if self.kind not in target_optional and self.target is None:
            raise ValueError(f"Action kind '{self.kind}' requires a target")
        return self


def action_fingerprint(action: ActionCommand) -> str:
    """Return a stable digest used to detect action-payload mutation."""
    payload_data = action.model_dump(mode="json")
    # Preserve fingerprints created before the optional UI threshold field
    # existed. Explicit thresholds remain part of the immutable action payload.
    if payload_data.get("auto_approve_risk_threshold") is None:
        payload_data.pop("auto_approve_risk_threshold", None)
    payload = json.dumps(payload_data, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def action_intent_fingerprint(action: ActionCommand) -> str:
    """Return a stable digest for owner-scoped idempotent action retries."""
    intent = action.model_dump(mode="json", exclude={"action_id", "idempotency_key"})
    if intent.get("auto_approve_risk_threshold") is None:
        intent.pop("auto_approve_risk_threshold", None)
    payload = json.dumps(intent, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ActionOwner(BaseModel):
    thread_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    model_config = ConfigDict(frozen=True)


class ActionLifecycleStatus(StrEnum):
    pending = "pending"
    awaiting_approval = "awaiting_approval"
    approved = "approved"
    denied = "denied"
    executing = "executing"
    succeeded = "succeeded"
    failed = "failed"
    uncertain = "uncertain"
    cancelled = "cancelled"


class ActionLifecycleRecord(BaseModel):
    action: ActionCommand
    owner: ActionOwner
    status: ActionLifecycleStatus = ActionLifecycleStatus.pending
    action_fingerprint: str = Field(min_length=64, max_length=64)
    approval_id: str | None = None
    result: ActionResult | None = None
    error: StructuredError | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None

    @model_validator(mode="after")
    def validate_action_snapshot(self) -> ActionLifecycleRecord:
        if self.action_fingerprint != action_fingerprint(self.action):
            raise ValueError("action payload does not match its immutable fingerprint")
        terminal = {
            ActionLifecycleStatus.denied,
            ActionLifecycleStatus.succeeded,
            ActionLifecycleStatus.failed,
            ActionLifecycleStatus.uncertain,
            ActionLifecycleStatus.cancelled,
        }
        if (self.status in terminal) != (self.completed_at is not None):
            raise ValueError("terminal action lifecycle status requires completed_at")
        return self


class ApprovalLifecycleStatus(StrEnum):
    pending = "pending"
    approved = "approved"
    denied = "denied"
    expired = "expired"
    cancelled = "cancelled"


class ApprovalRecord(BaseModel):
    approval_id: str
    action_id: str
    session_id: str
    owner: ActionOwner
    action_fingerprint: str = Field(min_length=64, max_length=64)
    status: ApprovalLifecycleStatus = ApprovalLifecycleStatus.pending
    reasons: list[str] = Field(default_factory=list)
    consequences: list[str] = Field(default_factory=list)
    args: dict[str, Any] | None = None
    created_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime
    decided_at: datetime | None = None
    decided_by: str | None = None
    decision_reason: str | None = None

    @model_validator(mode="after")
    def validate_decision_state(self) -> ApprovalRecord:
        decided = self.status != ApprovalLifecycleStatus.pending
        if decided != (self.decided_at is not None):
            raise ValueError("non-pending approval status requires decided_at")
        if self.expires_at <= self.created_at:
            raise ValueError("approval expires_at must be after created_at")
        return self


class LifecycleEventType(StrEnum):
    action_submitted = "action_submitted"
    action_status_changed = "action_status_changed"
    approval_requested = "approval_requested"
    approval_decided = "approval_decided"


class ComputerUseLifecycleEvent(BaseModel):
    """Sanitized event suitable for live operator and agent status views."""

    sequence: int = Field(ge=1)
    event_type: LifecycleEventType
    owner: ActionOwner
    session_id: str
    action_id: str
    action_kind: ActionKind
    action_status: ActionLifecycleStatus | None = None
    approval_id: str | None = None
    approval_status: ApprovalLifecycleStatus | None = None
    error_code: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    model_config = ConfigDict(frozen=True)


class NativeActionResult(BaseModel):
    succeeded: bool
    details: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None


class AuditEventType(StrEnum):
    action_requested = "action_requested"
    action_blocked = "action_blocked"
    action_completed = "action_completed"
    emergency_stop_changed = "emergency_stop_changed"
    observation_blob_exported = "observation_blob_exported"
    observation_blob_export_blocked = "observation_blob_export_blocked"


class ComputerUseAuditEvent(BaseModel):
    event_id: str
    event_type: AuditEventType
    session_id: str
    action_id: str | None = None
    action_kind: ActionKind | None = None
    target_strategy: TargetStrategy | None = None
    argument_keys: list[str] = Field(default_factory=list)
    succeeded: bool | None = None
    emergency_stop_engaged: bool | None = None
    error_code: str | None = None
    reason: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class VerificationResult(BaseModel):
    succeeded: bool
    checked_conditions: int = Field(ge=0)
    failed_conditions: list[Condition] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)


class StructuredError(BaseModel):
    code: str
    message: str
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class ActionStatus(StrEnum):
    succeeded = "succeeded"
    failed = "failed"
    blocked = "blocked"
    cancelled = "cancelled"
    uncertain = "uncertain"


class ActionResult(BaseModel):
    action_id: str
    status: ActionStatus
    started_at: datetime
    completed_at: datetime
    before_observation_id: str
    after_observation_id: str | None = None
    verification: VerificationResult | None = None
    native_result: NativeActionResult | None = None
    error: StructuredError | None = None


class PolicyDecision(StrEnum):
    allow = "allow"
    deny = "deny"
    require_approval = "require_approval"


class PolicyVerdict(BaseModel):
    decision: PolicyDecision
    reasons: list[str] = Field(default_factory=list)
    policy_id: str = "vilagent.default.v1"


class ApprovalRequest(BaseModel):
    approval_id: str
    action: ActionCommand
    reasons: list[str] = Field(default_factory=list)
    consequences: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime | None = None


class ApprovalDecision(BaseModel):
    approved: bool
    approval_id: str
    decided_at: datetime = Field(default_factory=utc_now)
    reason: str | None = None


class DesktopSessionRef(BaseModel):
    session_id: str
    platform: str = "windows"
    monitor_id: str = "primary"
    created_at: datetime = Field(default_factory=utc_now)
    model_config = ConfigDict(frozen=True)


class DesktopSessionStatus(StrEnum):
    ready = "ready"
    stopped = "stopped"


class ProviderHealthStatus(StrEnum):
    healthy = "healthy"
    degraded = "degraded"
    stopped = "stopped"


class DesktopSafetyStatus(StrEnum):
    ready = "ready"
    locked = "locked"
    secure_desktop = "secure_desktop"
    unavailable = "unavailable"
    unknown = "unknown"


class DesktopSafetySnapshot(BaseModel):
    status: DesktopSafetyStatus
    checked_at: datetime = Field(default_factory=utc_now)
    reason_code: str | None = None

    @property
    def mutation_allowed(self) -> bool:
        return self.status == DesktopSafetyStatus.ready


class ComputerUseHostHealth(BaseModel):
    desktop_safety: DesktopSafetySnapshot
    emergency_stop_engaged: bool
    emergency_stop_reason: str | None = None
    emergency_stop_hotkey_registered: bool
    local_ipc_listening: bool = False
    ipc_heartbeat_status: str | None = None
    last_ipc_heartbeat_at: datetime | None = None
    mutation_allowed: bool
    checked_at: datetime = Field(default_factory=utc_now)


class EmergencyStopSnapshot(BaseModel):
    engaged: bool
    reason: str | None = None


class DesktopSessionSnapshot(BaseModel):
    session: DesktopSessionRef
    status: DesktopSessionStatus
    provider_name: str
    provider_health: ProviderHealthStatus
    latest_observation_id: str | None = None
    last_error: StructuredError | None = None
