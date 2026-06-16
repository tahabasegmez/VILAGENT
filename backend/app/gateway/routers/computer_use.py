"""Gateway management API for the local VILAGENT Windows Agent Host."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urljoin

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from app.gateway.computer_use_deps import get_computer_use_remote_control, require_internal_request
from app.gateway.deps import get_config
from vilagent.runtime import serialize_channel_values
from vilagent.config.app_config import AppConfig, reload_app_config
from vilagent.computer_use.browser import BrowserHealth
from vilagent.computer_use.browser_actions import BrowserActionBuildError, build_browser_action
from vilagent.computer_use.models import (
    ActionCommand,
    ActionLifecycleRecord,
    ActionOwner,
    ApprovalRecord,
    BrowserStateSummary,
    Condition,
    ComputerUseAuditEvent,
    ComputerUseHostHealth,
    ComputerUseLifecycleEvent,
    DesktopSessionSnapshot,
    Observation,
    RiskLevel,
    TargetQuery,
    TargetRef,
    TargetResolutionResult,
    UIAElementRef,
    UIAQuery,
    WindowRef,
)
from vilagent.computer_use.plan_execute import JsonLLMPlanner, PlanExecuteComputerUseOrchestrator
from vilagent.computer_use.remote_host import (
    RemoteHostOperationError,
    RemoteHostUnavailableError,
    RemoteLifecycleRecordNotFoundError,
    RemoteSessionNotFoundError,
    RemoteWindowsHostControl,
)
from vilagent.computer_use.vision import UiTarsPyngrokTargetProvider, VisionProviderHealth

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/computer-use", tags=["computer-use"], dependencies=[Depends(require_internal_request)])
_PLAN_EXECUTE_ACTIVITY: dict[str, AgentActivityResponse] = {}

_STATE_FILE_PATH = Path(".vilagent_state.json")

def _read_vilagent_state() -> dict[str, Any]:
    if _STATE_FILE_PATH.exists():
        try:
            return json.loads(_STATE_FILE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}

def _write_vilagent_state(state: dict[str, Any]) -> None:
    _STATE_FILE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")

def _get_vilagent_state_value(key: str, default: Any) -> Any:
    return _read_vilagent_state().get(key, default)

def _set_vilagent_state_value(key: str, value: Any) -> None:
    state = _read_vilagent_state()
    state[key] = value
    _write_vilagent_state(state)


def _active_vision_provider(config: "AppConfig") -> str:
    """The operator-UI-selected vision provider (persisted state) is authoritative.

    Config is only the bootstrap default used before the UI has selected one, so
    health, activity, and runs all agree on a single source of truth.
    """
    return _get_vilagent_state_value("vision_provider", config.computer_use.vision_provider)


class CreateDesktopSessionRequest(BaseModel):
    session_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]+$")


class EmergencyStopRequest(BaseModel):
    reason: str = Field(default="Operator emergency stop", min_length=1, max_length=500)


class EmergencyStopStatus(BaseModel):
    engaged: bool
    reason: str | None = None


class ActionOwnerRequest(BaseModel):
    thread_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)

    def to_owner(self) -> ActionOwner:
        return ActionOwner.model_validate(self.model_dump())


class ApprovalDecisionRequest(BaseModel):
    owner: ActionOwnerRequest
    decided_by: str = Field(min_length=1, max_length=200)
    reason: str | None = Field(default=None, max_length=1000)
    model_config = ConfigDict(extra="forbid")


class ActionCancelRequest(BaseModel):
    owner: ActionOwnerRequest
    reason: str | None = Field(default=None, max_length=1000)
    model_config = ConfigDict(extra="forbid")


class ActionSubmissionRequest(BaseModel):
    owner: ActionOwnerRequest
    action: ActionCommand
    model_config = ConfigDict(extra="forbid")


class ActionExecutionRequest(BaseModel):
    owner: ActionOwnerRequest
    model_config = ConfigDict(extra="forbid")


class BrowserSessionCreateRequest(BaseModel):
    owner: ActionOwnerRequest
    url: str = Field(min_length=1, max_length=4096)
    model_config = ConfigDict(extra="forbid")


class BrowserContextRequest(BaseModel):
    owner: ActionOwnerRequest
    browser_session_id: str = Field(min_length=1, max_length=200)
    model_config = ConfigDict(extra="forbid")


class BrowserTargetResolutionRequest(BrowserContextRequest):
    query: TargetQuery


class BrowserActionSubmissionRequest(BaseModel):
    owner: ActionOwnerRequest
    session_id: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9_-]+$")
    target: TargetRef
    browser_state: BrowserStateSummary
    browser_action: str = Field(default="click", min_length=1, max_length=100)
    args: dict = Field(default_factory=dict)
    postconditions: list[Condition] = Field(default_factory=list)
    action_id: str | None = Field(default=None, min_length=1, max_length=200)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)
    timeout_seconds: float = Field(default=30, gt=0, le=600)
    model_config = ConfigDict(extra="forbid")


class ComputerUseModelStatus(BaseModel):
    provider: str
    model_config_name: str | None = None
    model_name: str | None = None
    configured: bool
    endpoint_configured: bool = False


class ComputerUseBudgetStatus(BaseModel):
    token_usage_enabled: bool
    planner_calls: int
    vision_calls: int
    total_actions: int
    duration_seconds: int


class ComputerUseVisionStatus(BaseModel):
    provider: str
    enabled: bool
    model_name: str
    endpoint_configured: bool
    endpoint_path: str


class ComputerUseStatusResponse(BaseModel):
    enabled: bool
    agent_mode: str
    architecture: str
    execution_mode: str = "hybrid"
    assistant_id: str = "computer_use_agent"
    prompt_profile: str
    platform: str
    runtime_mode: str
    text_model: ComputerUseModelStatus
    vision_model: ComputerUseVisionStatus
    browser_enabled: bool
    allowed_actions: list[str]
    budgets: ComputerUseBudgetStatus


class ComputerUseConfigCheck(BaseModel):
    key: str
    status: str
    message: str


class ComputerUseConfigValidationResponse(BaseModel):
    healthy: bool
    config_path: str | None = None
    env_path: str | None = None
    checks: list[ComputerUseConfigCheck]


class ComputerUseTaskRunRequest(BaseModel):
    thread_id: str = Field(min_length=1, max_length=200)
    run_id: str | None = Field(default=None, max_length=200)
    prompt: str = Field(min_length=1, max_length=12000)
    auto_approve_risk_threshold: RiskLevel
    model_config = ConfigDict(extra="forbid")


class ComputerUseTaskRunResponse(BaseModel):
    thread_id: str
    assistant_id: str = "computer_use_agent"
    output: dict[str, Any]
    error: str | None = None


class AgentActivityItem(BaseModel):
    agent_id: str
    role: str
    status: str
    task: str | None = None
    model_name: str | None = None
    request_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    tool_calls: list[str] = Field(default_factory=list)
    last_event: str | None = None
    current_thought: str | None = None
    last_updated_at: str | None = None


class PlanStepActivityItem(BaseModel):
    step_id: str
    instruction: str
    completion_criteria: str
    max_actions: int = 4
    status: str
    requires_vision: bool
    error_code: str | None = None
    summary: str | None = None


class AgentActivityResponse(BaseModel):
    thread_id: str
    run_id: str | None = None
    agents: list[AgentActivityItem]
    plan_steps: list[PlanStepActivityItem] = Field(default_factory=list)
    total_request_count: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_tokens: int = 0


class TextModelHealthResponse(BaseModel):
    provider_name: str = "vilagent-text-model"
    provider: str
    healthy: bool
    configured: bool
    endpoint_configured: bool
    probe_supported: bool
    model_config_name: str | None = None
    model_name: str | None = None
    endpoint_kind: str
    error_code: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class TextModelPresetInfo(BaseModel):
    provider: str
    model_config_name: str
    model_name: str
    api_key_configured: bool
    base_url: str | None = None


class TextModelSelectionResponse(BaseModel):
    provider: str
    selected_config_name: str | None = None
    selected_model_name: str | None = None
    options: list[str] = Field(default_factory=lambda: ["gemini", "glm", "ollama", "fara"])
    gemini: TextModelPresetInfo
    glm: TextModelPresetInfo
    ollama: TextModelPresetInfo
    fara: TextModelPresetInfo


class TextModelSelectionUpdateRequest(BaseModel):
    provider: str = Field(pattern="^(gemini|glm|ollama|fara)$")
    model_name: str | None = Field(default=None, max_length=200)
    api_key: str | None = Field(default=None, max_length=2000)
    base_url: str | None = Field(default=None, max_length=2000)
    model_config = ConfigDict(extra="forbid")


class VisionModelSelectionResponse(BaseModel):
    provider: str
    options: list[str] = Field(default_factory=lambda: ["fara", "ui_tars"])


class VisionModelSelectionUpdateRequest(BaseModel):
    provider: str = Field(pattern="^(fara|ui_tars)$")
    model_config = ConfigDict(extra="forbid")


class ExecutionModeSelectionResponse(BaseModel):
    execution_mode: str
    options: list[str] = Field(default_factory=lambda: ["hybrid", "vision_only"])


class ExecutionModeSelectionUpdateRequest(BaseModel):
    execution_mode: str = Field(pattern="^(hybrid|vision_only)$")
    model_config = ConfigDict(extra="forbid")


@router.post("/tasks/run", response_model=ComputerUseTaskRunResponse, summary="Run a VILAGENT computer-use task")
async def run_computer_use_task(
    body: ComputerUseTaskRunRequest,
    request: Request,
    config: AppConfig = Depends(get_config),
) -> ComputerUseTaskRunResponse:
    """Run the VILAGENT computer-use agent through a VILAGENT-native API.

    This endpoint intentionally hides the generic run payload from the
    Electron operator.  The public contract is computer-use specific.
    """
    return await _run_plan_execute_task(body, request, config)

async def _run_plan_execute_task(
    body: ComputerUseTaskRunRequest,
    request: Request,
    config: AppConfig,
) -> ComputerUseTaskRunResponse:
    remote = getattr(request.app.state, "computer_use_remote_control", None)
    if remote is None:
        raise HTTPException(status_code=503, detail="Computer-use host is unavailable")
    try:
        model_name = _resolve_plan_execute_model_name(config)
        run_id = body.run_id or f"plan-execute-{os.urandom(4).hex()}"
        owner = ActionOwner(thread_id=body.thread_id, run_id=run_id, agent_id="computer_use_plan_execute")
        _PLAN_EXECUTE_ACTIVITY[body.thread_id] = AgentActivityResponse(
            thread_id=body.thread_id,
            run_id=owner.run_id,
            agents=[
                AgentActivityItem(
                    agent_id="computer_use_plan_execute",
                    role="lead",
                    status="running",
                    task=body.prompt,
                    model_name=model_name,
                    last_event="planning",
                ),
                AgentActivityItem(
                    agent_id="vision_executor",
                    role="subagent",
                    status="idle",
                    task="Vision-first step execution.",
                    model_name=_selected_vision_model_name(config.computer_use)
                    if _selected_vision_enabled(config.computer_use)
                    else None,
                    last_event="idle",
                ),
                AgentActivityItem(agent_id="uia_executor", role="subagent", status="idle", task="Windows UIA execution.", last_event="idle"),
                AgentActivityItem(agent_id="browser_executor", role="subagent", status="idle", task="Browser DOM execution.", last_event="idle"),
            ],
        )
        # The operator UI persists these selections to .vilagent_state.json and they
        # are authoritative for the run. The orchestrator must use them rather than
        # re-reading config defaults (config is only the bootstrap value before the
        # UI has selected anything).
        strategy = _get_vilagent_state_value("execution_mode", "hybrid")
        vision_provider = _get_vilagent_state_value("vision_provider", config.computer_use.vision_provider)
        # Recovery supervisor (opt-in via UI toggle): when stuck, consult a stronger
        # vision+reasoning model. Source is operator-selected: the current planner
        # model, or a dedicated env-configured GLM-V (Zhipu) endpoint.
        vision_recovery = bool(_get_vilagent_state_value("vision_recovery", False))
        supervisor_source = _active_supervisor_source(config)
        supervisor_factory = _build_supervisor_factory(supervisor_source, model_name, config) if vision_recovery else None

        try:
            orchestrator = PlanExecuteComputerUseOrchestrator(
                planner=JsonLLMPlanner(model_name),
                remote=remote,
                auto_approve_risk_threshold=body.auto_approve_risk_threshold,
                execution_mode=strategy,
                vision_provider=vision_provider,
                vision_recovery=vision_recovery,
                supervisor_model_factory=supervisor_factory,
                max_replans=2,
                max_steps=min(config.computer_use.budgets.total_actions, 20),
            )
        except Exception as e:
            return ComputerUseTaskRunResponse(
                thread_id=body.thread_id,
                output="Planner initialization failed.",
                error=str(e),
                token_usage={"total": 0},
            )
        def on_activity_update(raw_agent_id: str, last_event: str, current_thought: str | None = None) -> None:
            activity = _PLAN_EXECUTE_ACTIVITY.get(body.thread_id)
            if activity:
                # Map internal orchestrator roles to API agent IDs
                agent_mapping = {
                    "lead": "computer_use_plan_execute",
                    "vision": "vision_executor",
                    "uia": "uia_executor",
                    "browser": "browser_executor",
                }
                agent_id = agent_mapping.get(raw_agent_id, raw_agent_id)
                
                for i, agent in enumerate(activity.agents):
                    if agent.agent_id == agent_id:
                        activity.agents[i].last_event = last_event
                        if current_thought is not None:
                            activity.agents[i].current_thought = current_thought
                        
                        if agent_id == "computer_use_plan_execute":
                            # Set others to idle
                            for j, other in enumerate(activity.agents):
                                if other.agent_id != agent_id:
                                    activity.agents[j].status = "idle"
                            activity.agents[i].status = "running"
                        else:
                            # Mark lead as idle, target subagent as running
                            activity.agents[0].status = "idle"
                            for j in range(1, len(activity.agents)):
                                activity.agents[j].status = "running" if activity.agents[j].agent_id == agent_id else "idle"

        def on_plan_update(plan: Any, results: list[Any], current_step_id: str | None) -> None:
            activity = _PLAN_EXECUTE_ACTIVITY.get(body.thread_id)
            if activity is None:
                return
            activity.plan_steps = _plan_step_activity(plan, results, current_step_id)

        import asyncio

        run_task = asyncio.create_task(
            orchestrator.run(
                body.prompt,
                owner=owner,
                context={
                    "architecture": "plan_execute",
                    "text_model": model_name,
                },
                on_activity_update=on_activity_update,
                on_plan_update=on_plan_update,
            )
        )

        # Do not cancel a run merely because the initiating HTTP client
        # disconnects. Approvals and progress use separate polling requests.
        result = await run_task
    except Exception as exc:
        logger.exception("Plan Execute orchestrator failed unexpectedly")
        return ComputerUseTaskRunResponse(
            thread_id=body.thread_id,
            output={"status": "failed", "error": str(exc)},
            error=str(exc),
        )
        
    _PLAN_EXECUTE_ACTIVITY[body.thread_id] = _activity_from_plan_execute_result(body.thread_id, owner.run_id, model_name, config, result)
    return ComputerUseTaskRunResponse(
        thread_id=body.thread_id,
        output={
            "architecture": "plan_execute",
            "status": result.status.value,
            "plan": result.plan.model_dump(mode="json"),
            "steps": [step.model_dump(mode="json") for step in result.steps],
            "replan_count": result.replan_count,
            "request_count_estimate": result.request_count_estimate,
            "messages": [
                {
                    "type": "ai",
                    "content": _format_plan_execute_response(result),
                }
            ],
        },
        error=None if result.status.value == "completed" else result.summary,
    )


@router.get(
    "/agents/activity",
    response_model=AgentActivityResponse,
    summary="Get live lead/subagent model and task activity for the operator UI",
)
async def get_agent_activity(thread_id: str, request: Request, run_id: str | None = None) -> AgentActivityResponse:
    live_activity = _PLAN_EXECUTE_ACTIVITY.get(thread_id)
    if live_activity is not None and (run_id is None or live_activity.run_id == run_id):
        return live_activity
    run_mgr = getattr(request.app.state, "run_manager", None)
    event_store = getattr(request.app.state, "run_event_store", None)
    latest_run = None
    if run_mgr is not None:
        runs = await run_mgr.list_by_thread(thread_id, user_id=None, limit=20)
        if run_id:
            latest_run = next((run for run in runs if run.run_id == run_id), None)
        elif runs:
            latest_run = runs[0]

    selected_run_id = run_id or (latest_run.run_id if latest_run is not None else None)
    messages: list[Any] = []
    if event_store is not None and selected_run_id is not None:
        try:
            rows = await event_store.list_messages_by_run(thread_id, selected_run_id, limit=200)
            messages = [row.get("content") for row in rows]
        except Exception:
            logger.exception("Failed to load VILAGENT activity messages for thread=%s run=%s", thread_id, selected_run_id)

    agents = _build_agent_activity_items(latest_run, messages)
    return AgentActivityResponse(
        thread_id=thread_id,
        run_id=selected_run_id,
        agents=agents,
        total_request_count=sum(item.request_count for item in agents),
        total_input_tokens=sum(item.input_tokens for item in agents),
        total_output_tokens=sum(item.output_tokens for item in agents),
        total_tokens=sum(item.total_tokens for item in agents),
    )


@router.get(
    "/execution-mode",
    response_model=ExecutionModeSelectionResponse,
    summary="Get active VILAGENT execution mode",
)
async def get_execution_mode_selection() -> ExecutionModeSelectionResponse:
    mode = _get_vilagent_state_value("execution_mode", "hybrid")
    return ExecutionModeSelectionResponse(execution_mode=mode)


@router.post(
    "/execution-mode",
    response_model=ExecutionModeSelectionResponse,
    summary="Switch active VILAGENT execution mode and persist it to state file",
)
async def update_execution_mode_selection(body: ExecutionModeSelectionUpdateRequest) -> ExecutionModeSelectionResponse:
    _set_vilagent_state_value("execution_mode", body.execution_mode)
    return ExecutionModeSelectionResponse(execution_mode=body.execution_mode)


class VisionRecoverySelectionResponse(BaseModel):
    enabled: bool


class VisionRecoverySelectionUpdateRequest(BaseModel):
    enabled: bool


@router.get(
    "/vision/recovery",
    response_model=VisionRecoverySelectionResponse,
    summary="Get whether the recovery supervisor (stuck escalation) is enabled",
)
async def get_vision_recovery_selection() -> VisionRecoverySelectionResponse:
    return VisionRecoverySelectionResponse(enabled=bool(_get_vilagent_state_value("vision_recovery", False)))


@router.post(
    "/vision/recovery",
    response_model=VisionRecoverySelectionResponse,
    summary="Toggle the recovery supervisor and persist it to the state file",
)
async def update_vision_recovery_selection(body: VisionRecoverySelectionUpdateRequest) -> VisionRecoverySelectionResponse:
    _set_vilagent_state_value("vision_recovery", body.enabled)
    return VisionRecoverySelectionResponse(enabled=body.enabled)


class SupervisorSourceResponse(BaseModel):
    source: str
    options: list[str] = Field(default_factory=lambda: ["planner", "api"])
    api_configured: bool
    api_model_name: str | None = None


class SupervisorSourceUpdateRequest(BaseModel):
    source: Literal["planner", "api"]


def _supervisor_source_response(config: AppConfig) -> SupervisorSourceResponse:
    sup = config.computer_use.supervisor_model
    return SupervisorSourceResponse(
        source=_active_supervisor_source(config),
        api_configured=sup.configured,
        api_model_name=sup.model_name if sup.configured else None,
    )


@router.get(
    "/vision/supervisor",
    response_model=SupervisorSourceResponse,
    summary="Get the recovery-supervisor model source (planner | api)",
)
async def get_supervisor_source(config: AppConfig = Depends(get_config)) -> SupervisorSourceResponse:
    return _supervisor_source_response(config)


@router.post(
    "/vision/supervisor",
    response_model=SupervisorSourceResponse,
    summary="Switch the recovery-supervisor model source and persist it",
)
async def update_supervisor_source(body: SupervisorSourceUpdateRequest, config: AppConfig = Depends(get_config)) -> SupervisorSourceResponse:
    _set_vilagent_state_value("supervisor_source", body.source)
    return _supervisor_source_response(config)


@router.get("/status", response_model=ComputerUseStatusResponse, summary="Get sanitized VILAGENT computer-use configuration status")
async def get_computer_use_status(config: AppConfig = Depends(get_config)) -> ComputerUseStatusResponse:
    cu = config.computer_use
    model_config_name = cu.text_model.model_config_name or cu.planner_model
    text_endpoint = _resolve_text_model_endpoint(config)
    return ComputerUseStatusResponse(
        enabled=cu.enabled,
        agent_mode=cu.agent_mode,
        architecture="plan_execute",
        execution_mode=_get_vilagent_state_value("execution_mode", "hybrid"),
        prompt_profile=cu.prompt_profile,
        platform=cu.platform,
        runtime_mode=cu.runtime_mode,
        text_model=ComputerUseModelStatus(
            provider=cu.text_model.provider,
            model_config_name=model_config_name,
            model_name=cu.text_model.model_name,
            configured=bool(model_config_name and config.get_model_config(model_config_name)),
            endpoint_configured=bool(
                text_endpoint
                if cu.text_model.provider == "pyngrok"
                else text_endpoint or _text_api_key_configured(config)
            ),
        ),
        vision_model=ComputerUseVisionStatus(
            provider=cu.vision_provider,
            enabled=_selected_vision_enabled(cu),
            model_name=_selected_vision_model_name(cu),
            endpoint_configured=_selected_vision_endpoint_configured(cu),
            endpoint_path=_selected_vision_endpoint_path(cu),
        ),
        browser_enabled=cu.browser.enabled,
        allowed_actions=[action.value for action in cu.host_safety.allowed_actions] if cu.host_safety.allowed_actions is not None else [],
        budgets=ComputerUseBudgetStatus(
            token_usage_enabled=config.token_usage.enabled,
            planner_calls=cu.budgets.planner_calls,
            vision_calls=cu.budgets.vision_calls,
            total_actions=cu.budgets.total_actions,
            duration_seconds=cu.budgets.duration_seconds,
        ),
    )


@router.get(
    "/config/validation",
    response_model=ComputerUseConfigValidationResponse,
    summary="Validate sanitized VILAGENT .env and config.yaml model wiring",
)
async def validate_computer_use_config(config: AppConfig = Depends(get_config)) -> ComputerUseConfigValidationResponse:
    checks = _validate_computer_use_config(config)
    return ComputerUseConfigValidationResponse(
        healthy=all(check.status != "error" for check in checks),
        config_path=_safe_config_path(),
        env_path=_safe_env_path(),
        checks=checks,
    )


@router.get(
    "/text-model/health",
    response_model=TextModelHealthResponse,
    summary="Probe the configured VILAGENT text LLM endpoint",
)
async def get_text_model_health(config: AppConfig = Depends(get_config)) -> TextModelHealthResponse:
    cu = config.computer_use
    model_config_name = cu.text_model.model_config_name or cu.planner_model
    model_config = config.get_model_config(model_config_name) if model_config_name else None
    model_name = cu.text_model.model_name or (str(model_config.model) if model_config else None)
    endpoint = _resolve_text_model_endpoint(config)
    configured = bool(model_config_name and model_config)
    api_key_configured = _text_api_key_configured(config)
    endpoint_kind = "pyngrok" if cu.text_model.provider == "pyngrok" else "api"

    if not configured:
        return TextModelHealthResponse(
            provider=cu.text_model.provider,
            healthy=False,
            configured=False,
            endpoint_configured=bool(endpoint),
            probe_supported=False,
            model_config_name=model_config_name,
            model_name=model_name,
            endpoint_kind=endpoint_kind,
            error_code="model_config_missing",
        )
    if cu.text_model.provider == "pyngrok" and not endpoint:
        return TextModelHealthResponse(
            provider=cu.text_model.provider,
            healthy=False,
            configured=True,
            endpoint_configured=False,
            probe_supported=True,
            model_config_name=model_config_name,
            model_name=model_name,
            endpoint_kind=endpoint_kind,
            error_code="pyngrok_endpoint_missing",
        )
    if not endpoint:
        return TextModelHealthResponse(
            provider=cu.text_model.provider,
            healthy=api_key_configured,
            configured=True,
            endpoint_configured=False,
            probe_supported=False,
            model_config_name=model_config_name,
            model_name=model_name,
            endpoint_kind=endpoint_kind,
            error_code=None if api_key_configured else "api_key_missing",
            details={"api_key_configured": api_key_configured},
        )

    health_url = _openai_compatible_models_url(endpoint)
    try:
        headers = {"Accept": "application/json"}
        if api_key_configured:
            headers["Authorization"] = f"Bearer {_text_api_key_value(config)}"
        timeout = min(config.computer_use.text_model.timeout_seconds, 10)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(health_url, headers=headers)
            response.raise_for_status()
            data = response.json()
    except Exception:
        return TextModelHealthResponse(
            provider=cu.text_model.provider,
            healthy=False,
            configured=True,
            endpoint_configured=True,
            probe_supported=True,
            model_config_name=model_config_name,
            model_name=model_name,
            endpoint_kind=endpoint_kind,
            error_code="text_model_health_unavailable",
        )

    model_count = len(data.get("data", [])) if isinstance(data, dict) and isinstance(data.get("data"), list) else None
    return TextModelHealthResponse(
        provider=cu.text_model.provider,
        healthy=True,
        configured=True,
        endpoint_configured=True,
        probe_supported=True,
        model_config_name=model_config_name,
        model_name=model_name,
        endpoint_kind=endpoint_kind,
        details={"models_url": _redact_url(health_url), "model_count": model_count},
    )


@router.get(
    "/text-model/selection",
    response_model=TextModelSelectionResponse,
    summary="Get selectable VILAGENT text model presets",
)
async def get_text_model_selection(config: AppConfig = Depends(get_config)) -> TextModelSelectionResponse:
    return _text_model_selection_response(config)


@router.post(
    "/text-model/selection",
    response_model=TextModelSelectionResponse,
    summary="Switch VILAGENT text model preset and persist it to state file",
)
async def update_text_model_selection(body: TextModelSelectionUpdateRequest) -> TextModelSelectionResponse:
    _set_vilagent_state_value("text_provider", body.provider)
    config = reload_app_config()
    return _text_model_selection_response(config)


@router.get(
    "/vision/selection",
    response_model=VisionModelSelectionResponse,
    summary="Get selectable VILAGENT vision model presets",
)
async def get_vision_model_selection(config: AppConfig = Depends(get_config)) -> VisionModelSelectionResponse:
    provider = _get_vilagent_state_value("vision_provider", config.computer_use.vision_provider)
    return VisionModelSelectionResponse(
        provider=provider,
        options=["fara", "ui_tars"]
    )


@router.post(
    "/vision/selection",
    response_model=VisionModelSelectionResponse,
    summary="Switch VILAGENT vision model preset and persist it to state file",
)
async def update_vision_model_selection(body: VisionModelSelectionUpdateRequest) -> VisionModelSelectionResponse:
    _set_vilagent_state_value("vision_provider", body.provider)
    config = reload_app_config()
    return VisionModelSelectionResponse(
        provider=body.provider,
        options=["fara", "ui_tars"]
    )


@router.get("/vision/health", response_model=VisionProviderHealth, summary="Probe vision endpoint health")
async def get_vision_health(config: AppConfig = Depends(get_config)) -> VisionProviderHealth:
    if _active_vision_provider(config) == "fara":
        fara = config.computer_use.vision_fara_model
        return VisionProviderHealth(
            provider_name="fara",
            enabled=fara.enabled,
            healthy=fara.enabled and bool(fara.base_url),
            endpoint_configured=bool(fara.base_url),
            model_name=fara.model_name,
            error_code=None if fara.enabled and fara.base_url else "fara_endpoint_missing",
            details={"endpoint_kind": "openai_compatible"},
        )
    return await UiTarsPyngrokTargetProvider(config.computer_use.vision_uitars_model).health()


@router.get("/health", response_model=ComputerUseHostHealth, summary="Get computer-use host health")
async def get_host_health(remote: RemoteWindowsHostControl = Depends(get_computer_use_remote_control)) -> ComputerUseHostHealth:
    try:
        return await remote.health()
    except RemoteHostUnavailableError as exc:
        raise HTTPException(status_code=503, detail="Computer use remote host health unavailable") from exc


@router.get("/sessions", response_model=list[DesktopSessionSnapshot], summary="List desktop sessions")
async def list_sessions(remote: RemoteWindowsHostControl = Depends(get_computer_use_remote_control)) -> list[DesktopSessionSnapshot]:
    try:
        return await remote.list_sessions()
    except RemoteHostUnavailableError as exc:
        raise HTTPException(status_code=503, detail="Computer use remote host sessions unavailable") from exc


@router.post("/sessions", response_model=DesktopSessionSnapshot, status_code=201, summary="Create desktop session")
async def create_session(
    request: CreateDesktopSessionRequest,
    remote: RemoteWindowsHostControl = Depends(get_computer_use_remote_control),
) -> DesktopSessionSnapshot:
    try:
        return await remote.create_session(request.session_id)
    except RemoteHostOperationError as exc:
        _raise_remote_operation(exc)
    except RemoteHostUnavailableError as exc:
        raise HTTPException(status_code=503, detail="Computer use remote host unavailable") from exc


@router.get("/sessions/{session_id}", response_model=DesktopSessionSnapshot, summary="Get desktop session")
async def get_session(session_id: str, remote: RemoteWindowsHostControl = Depends(get_computer_use_remote_control)) -> DesktopSessionSnapshot:
    try:
        return await remote.get_session(session_id)
    except RemoteSessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Desktop session not found") from exc
    except RemoteHostUnavailableError as exc:
        raise HTTPException(status_code=503, detail="Computer use remote host session unavailable") from exc


@router.post("/sessions/{session_id}/observe", response_model=Observation, summary="Capture desktop observation")
async def observe_session(
    session_id: str,
    thread_id: str | None = None,
    run_id: str | None = None,
    agent_id: str | None = None,
    browser_session_id: str | None = None,
    remote: RemoteWindowsHostControl = Depends(get_computer_use_remote_control),
) -> Observation:
    owner = _owner_for_optional_browser_context(thread_id, run_id, agent_id, browser_session_id)
    try:
        return await remote.observe_session(session_id, owner=owner, browser_session_id=browser_session_id)
    except RemoteHostOperationError as exc:
        _raise_remote_operation(exc)
    except RemoteHostUnavailableError as exc:
        raise HTTPException(status_code=503, detail="Desktop observation is unavailable") from exc


@router.post(
    "/sessions/{session_id}/browser/observe",
    response_model=Observation,
    summary="Capture desktop observation with owned browser state",
)
async def observe_session_with_browser(
    session_id: str,
    request: BrowserContextRequest,
    remote: RemoteWindowsHostControl = Depends(get_computer_use_remote_control),
) -> Observation:
    try:
        return await remote.observe_session(
            session_id,
            owner=request.owner.to_owner(),
            browser_session_id=request.browser_session_id,
        )
    except RemoteHostOperationError as exc:
        _raise_remote_operation(exc)
    except RemoteHostUnavailableError as exc:
        raise HTTPException(status_code=503, detail="Desktop observation is unavailable") from exc


@router.get("/sessions/{session_id}/observations/{observation_id}/blobs/{blob_id}", summary="Stream owner-scoped redacted observation blob")
async def stream_observation_blob(
    session_id: str, observation_id: str, blob_id: str, thread_id: str, run_id: str, agent_id: str,
    remote: RemoteWindowsHostControl = Depends(get_computer_use_remote_control),
) -> Response:
    try:
        ref, data = await remote.export_observation_blob(
            session_id, observation_id, blob_id, ActionOwner(thread_id=thread_id, run_id=run_id, agent_id=agent_id)
        )
        return Response(content=data, media_type=ref.media_type, headers={"Content-Length": str(ref.size_bytes), "ETag": f'"{ref.sha256}"'})
    except RemoteHostOperationError as exc:
        _raise_remote_operation(exc)
    except RemoteHostUnavailableError as exc:
        raise HTTPException(status_code=503, detail="Observation blob is unavailable") from exc


@router.post(
    "/sessions/{session_id}/targets/resolve",
    response_model=TargetResolutionResult,
    summary="Resolve target against latest observation",
)
async def resolve_target(
    session_id: str,
    query: TargetQuery,
    thread_id: str | None = None,
    run_id: str | None = None,
    agent_id: str | None = None,
    browser_session_id: str | None = None,
    remote: RemoteWindowsHostControl = Depends(get_computer_use_remote_control),
) -> TargetResolutionResult:
    owner = _owner_for_optional_browser_context(thread_id, run_id, agent_id, browser_session_id)
    try:
        return await remote.resolve_target(session_id, query, owner=owner, browser_session_id=browser_session_id)
    except RemoteHostOperationError as exc:
        _raise_remote_operation(exc)
    except RemoteHostUnavailableError as exc:
        raise HTTPException(status_code=503, detail="Target resolution is unavailable") from exc


@router.post(
    "/sessions/{session_id}/browser/targets/resolve",
    response_model=TargetResolutionResult,
    summary="Resolve target against owned browser-enriched observation",
)
async def resolve_browser_target(
    session_id: str,
    request: BrowserTargetResolutionRequest,
    remote: RemoteWindowsHostControl = Depends(get_computer_use_remote_control),
) -> TargetResolutionResult:
    try:
        return await remote.resolve_target(
            session_id,
            request.query,
            owner=request.owner.to_owner(),
            browser_session_id=request.browser_session_id,
        )
    except RemoteHostOperationError as exc:
        _raise_remote_operation(exc)
    except RemoteHostUnavailableError as exc:
        raise HTTPException(status_code=503, detail="Target resolution is unavailable") from exc


@router.post("/sessions/{session_id}/stop", response_model=DesktopSessionSnapshot, summary="Stop desktop session")
async def stop_session(session_id: str, remote: RemoteWindowsHostControl = Depends(get_computer_use_remote_control)) -> DesktopSessionSnapshot:
    try:
        return await remote.stop_session(session_id)
    except RemoteHostOperationError as exc:
        _raise_remote_operation(exc)
    except RemoteHostUnavailableError as exc:
        raise HTTPException(status_code=503, detail="Computer use remote host unavailable") from exc


@router.delete("/sessions/{session_id}", status_code=204, summary="Delete desktop session")
async def delete_session(session_id: str, remote: RemoteWindowsHostControl = Depends(get_computer_use_remote_control)) -> None:
    try:
        await remote.delete_session(session_id)
    except RemoteHostOperationError as exc:
        _raise_remote_operation(exc)
    except RemoteHostUnavailableError as exc:
        raise HTTPException(status_code=503, detail="Computer use remote host unavailable") from exc


@router.get("/uia/windows", response_model=list[WindowRef], summary="List Windows UI Automation windows")
async def list_uia_windows(remote: RemoteWindowsHostControl = Depends(get_computer_use_remote_control)) -> list[WindowRef]:
    try:
        return await remote.list_uia_windows()
    except RemoteHostUnavailableError as exc:
        raise HTTPException(status_code=503, detail="Windows UI Automation is unavailable") from exc


@router.post("/uia/find", response_model=list[UIAElementRef], summary="Find Windows UI Automation elements")
async def find_uia_elements(query: UIAQuery, remote: RemoteWindowsHostControl = Depends(get_computer_use_remote_control)) -> list[UIAElementRef]:
    try:
        return await remote.find_uia_elements(query)
    except RemoteHostUnavailableError as exc:
        raise HTTPException(status_code=503, detail="Windows UI Automation is unavailable") from exc


@router.get("/browser/health", response_model=BrowserHealth, summary="Get browser runtime health")
async def get_browser_health(remote: RemoteWindowsHostControl = Depends(get_computer_use_remote_control)) -> BrowserHealth:
    try:
        return await remote.browser_health()
    except RemoteHostOperationError as exc:
        _raise_remote_operation(exc)
    except RemoteHostUnavailableError as exc:
        raise HTTPException(status_code=503, detail="Browser runtime is unavailable") from exc


@router.get("/browser/sessions", response_model=list[str], summary="List owned browser sessions")
async def list_browser_sessions(
    thread_id: str,
    run_id: str,
    agent_id: str,
    remote: RemoteWindowsHostControl = Depends(get_computer_use_remote_control),
) -> list[str]:
    try:
        return await remote.list_browser_sessions(ActionOwner(thread_id=thread_id, run_id=run_id, agent_id=agent_id))
    except RemoteHostOperationError as exc:
        _raise_remote_operation(exc)
    except RemoteHostUnavailableError as exc:
        raise HTTPException(status_code=503, detail="Browser runtime is unavailable") from exc


@router.post("/browser/sessions", response_model=BrowserStateSummary, status_code=201, summary="Create owned browser session")
async def create_browser_session(
    request: BrowserSessionCreateRequest,
    remote: RemoteWindowsHostControl = Depends(get_computer_use_remote_control),
) -> BrowserStateSummary:
    try:
        return await remote.create_browser_session(request.url, request.owner.to_owner())
    except RemoteHostOperationError as exc:
        _raise_remote_operation(exc)
    except RemoteHostUnavailableError as exc:
        raise HTTPException(status_code=503, detail="Browser runtime is unavailable") from exc


@router.delete("/browser/sessions/{browser_session_id}", status_code=204, summary="Close owned browser session")
async def close_browser_session(
    browser_session_id: str,
    request: ActionOwnerRequest,
    remote: RemoteWindowsHostControl = Depends(get_computer_use_remote_control),
) -> None:
    try:
        await remote.close_browser_session(browser_session_id, request.to_owner())
    except RemoteHostOperationError as exc:
        _raise_remote_operation(exc)
    except RemoteHostUnavailableError as exc:
        raise HTTPException(status_code=503, detail="Browser runtime is unavailable") from exc


@router.get("/audit/{session_id}", response_model=list[ComputerUseAuditEvent], summary="List sanitized computer-use audit events")
async def list_audit_events(session_id: str, remote: RemoteWindowsHostControl = Depends(get_computer_use_remote_control)) -> list[ComputerUseAuditEvent]:
    try:
        return await remote.list_audit_events(session_id)
    except RemoteHostUnavailableError as exc:
        raise HTTPException(status_code=503, detail="Computer-use audit is unavailable") from exc


@router.get("/events", response_model=list[ComputerUseLifecycleEvent], summary="List sanitized lifecycle events")
async def list_lifecycle_events(
    thread_id: str,
    run_id: str,
    agent_id: str,
    session_id: str | None = None,
    after_sequence: int = 0,
    limit: int = 100,
    remote: RemoteWindowsHostControl = Depends(get_computer_use_remote_control),
) -> list[ComputerUseLifecycleEvent]:
    owner = ActionOwner(thread_id=thread_id, run_id=run_id, agent_id=agent_id)
    try:
        return await remote.list_lifecycle_events(
            owner=owner,
            session_id=session_id,
            after_sequence=after_sequence,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RemoteHostUnavailableError as exc:
        raise HTTPException(status_code=503, detail="Action lifecycle storage unavailable") from exc


@router.get("/events/wait", response_model=list[ComputerUseLifecycleEvent], summary="Wait for sanitized lifecycle events")
async def wait_for_lifecycle_events(
    thread_id: str,
    run_id: str,
    agent_id: str,
    session_id: str | None = None,
    after_sequence: int = 0,
    limit: int = 100,
    timeout_seconds: float = 20,
    remote: RemoteWindowsHostControl = Depends(get_computer_use_remote_control),
) -> list[ComputerUseLifecycleEvent]:
    owner = ActionOwner(thread_id=thread_id, run_id=run_id, agent_id=agent_id)
    try:
        return await remote.wait_lifecycle_events(
            owner=owner,
            session_id=session_id,
            after_sequence=after_sequence,
            limit=limit,
            timeout_seconds=timeout_seconds,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RemoteHostUnavailableError as exc:
        raise HTTPException(status_code=503, detail="Action lifecycle storage unavailable") from exc


@router.get("/events/stream", summary="Stream sanitized lifecycle events")
async def stream_lifecycle_events(
    request: Request,
    thread_id: str,
    run_id: str,
    agent_id: str,
    session_id: str | None = None,
    after_sequence: int = 0,
    heartbeat_seconds: float = 15,
    remote: RemoteWindowsHostControl = Depends(get_computer_use_remote_control),
) -> StreamingResponse:
    owner = ActionOwner(thread_id=thread_id, run_id=run_id, agent_id=agent_id)
    cursor = _resolve_event_cursor(request, after_sequence)
    if not 0 < heartbeat_seconds <= 30:
        raise HTTPException(status_code=422, detail="heartbeat_seconds must be between 0 and 30")
    return StreamingResponse(
        _lifecycle_sse(
            request,
            remote,
            owner=owner,
            session_id=session_id,
            after_sequence=cursor,
            heartbeat_seconds=heartbeat_seconds,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/approvals", response_model=list[ApprovalRecord], summary="List pending approval requests")
async def list_pending_approvals(
    thread_id: str,
    run_id: str,
    agent_id: str,
    remote: RemoteWindowsHostControl = Depends(get_computer_use_remote_control),
) -> list[ApprovalRecord]:
    try:
        approvals = []
        seen = set()
        for check_agent_id in {agent_id, "computer_use_plan_execute", "computer_use_react"}:
            owner = ActionOwner(thread_id=thread_id, run_id=run_id, agent_id=check_agent_id)
            agent_approvals = await remote.list_pending_approvals(owner)
            for app in agent_approvals:
                if app.approval_id not in seen:
                    seen.add(app.approval_id)
                    approvals.append(app)
        return approvals
    except RemoteHostUnavailableError as exc:
        raise HTTPException(status_code=503, detail="Action lifecycle storage unavailable") from exc


@router.get("/approvals/{approval_id}", response_model=ApprovalRecord, summary="Get approval request")
async def get_approval(
    approval_id: str,
    thread_id: str,
    run_id: str,
    agent_id: str,
    remote: RemoteWindowsHostControl = Depends(get_computer_use_remote_control),
) -> ApprovalRecord:
    owner = ActionOwner(thread_id=thread_id, run_id=run_id, agent_id=agent_id)
    try:
        return await remote.get_approval(approval_id, owner)
    except RemoteHostOperationError as exc:
        _raise_remote_operation(exc)
    except RemoteHostUnavailableError as exc:
        raise HTTPException(status_code=503, detail="Action lifecycle storage unavailable") from exc


@router.post("/approvals/{approval_id}/approve", response_model=ApprovalRecord, summary="Approve action")
async def approve_action(
    approval_id: str,
    request: ApprovalDecisionRequest,
    remote: RemoteWindowsHostControl = Depends(get_computer_use_remote_control),
) -> ApprovalRecord:
    return await _decide_remote_approval(remote, approval_id, request, approved=True)


@router.post("/approvals/{approval_id}/deny", response_model=ApprovalRecord, summary="Deny action")
async def deny_action(
    approval_id: str,
    request: ApprovalDecisionRequest,
    remote: RemoteWindowsHostControl = Depends(get_computer_use_remote_control),
) -> ApprovalRecord:
    return await _decide_remote_approval(remote, approval_id, request, approved=False)


@router.post("/actions/{action_id}/cancel", response_model=ActionLifecycleRecord, summary="Cancel queued action")
async def cancel_action(
    action_id: str,
    request: ActionCancelRequest,
    remote: RemoteWindowsHostControl = Depends(get_computer_use_remote_control),
) -> ActionLifecycleRecord:
    try:
        return await remote.cancel_action(action_id, request.owner.to_owner(), reason=request.reason)
    except RemoteHostOperationError as exc:
        _raise_remote_operation(exc)
    except RemoteHostUnavailableError as exc:
        raise HTTPException(status_code=503, detail="Action lifecycle storage unavailable") from exc


@router.get("/actions/{action_id}", response_model=ActionLifecycleRecord, summary="Get owned action lifecycle record")
async def get_action(
    action_id: str,
    thread_id: str,
    run_id: str,
    agent_id: str,
    remote: RemoteWindowsHostControl = Depends(get_computer_use_remote_control),
) -> ActionLifecycleRecord:
    owner = ActionOwner(thread_id=thread_id, run_id=run_id, agent_id=agent_id)
    try:
        return await remote.get_action(action_id, owner)
    except RemoteLifecycleRecordNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Action not found") from exc
    except RemoteHostUnavailableError as exc:
        raise HTTPException(status_code=503, detail="Action lifecycle storage unavailable") from exc


@router.post("/actions", response_model=ActionLifecycleRecord, status_code=201, summary="Submit typed action")
async def submit_action(
    request: ActionSubmissionRequest,
    remote: RemoteWindowsHostControl = Depends(get_computer_use_remote_control),
) -> ActionLifecycleRecord:
    try:
        return await remote.submit_action(request.action, request.owner.to_owner())
    except RemoteHostOperationError as exc:
        _raise_remote_operation(exc)
    except RemoteHostUnavailableError as exc:
        raise HTTPException(status_code=503, detail="Action lifecycle storage unavailable") from exc


@router.post("/browser/actions", response_model=ActionLifecycleRecord, status_code=201, summary="Submit browser DOM action")
async def submit_browser_action(
    request: BrowserActionSubmissionRequest,
    remote: RemoteWindowsHostControl = Depends(get_computer_use_remote_control),
) -> ActionLifecycleRecord:
    try:
        action = build_browser_action(
            session_id=request.session_id,
            target=request.target,
            browser_state=request.browser_state,
            browser_action=request.browser_action,
            action_id=request.action_id,
            args=request.args,
            postconditions=request.postconditions,
            idempotency_key=request.idempotency_key,
            timeout_seconds=request.timeout_seconds,
        )
    except BrowserActionBuildError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        return await remote.submit_action(action, request.owner.to_owner())
    except RemoteHostOperationError as exc:
        _raise_remote_operation(exc)
    except RemoteHostUnavailableError as exc:
        raise HTTPException(status_code=503, detail="Action lifecycle storage unavailable") from exc


@router.post("/actions/{action_id}/execute", response_model=ActionLifecycleRecord, summary="Execute approved stored action")
async def execute_action(
    action_id: str,
    request: ActionExecutionRequest,
    remote: RemoteWindowsHostControl = Depends(get_computer_use_remote_control),
) -> ActionLifecycleRecord:
    try:
        return await remote.execute_action(action_id, request.owner.to_owner())
    except RemoteHostOperationError as exc:
        _raise_remote_operation(exc)
    except RemoteHostUnavailableError as exc:
        raise HTTPException(status_code=503, detail="Action lifecycle storage unavailable") from exc


@router.get("/emergency-stop", response_model=EmergencyStopStatus, summary="Get emergency-stop status")
async def get_emergency_stop(remote: RemoteWindowsHostControl = Depends(get_computer_use_remote_control)) -> EmergencyStopStatus:
    try:
        snapshot = await remote.emergency_stop()
        return EmergencyStopStatus.model_validate(snapshot.model_dump())
    except (RemoteHostOperationError, RemoteHostUnavailableError) as exc:
        raise HTTPException(status_code=503, detail="Emergency stop is unavailable") from exc


@router.post("/emergency-stop/engage", response_model=EmergencyStopStatus, summary="Engage emergency stop")
async def engage_emergency_stop(
    request: EmergencyStopRequest,
    remote: RemoteWindowsHostControl = Depends(get_computer_use_remote_control),
) -> EmergencyStopStatus:
    try:
        snapshot = await remote.engage_emergency_stop(request.reason)
        return EmergencyStopStatus.model_validate(snapshot.model_dump())
    except (RemoteHostOperationError, RemoteHostUnavailableError) as exc:
        raise HTTPException(status_code=503, detail="Emergency stop is unavailable") from exc


@router.post("/emergency-stop/reset", response_model=EmergencyStopStatus, summary="Reset emergency stop")
async def reset_emergency_stop(
    request: EmergencyStopRequest,
    remote: RemoteWindowsHostControl = Depends(get_computer_use_remote_control),
) -> EmergencyStopStatus:
    try:
        snapshot = await remote.reset_emergency_stop(request.reason)
        return EmergencyStopStatus.model_validate(snapshot.model_dump())
    except (RemoteHostOperationError, RemoteHostUnavailableError) as exc:
        logger.exception("Failed to audit emergency-stop reset")
        raise HTTPException(status_code=503, detail="Emergency-stop reset audit failed; host remains stopped") from exc


async def _decide_remote_approval(
    remote: RemoteWindowsHostControl,
    approval_id: str,
    request: ApprovalDecisionRequest,
    *,
    approved: bool,
) -> ApprovalRecord:
    try:
        return await remote.decide_approval(
            approval_id,
            request.owner.to_owner(),
            approved=approved,
            decided_by=request.decided_by,
            reason=request.reason,
        )
    except RemoteHostOperationError as exc:
        _raise_remote_operation(exc)
    except RemoteHostUnavailableError as exc:
        raise HTTPException(status_code=503, detail="Action lifecycle storage unavailable") from exc


def _text_model_selection_response(config: AppConfig) -> TextModelSelectionResponse:
    selected = _selected_text_provider(config)
    selected_config_name = config.computer_use.text_model.model_config_name
    selected_model_name = config.computer_use.text_model.model_name
    if not selected_model_name and selected_config_name:
        if model_cfg := config.get_model_config(selected_config_name):
            selected_model_name = model_cfg.model
            
    return TextModelSelectionResponse(
        provider=selected,
        selected_config_name=selected_config_name,
        selected_model_name=selected_model_name,
        options=["gemini", "glm", "ollama", "fara"],
        gemini=TextModelPresetInfo(
            provider="gemini",
            model_config_name="vilagent-text-gemini",
            model_name=os.getenv("VILAGENT_GEMINI_MODEL_NAME", "gemini-2.5-flash"),
            api_key_configured=bool(os.getenv("VILAGENT_GEMINI_API_KEY") or os.getenv("GEMINI_API_KEY")),
        ),
        glm=TextModelPresetInfo(
            provider="glm",
            model_config_name="vilagent-text-glm",
            model_name=os.getenv("VILAGENT_GLM_MODEL_NAME", "glm-4-flash"),
            api_key_configured=bool(os.getenv("VILAGENT_GLM_API_KEY")),
            base_url=os.getenv("VILAGENT_GLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4/"),
        ),
        ollama=TextModelPresetInfo(
            provider="ollama",
            model_config_name="vilagent-text-ollama",
            model_name=os.getenv("VILAGENT_OLLAMA_MODEL_NAME", "llama3"),
            api_key_configured=True,
            base_url=os.getenv("VILAGENT_OLLAMA_BASE_URL", "http://localhost:11434/v1"),
        ),
        fara=TextModelPresetInfo(
            provider="fara",
            model_config_name="vilagent-text-fara",
            model_name=os.getenv("VILAGENT_FARA_MODEL_NAME", "microsoft/Fara-7B"),
            api_key_configured=True,
            base_url=os.getenv("VILAGENT_FARA_BASE_URL", "http://localhost:5000/v1"),
        ),
    )


def _active_supervisor_source(config: AppConfig) -> str:
    """Resolve the supervisor model source.

    When a dedicated GLM-V supervisor is configured in env, default to "api" so it
    is actually used (configuring it implies wanting it); otherwise default to the
    selected planner model. The operator UI can still override either way.
    """
    default = "api" if config.computer_use.supervisor_model.configured else "planner"
    return str(_get_vilagent_state_value("supervisor_source", default))


def _build_supervisor_factory(source: str, planner_model_name: str, config: AppConfig):
    """Build a 0-arg factory for the recovery-supervisor chat model.

    source == "api": a dedicated env-configured GLM-V (Zhipu) OpenAI-compatible
    endpoint, if configured. Otherwise (or "planner"): the selected planner model.
    """
    if source == "api":
        sup = config.computer_use.supervisor_model
        if sup.configured:
            from langchain_openai import ChatOpenAI

            return lambda: ChatOpenAI(
                model=sup.model_name,
                base_url=sup.base_url,
                api_key=sup.api_key,
                temperature=0,
                timeout=sup.timeout_seconds,
                # Fail fast on rate limits (e.g. GLM-V 429) instead of hammering the
                # endpoint with retries; the loop falls back to a generic nudge.
                max_retries=0,
            )
    from vilagent.models import create_chat_model

    return lambda: create_chat_model(planner_model_name, thinking_enabled=False, attach_tracing=False)


def _resolve_plan_execute_model_name(config: AppConfig) -> str:
    # Force read from state to avoid cache issues
    preset_provider = _get_vilagent_state_value("text_provider", None)
    config_name = f"vilagent-text-{preset_provider}" if preset_provider else None
    
    if config_name and not config.get_model_config(config_name):
        # Dynamically inject the preset if it was selected in the UI but missing from config.yaml
        preset_model = None
        if config_name == "vilagent-text-glm":
            from vilagent.config.model_config import ModelConfig
            preset_model = ModelConfig(
                name=config_name,
                use="langchain_openai.ChatOpenAI",
                model=os.getenv("VILAGENT_GLM_MODEL_NAME", "glm-4-flash"),
                base_url=os.getenv("VILAGENT_GLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4/"),
                api_key=os.getenv("VILAGENT_GLM_API_KEY", "")
            )
        elif config_name == "vilagent-text-ollama":
            from vilagent.config.model_config import ModelConfig
            preset_model = ModelConfig(
                name=config_name,
                use="langchain_community.chat_models.ChatOllama",
                model=os.getenv("VILAGENT_OLLAMA_MODEL_NAME", "llama3"),
                base_url=os.getenv("VILAGENT_OLLAMA_BASE_URL", "http://localhost:11434/v1")
            )
        elif config_name == "vilagent-text-fara":
            from vilagent.config.model_config import ModelConfig
            preset_model = ModelConfig(
                name=config_name,
                use="langchain_openai.ChatOpenAI",
                model=os.getenv("VILAGENT_FARA_MODEL_NAME", "microsoft/Fara-7B"),
                base_url=os.getenv("VILAGENT_FARA_BASE_URL", "http://localhost:5000/v1"),
                api_key="empty"
            )
        
        if preset_model:
            config.models.append(preset_model)

    if config_name and config.get_model_config(config_name):
        return config_name
    
    # Fallback to current config
    cu = config.computer_use
    if cu.text_model.model_config_name and config.get_model_config(cu.text_model.model_config_name):
        return cu.text_model.model_config_name
    if not config.models:
        raise HTTPException(status_code=400, detail="No text model is configured. Please select a model in the UI.")
    return config.models[0].name


def _format_plan_execute_response(result: Any) -> str:
    lines = [
        "PLAN:",
        *[f"{index + 1}. [{'vision' if step.requires_vision else 'browser' if step.environment == 'browser' else 'uia'}] {step.instruction}" for index, step in enumerate(result.plan.steps)],
        "",
        f"STATUS: {result.status.value}",
        f"REPLANS: {result.replan_count}",
        f"REQUESTS_ESTIMATE: {result.request_count_estimate}",
    ]
    if hasattr(result, 'summary') and result.summary:
        lines.append(f"SUMMARY: {result.summary}")
    for step in result.steps:
        lines.append(f"- {step.step_id}: {step.status.value} {step.summary}".strip())
    return "\n".join(lines)


def _activity_from_plan_execute_result(
    thread_id: str,
    run_id: str,
    model_name: str,
    config: AppConfig,
    result: Any,
) -> AgentActivityResponse:
    agents = [
        AgentActivityItem(
            agent_id="computer_use_plan_execute",
            role="lead",
            status="idle",
            task=result.plan.goal,
            model_name=model_name,
            request_count=result.request_count_estimate,
            last_event=f"{result.status.value}; replans={result.replan_count}",
        )
    ]
    for executor_id, label in (
        ("vision_executor", "vision"),
        ("uia_executor", "uia"),
        ("browser_executor", "browser"),
    ):
        if label == "vision":
            executor_steps = [step for step in result.steps if step.requires_vision]
        elif label == "browser":
            executor_steps = [step for step in result.steps if not step.requires_vision and step.environment == "browser"]
        else:
            executor_steps = [step for step in result.steps if not step.requires_vision and step.environment == "native"]
        total = len(executor_steps)
        failed = next((step for step in executor_steps if step.status.value in {"blocked", "failed"}), None)
        agents.append(
            AgentActivityItem(
                agent_id=executor_id,
                role="subagent",
                status="idle",
                task=f"{total} step(s) handled." if total else "No work in last plan.",
                model_name=_selected_vision_model_name(config.computer_use)
                if label == "vision" and _selected_vision_enabled(config.computer_use)
                else None,
                request_count=0,
                tool_calls=[step.action_status.value if step.action_status else step.status.value for step in executor_steps],
                last_event=(failed.summary if failed else executor_steps[-1].summary if executor_steps else "idle"),
            )
        )
    return AgentActivityResponse(
        thread_id=thread_id,
        run_id=run_id,
        agents=agents,
        plan_steps=_plan_step_activity(result.plan, result.steps, None),
        total_request_count=sum(agent.request_count for agent in agents),
        total_input_tokens=0,
        total_output_tokens=0,
        total_tokens=0,
    )


def _plan_step_activity(plan: Any, results: list[Any], current_step_id: str | None) -> list[PlanStepActivityItem]:
    result_by_step = {result.step_id: result for result in results}
    return [
        PlanStepActivityItem(
            step_id=step.step_id,
            instruction=step.instruction,
            completion_criteria=step.completion_criteria,
            max_actions=step.max_actions,
            status=(
                result_by_step[step.step_id].status.value
                if step.step_id in result_by_step
                else "running"
                if step.step_id == current_step_id
                else "pending"
            ),
            requires_vision=step.requires_vision,
            error_code=result_by_step[step.step_id].error_code if step.step_id in result_by_step else None,
            summary=result_by_step[step.step_id].summary if step.step_id in result_by_step else None,
        )
        for step in plan.steps
    ]


def _build_agent_activity_items(latest_run: Any | None, messages: list[Any]) -> list[AgentActivityItem]:
    lead = AgentActivityItem(
        agent_id="computer_use_agent",
        role="lead",
        status=_run_status(latest_run),
        task=_latest_user_task(messages),
        model_name=getattr(latest_run, "model_name", None) if latest_run is not None else None,
        last_updated_at=getattr(latest_run, "updated_at", None) if latest_run is not None else None,
    )
    subagents: dict[str, AgentActivityItem] = {}

    for message in messages:
        record = _message_record(message)
        if record is None:
            continue
        msg_type = str(record.get("type") or record.get("role") or "").lower()
        if msg_type not in {"ai", "assistant", "aimessage"} and not (record.get("tool_calls") or record.get("usage_metadata")):
            continue

        usage = record.get("usage_metadata") if isinstance(record.get("usage_metadata"), dict) else {}
        lead.request_count += 1
        lead.input_tokens += _int_value(usage.get("input_tokens"))
        lead.output_tokens += _int_value(usage.get("output_tokens"))
        lead.total_tokens += _int_value(usage.get("total_tokens"))
        lead.model_name = lead.model_name or _message_model_name(record)

        tool_names = _tool_names(record)
        lead.tool_calls.extend(tool_names)
        if tool_names:
            lead.last_event = f"tools: {', '.join(tool_names[:3])}"
        elif record.get("content"):
            lead.last_event = "assistant response"

        attribution = _token_attribution(record)
        for action in attribution.get("actions", []) if isinstance(attribution.get("actions"), list) else []:
            if not isinstance(action, dict) or action.get("kind") != "subagent":
                continue
            subagent_id = str(action.get("subagent_type") or "subagent")
            item = subagents.setdefault(
                subagent_id,
                AgentActivityItem(agent_id=subagent_id, role="subagent", status="idle"),
            )
            item.status = "running" if _run_status(latest_run) == "running" else "idle"
            item.task = str(action.get("description") or item.task or "")
            item.request_count += 1
            item.tool_calls.append("task")
            item.last_event = "subagent dispatched"

    if latest_run is not None:
        lead.request_count = max(lead.request_count, _int_value(getattr(latest_run, "llm_call_count", 0)))
        lead.input_tokens = max(lead.input_tokens, _int_value(getattr(latest_run, "total_input_tokens", 0)))
        lead.output_tokens = max(lead.output_tokens, _int_value(getattr(latest_run, "total_output_tokens", 0)))
        lead.total_tokens = max(lead.total_tokens, _int_value(getattr(latest_run, "lead_agent_tokens", 0)) or _int_value(getattr(latest_run, "total_tokens", 0)))

    if lead.status in {"pending", "running"} and lead.last_event is None:
        lead.last_event = "waiting for model/tool event"
    if lead.request_count == 0 and lead.status == "idle":
        lead.last_event = "idle"

    if not subagents:
        subagents["subagents"] = AgentActivityItem(
            agent_id="subagents",
            role="subagent",
            status="idle",
            task="No subagent work active for the current VILAGENT run.",
            last_event="idle",
        )
    return [lead, *subagents.values()]


def _run_status(run: Any | None) -> str:
    if run is None:
        return "idle"
    status = getattr(run, "status", None)
    value = getattr(status, "value", status)
    if value in {"pending", "running"}:
        return str(value)
    return "idle" if value in {"success", "completed", "error", "cancelled"} else str(value or "idle")


def _message_record(message: Any) -> dict[str, Any] | None:
    if isinstance(message, dict):
        return message
    if hasattr(message, "model_dump"):
        dumped = message.model_dump(mode="json")
        return dumped if isinstance(dumped, dict) else None
    return None


def _latest_user_task(messages: list[Any]) -> str | None:
    for message in reversed(messages):
        record = _message_record(message)
        if record is None:
            continue
        role = str(record.get("role") or record.get("type") or "").lower()
        if role in {"human", "user", "humanmessage"}:
            content = record.get("content")
            return content if isinstance(content, str) else None
    return None


def _message_model_name(record: dict[str, Any]) -> str | None:
    response_metadata = record.get("response_metadata")
    if isinstance(response_metadata, dict):
        value = response_metadata.get("model_name") or response_metadata.get("model")
        if value:
            return str(value)
    return None


def _tool_names(record: dict[str, Any]) -> list[str]:
    tool_calls = record.get("tool_calls")
    if not isinstance(tool_calls, list):
        return []
    names: list[str] = []
    for call in tool_calls:
        if isinstance(call, dict) and call.get("name"):
            names.append(str(call["name"]))
    return names


def _token_attribution(record: dict[str, Any]) -> dict[str, Any]:
    additional_kwargs = record.get("additional_kwargs")
    if isinstance(additional_kwargs, dict):
        value = additional_kwargs.get("token_usage_attribution")
        if isinstance(value, dict):
            return value
    return {}


def _int_value(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _selected_text_provider(config: AppConfig) -> str:
    preset = _get_vilagent_state_value("text_provider", None)
    if preset in {"gemini", "glm", "ollama", "fara"}:
        return preset
    selected = config.computer_use.text_model.model_config_name or ""
    if "glm" in selected:
        return "glm"
    elif "ollama" in selected:
        return "ollama"
    elif "fara" in selected:
        return "fara"
    return "gemini"


def _validate_computer_use_config(config: AppConfig) -> list[ComputerUseConfigCheck]:
    cu = config.computer_use
    model_config_name = cu.text_model.model_config_name or cu.planner_model
    model_config = config.get_model_config(model_config_name) if model_config_name else None
    endpoint = _resolve_text_model_endpoint(config)
    checks = [
        ComputerUseConfigCheck(
            key="computer_use.enabled",
            status="ok" if cu.enabled else "warn",
            message="Computer-use is enabled." if cu.enabled else "Computer-use is disabled; enable it when running VILAGENT.",
        ),
        ComputerUseConfigCheck(
            key="computer_use.text_model.provider",
            status="ok" if cu.text_model.provider in {"api", "pyngrok"} else "error",
            message=f"Text planner provider is {cu.text_model.provider}.",
        ),
        ComputerUseConfigCheck(
            key="computer_use.text_model.model_config_name",
            status="ok" if model_config else "error",
            message=(
                f"Model config '{model_config_name}' is present."
                if model_config
                else "Set computer_use.text_model.model_config_name to a top-level models entry."
            ),
        ),
        ComputerUseConfigCheck(
            key="computer_use.text_model.endpoint",
            status="ok" if _text_endpoint_status_ok(config, endpoint) else "error",
            message=_text_endpoint_message(config, endpoint),
        ),
        ComputerUseConfigCheck(
            key="computer_use.budgets",
            status="ok",
            message=(
                f"Budgets: planner={cu.budgets.planner_calls}, vision={cu.budgets.vision_calls}, "
                f"actions={cu.budgets.total_actions}, duration={cu.budgets.duration_seconds}s."
            ),
        ),
        ComputerUseConfigCheck(
            key="token_usage.enabled",
            status="ok" if config.token_usage.enabled else "warn",
            message="Token usage reporting is enabled." if config.token_usage.enabled else "Token usage reporting is disabled.",
        ),
        ComputerUseConfigCheck(
            key="computer_use.vision_provider.endpoint",
            status="ok" if (not _selected_vision_enabled(cu) or _selected_vision_endpoint_configured(cu)) else "error",
            message=(
                f"{cu.vision_provider} endpoint is configured."
                if _selected_vision_endpoint_configured(cu)
                else f"{cu.vision_provider} is disabled."
                if not _selected_vision_enabled(cu)
                else "Set the selected vision provider endpoint in .env."
            ),
        ),
    ]
    return checks


def _selected_vision_enabled(cu) -> bool:
    if _get_vilagent_state_value("vision_provider", cu.vision_provider) == "fara":
        return cu.vision_fara_model.enabled
    return cu.vision_uitars_model.enabled


def _selected_vision_model_name(cu) -> str:
    if _get_vilagent_state_value("vision_provider", cu.vision_provider) == "fara":
        return cu.vision_fara_model.model_name
    return cu.vision_uitars_model.model_name


def _selected_vision_endpoint_configured(cu) -> bool:
    if _get_vilagent_state_value("vision_provider", cu.vision_provider) == "fara":
        return bool(cu.vision_fara_model.base_url)
    return bool(cu.vision_uitars_model.pyngrok_url)


def _selected_vision_endpoint_path(cu) -> str:
    if _get_vilagent_state_value("vision_provider", cu.vision_provider) == "fara":
        return "/chat/completions"
    return cu.vision_uitars_model.endpoint_path


def _resolve_text_model_endpoint(config: AppConfig) -> str | None:
    cu = config.computer_use
    if cu.text_model.provider == "pyngrok":
        return cu.text_model.pyngrok_url
    if cu.text_model.api_base_url:
        return cu.text_model.api_base_url
    model_config_name = cu.text_model.model_config_name or cu.planner_model
    model_config = config.get_model_config(model_config_name) if model_config_name else None
    for field_name in ("base_url", "api_base", "api_base_url", "openai_api_base"):
        value = _model_config_extra(model_config, field_name)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _text_endpoint_status_ok(config: AppConfig, endpoint: str | None) -> bool:
    if config.computer_use.text_model.provider == "pyngrok":
        return bool(endpoint)
    return bool(endpoint or _text_api_key_configured(config))


def _text_endpoint_message(config: AppConfig, endpoint: str | None) -> str:
    provider = config.computer_use.text_model.provider
    if provider == "pyngrok":
        return "pyngrok OpenAI-compatible base URL is configured." if endpoint else "Set VILAGENT_TEXT_PYNGROK_BASE_URL with the /v1 suffix."
    if endpoint:
        return "API base URL is configured for an OpenAI-compatible provider."
    if _text_api_key_configured(config):
        return "API key is configured; direct health probe may not be supported by this provider."
    return "Set VILAGENT_TEXT_API_KEY or configure an OpenAI-compatible API base URL."


def _text_api_key_configured(config: AppConfig) -> bool:
    return bool(_text_api_key_value(config))


def _text_api_key_value(config: AppConfig) -> str | None:
    cu = config.computer_use
    if cu.text_model.api_key:
        return cu.text_model.api_key
    model_config_name = cu.text_model.model_config_name or cu.planner_model
    model_config = config.get_model_config(model_config_name) if model_config_name else None
    for field_name in ("api_key", "google_api_key", "gemini_api_key", "openai_api_key"):
        value = _model_config_extra(model_config, field_name)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _model_config_extra(model_config: Any, field_name: str) -> Any:
    if model_config is None:
        return None
    value = getattr(model_config, field_name, None)
    if value is not None:
        return value
    extras = getattr(model_config, "model_extra", None)
    if isinstance(extras, dict):
        return extras.get(field_name)
    return None


def _openai_compatible_models_url(base_url: str) -> str:
    base = base_url.rstrip("/") + "/"
    if base.rstrip("/").endswith("/v1"):
        return urljoin(base, "models")
    return urljoin(base, "v1/models")


def _redact_url(url: str) -> str:
    return url.split("?", 1)[0]


def _safe_config_path() -> str | None:
    try:
        return str(AppConfig.resolve_config_path())
    except Exception:
        return None


def _safe_env_path() -> str | None:
    for candidate in (os.getenv("VILAGENT_ENV_PATH"), ".env"):
        if candidate and os.path.exists(candidate):
            return os.path.abspath(candidate)
    return None


def _owner_for_optional_browser_context(
    thread_id: str | None,
    run_id: str | None,
    agent_id: str | None,
    browser_session_id: str | None,
) -> ActionOwner | None:
    if browser_session_id is not None and not all((thread_id, run_id, agent_id)):
        raise HTTPException(status_code=422, detail="browser_session_id requires thread_id, run_id, and agent_id")
    if browser_session_id is None and any((thread_id, run_id, agent_id)):
        raise HTTPException(status_code=422, detail="thread_id, run_id, and agent_id are only accepted with browser_session_id")
    if browser_session_id is None:
        return None
    return ActionOwner(thread_id=thread_id, run_id=run_id, agent_id=agent_id)


def _raise_remote_operation(exc: RemoteHostOperationError) -> None:
    status_and_detail = {
        "approval_not_found": (404, "Approval request not found"),
        "action_not_found": (404, "Action not found"),
        "session_not_found": (404, "Desktop session not found"),
        "approval_conflict": (409, "Approval request cannot be decided"),
        "action_conflict": (409, "Action submission conflicts with stored state"),
        "session_owner_conflict": (409, "Desktop session is already bound to another action owner"),
        "invalid_transition": (409, "Action transition is invalid"),
        "session_conflict": (409, "Desktop session already exists"),
        "session_stopped": (409, "Desktop session is stopped"),
        "observation_missing": (409, "Desktop session has no observation"),
        "lifecycle_unavailable": (503, "Action lifecycle storage unavailable"),
        "observation_unavailable": (503, "Desktop observation is unavailable"),
        "target_unavailable": (503, "Target resolution is unavailable"),
        "blob_not_found": (404, "Observation blob not found"),
        "blob_unavailable": (503, "Observation blob is unavailable"),
        "blob_integrity_failed": (503, "Observation blob is unavailable"),
        "browser_policy_denied": (403, "Browser URL is not allowed"),
        "browser_session_not_found": (404, "Browser session not found"),
        "browser_unavailable": (503, "Browser runtime is unavailable"),
    }
    status_code, detail = status_and_detail.get(exc.code, (503, "Computer use remote host operation unavailable"))
    raise HTTPException(status_code=status_code, detail=detail) from exc


def _resolve_event_cursor(request: Request, after_sequence: int) -> int:
    if after_sequence < 0:
        raise HTTPException(status_code=422, detail="after_sequence must not be negative")
    last_event_id = request.headers.get("Last-Event-ID")
    if last_event_id is None:
        return after_sequence
    try:
        header_sequence = int(last_event_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Last-Event-ID must be a non-negative integer") from exc
    if header_sequence < 0:
        raise HTTPException(status_code=422, detail="Last-Event-ID must be a non-negative integer")
    return max(after_sequence, header_sequence)


async def _lifecycle_sse(
    request: Request,
    remote: RemoteWindowsHostControl,
    *,
    owner: ActionOwner,
    session_id: str | None,
    after_sequence: int,
    heartbeat_seconds: float,
) -> AsyncIterator[str]:
    cursor = after_sequence
    while not await request.is_disconnected():
        events = await remote.wait_lifecycle_events(
            owner=owner,
            session_id=session_id,
            after_sequence=cursor,
            limit=100,
            timeout_seconds=heartbeat_seconds,
        )
        if not events:
            yield ": heartbeat\n\n"
            continue
        for event in events:
            if await request.is_disconnected():
                return
            cursor = event.sequence
            yield _format_lifecycle_sse(
                "computer-use.lifecycle",
                event.model_dump(mode="json"),
                event_id=str(event.sequence),
            )


def _format_lifecycle_sse(event: str, data: dict, *, event_id: str) -> str:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {payload}\nid: {event_id}\n\n"


from fastapi.responses import PlainTextResponse

@router.get('/logs/vilagent', response_class=PlainTextResponse, summary='Get vilagent logs')
async def get_vilagent_logs() -> str:
    log_path = 'd:/code/my-projects/vilagent-main/logs/vilagent.log'
    try:
        with open(log_path, 'r', encoding='utf-8') as f:
            return f.read()
    except Exception as e:
        return f'Failed to read logs: {e}'

