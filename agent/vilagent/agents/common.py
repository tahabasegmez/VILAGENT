"""Plan and result models plus helpers shared by both agent approaches."""

from __future__ import annotations

import json
from collections.abc import Callable
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from vilagent.actions import ActionKind

# on_activity(role, event, thought): role is "lead", "vision", "uia" or "browser".
ActivityCallback = Callable[[str, str, str | None], None]


# The one-step approaches: FARA owns the whole task (a written brief, or the raw prompt).
BRIEF_STEP_ID = "brief"
DIRECT_STEP_ID = "direct"
# "autonomous" is what the brief step was called before; records written then still read.
SINGLE_STEP_IDS = frozenset({BRIEF_STEP_ID, DIRECT_STEP_ID, "autonomous"})
#: The three ways to run a task, named as the UI names them.
APPROACHES = ("plan", "brief", "direct")
SINGLE_STEP_APPROACHES = frozenset({"brief", DIRECT_STEP_ID})
#: What earlier versions stored in the state file and in run records.
_OLD_APPROACHES = {"plan_execute": "plan", "autonomous": "brief"}


def approach_name(name: str | None) -> str:
    """An approach under its current name; anything unknown falls back to the default."""
    name = _OLD_APPROACHES.get(str(name or ""), str(name or ""))
    return name if name in APPROACHES else APPROACHES[0]


class EnvironmentContext(StrEnum):
    browser = "browser"
    native = "native"


class StepStatus(StrEnum):
    pending = "pending"
    running = "running"
    completed = "completed"
    blocked = "blocked"
    failed = "failed"
    skipped = "skipped"
    denied = "denied"  # the operator declined it; the run ends


class RiskLevel(StrEnum):
    low = "low"
    medium = "medium"
    high = "high"
    critical = "critical"


class PlannedRisk(BaseModel):
    level: RiskLevel
    reasons: list[str] = Field(default_factory=list)
    consequences: list[str] = Field(default_factory=list)


class PlanStep(BaseModel):
    step_id: str
    instruction: str = Field(min_length=1)
    completion_criteria: str = Field(default="The instructed command has completed.", min_length=1)
    max_actions: int = Field(default=8, ge=1, le=16)
    environment: EnvironmentContext = EnvironmentContext.native
    requires_vision: bool = True
    action_kind: ActionKind | None = None
    target_description: str | None = None
    selector_hints: dict[str, Any] = Field(default_factory=dict)
    args: dict[str, Any] = Field(default_factory=dict)
    # Kept so an ActionGate can ask before risky steps.
    risk: PlannedRisk
    requires_verification: bool = True
    model_config = ConfigDict(extra="ignore")


class Plan(BaseModel):
    goal: str
    steps: list[PlanStep] = Field(default_factory=list)
    model_config = ConfigDict(extra="ignore")


class StepResult(BaseModel):
    step_id: str
    environment: EnvironmentContext
    requires_vision: bool
    status: StepStatus
    actions: int = 0
    error_code: str | None = None
    summary: str = ""
    # For FARA steps: how completion was established (FARA said so, a check by FARA or the
    # supervisor model, or no check).
    verified_by: Literal["fara_finish", "fara_verify", "model_verify", "none"] | None = None
    # For blocked steps: what the replanner gets to go on (error, FARA's last notes, page/window).
    evidence: dict[str, Any] = Field(default_factory=dict)
    # What the vision model said while doing this step; memory reads it back after a run.
    notes: list[str] = Field(default_factory=list)
    # What went sideways inside the step even though it ended well (repeats, nudges, a give-up).
    struggles: list[str] = Field(default_factory=list)

    @classmethod
    def of(
        cls,
        step: PlanStep,
        status: StepStatus,
        summary: str,
        *,
        error_code: str | None = None,
        actions: int = 0,
        verified_by: Literal["fara_finish", "fara_verify", "model_verify", "none"] | None = None,
        evidence: dict[str, Any] | None = None,
        notes: list[str] | None = None,
        struggles: list[str] | None = None,
    ) -> StepResult:
        return cls(
            step_id=step.step_id,
            environment=step.environment,
            requires_vision=step.requires_vision,
            status=status,
            actions=actions,
            error_code=error_code,
            summary=summary,
            verified_by=verified_by,
            evidence=evidence or {},
            notes=notes or [],
            struggles=struggles or [],
        )


class RunResult(BaseModel):
    status: StepStatus
    plan: Plan
    steps: list[StepResult]
    replan_count: int = 0
    request_count_estimate: int = 0
    summary: str = ""
    # Usage read from the models' own responses (no extra calls).
    planner_request_count: int = 0
    planner_total_tokens: int = 0
    vision_request_count: int = 0
    vision_total_tokens: int = 0


def message_text(message: Any) -> str:
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(item if isinstance(item, str) else item.get("text", "") for item in content if isinstance(item, (str, dict)))
    return str(content)


def response_tokens(message: Any) -> int:
    """Total tokens a LangChain chat response reports (0 when it reports none)."""
    usage = getattr(message, "usage_metadata", None)
    return int(usage.get("total_tokens") or 0) if isinstance(usage, dict) else 0


def extract_reasoning(message: Any) -> str | None:
    """A model's own thinking text from its response, if it returned any."""
    extra = getattr(message, "additional_kwargs", {}) or {}
    for key in ("reasoning_content", "reasoning"):
        if isinstance(extra.get(key), str) and extra[key].strip():
            return extra[key].strip()
    content = getattr(message, "content", None)
    if isinstance(content, list):
        parts = [block.get("thinking") or block.get("text") for block in content if isinstance(block, dict) and block.get("type") in ("thinking", "reasoning")]
        parts = [part for part in parts if isinstance(part, str) and part.strip()]
        if parts:
            return "\n".join(parts).strip()
    meta = getattr(message, "response_metadata", {}) or {}
    value = meta.get("reasoning_content") or meta.get("reasoning")
    return value.strip() if isinstance(value, str) and value.strip() else None


def extract_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    start, end = stripped.find("{"), stripped.rfind("}")
    if start >= 0 and end >= start:
        stripped = stripped[start : end + 1]
    data = json.loads(stripped)
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object")
    return data


def exception_summary(exc: BaseException) -> str:
    message = str(exc).strip()
    return f"{exc.__class__.__name__}: {message}" if message else exc.__class__.__name__
