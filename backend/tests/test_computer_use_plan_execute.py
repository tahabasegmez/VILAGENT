"""Tests for the plan-and-execute computer-use orchestrator (current API).

The plan step model uses ``requires_vision`` (not the removed ``ExecutorRole``)
and the orchestrator gates the plan through an integration_action approval before
executing steps; these tests account for both.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from vilagent.computer_use.models import (
    ActionCommand,
    ActionKind,
    ActionLifecycleRecord,
    ActionLifecycleStatus,
    ActionOwner,
    BlobRef,
    DesktopSessionRef,
    DesktopSessionSnapshot,
    MonitorRef,
    Observation,
    Rect,
    RiskLevel,
    Size,
    TargetRef,
    TargetResolutionResult,
    TargetStrategy,
    action_fingerprint,
)
from vilagent.computer_use.plan_execute import (
    ComputerUsePlan,
    ComputerUsePlanStep,
    PlanExecuteComputerUseOrchestrator,
    PlannedRiskAssessment,
    StepStatus,
    _PLANNER_SYSTEM_PROMPT,
)

_RISK = PlannedRiskAssessment(level=RiskLevel.low, reasons=["test"], consequences=["test"])


def _step(step_id: str, instruction: str, *, requires_vision: bool, action_kind: ActionKind | None = None, **kwargs) -> ComputerUsePlanStep:
    return ComputerUsePlanStep(
        step_id=step_id,
        instruction=instruction,
        requires_vision=requires_vision,
        action_kind=action_kind,
        risk=_RISK,
        **kwargs,
    )


class FakePlanner:
    def __init__(self, plan: ComputerUsePlan, replan: ComputerUsePlan | None = None):
        self._plan = plan
        self._replan = replan
        self.plan_calls = 0
        self.replan_calls = 0

    async def plan(self, prompt, *, context):
        self.plan_calls += 1
        return self._plan

    async def replan(self, prompt, *, plan, completed_steps, blocked_step, context):
        self.replan_calls += 1
        return self._replan or self._plan


class FakeRemote:
    """Minimal remote host: every submitted action is auto-approved and succeeds."""

    def __init__(self, *, target_available=True):
        self.target_available = target_available
        self.resolve_queries = []
        self.submitted_actions: list[ActionCommand] = []

    async def get_session(self, session_id):
        return self._snapshot(session_id)

    async def create_session(self, session_id=None):
        return self._snapshot(session_id or "session-1")

    async def resolve_target(self, session_id, query, *, owner=None, browser_session_id=None):
        self.resolve_queries.append(query)
        if not self.target_available:
            self.target_available = True
            return TargetResolutionResult(target=None, attempts=[])
        return TargetResolutionResult(
            target=TargetRef(strategy=TargetStrategy.uia, selector={"automation_id": "save"}, confidence=0.9, observation_id="obs-1"),
            attempts=[],
        )

    async def submit_action(self, action, owner):
        self.submitted_actions.append(action)
        return self._record(action, ActionLifecycleStatus.approved, owner)

    async def execute_action(self, action_id, owner):
        action = next(a for a in self.submitted_actions if a.action_id == action_id)
        return self._record(action, ActionLifecycleStatus.succeeded, owner)

    async def get_action(self, action_id, owner):
        action = next(a for a in self.submitted_actions if a.action_id == action_id)
        return self._record(action, ActionLifecycleStatus.approved, owner)

    async def observe_session(self, session_id, *, owner=None, browser_session_id=None):
        return Observation(
            observation_id="obs-1",
            session_id=session_id,
            screenshot_ref=BlobRef(blob_id="blob-1", media_type="image/png", size_bytes=3, sha256="0" * 64),
            monitor=MonitorRef(monitor_id="primary", bounds=Rect(x=0, y=0, width=100, height=100), primary=True),
            screen_size=Size(width=100, height=100),
        )

    async def export_observation_blob(self, session_id, observation_id, blob_id, owner):
        return BlobRef(blob_id=blob_id, media_type="image/png", size_bytes=3, sha256="0" * 64), b"png"

    @property
    def step_actions(self) -> list[ActionCommand]:
        # Drop the plan-approval integration action so step assertions are stable.
        return [a for a in self.submitted_actions if a.kind != ActionKind.integration_action]

    def _snapshot(self, session_id):
        return DesktopSessionSnapshot(
            session=DesktopSessionRef(session_id=session_id, created_at=datetime.now(UTC)),
            status="ready",
            provider_name="fake",
            provider_health="healthy",
        )

    def _record(self, action, status, owner):
        return ActionLifecycleRecord(
            action=action,
            owner=owner,
            status=status,
            action_fingerprint=action_fingerprint(action),
            completed_at=datetime.now(UTC) if status == ActionLifecycleStatus.succeeded else None,
        )


def _owner() -> ActionOwner:
    return ActionOwner(thread_id="t", run_id="r", agent_id="computer_use_agent")


# --- planner prompt ---------------------------------------------------------


def test_planner_prompt_keeps_plan_and_session_boundaries():
    assert "Output ONLY one JSON object" in _PLANNER_SYSTEM_PROMPT
    assert "Plan, never execute" in _PLANNER_SYSTEM_PROMPT
    assert "Do not ask for screenshots or session ids" in _PLANNER_SYSTEM_PROMPT
    assert "requires_vision" in _PLANNER_SYSTEM_PROMPT


# --- plan step model --------------------------------------------------------


def test_plan_step_requires_vision_default_true():
    step = ComputerUsePlanStep(step_id="s1", instruction="x", risk=_RISK)
    assert step.requires_vision is True


@pytest.mark.parametrize(
    ("instruction", "expected"),
    [
        ("Launch Microsoft Edge browser and navigate to Gmail", "msedge"),
        ("open notepad", "notepad"),
        ("launch calculator", "calc"),
        ("Start Google Chrome and search for cats", "chrome"),
        ("open the edge browser", "msedge"),
        ("run excel", "excel"),
    ],
)
def test_normalize_app_name(instruction, expected):
    from vilagent.computer_use.plan_execute import _normalize_app_name

    assert _normalize_app_name(instruction) == expected


def test_args_for_step_normalizes_a_sentence_app_name():
    from vilagent.computer_use.plan_execute import _args_for_step

    step = _step("s1", "Launch Microsoft Edge browser and navigate to Gmail", requires_vision=False, action_kind=ActionKind.launch_app, args={"app_name": "Microsoft Edge browser and navigate to Gmail"})
    args = _args_for_step(step, ActionKind.launch_app)
    assert args["app_name"] == "msedge"  # not the whole sentence typed into Start search


# --- orchestrator deterministic (UIA) path ----------------------------------


@pytest.mark.asyncio
async def test_orchestrator_runs_deterministic_hotkey_step():
    plan = ComputerUsePlan(goal="save", steps=[_step("s1", "press ctrl+s", requires_vision=False, action_kind=ActionKind.hotkey, args={"keys": "ctrl+s"})])
    remote = FakeRemote()
    result = await PlanExecuteComputerUseOrchestrator(planner=FakePlanner(plan), remote=remote, auto_approve_risk_threshold=RiskLevel.critical).run("save", owner=_owner())

    assert result.status == StepStatus.completed
    assert remote.step_actions[0].kind == ActionKind.hotkey
    assert len(remote.resolve_queries) == 0  # hotkey needs no target resolution


@pytest.mark.asyncio
async def test_orchestrator_resolves_uia_click_target():
    plan = ComputerUsePlan(goal="save", steps=[_step("s1", "click Save", requires_vision=False, action_kind=ActionKind.click, target_description="Save button")])
    remote = FakeRemote()
    result = await PlanExecuteComputerUseOrchestrator(planner=FakePlanner(plan), remote=remote, auto_approve_risk_threshold=RiskLevel.critical).run("save", owner=_owner())

    assert result.status == StepStatus.completed
    assert len(remote.resolve_queries) == 1
    assert remote.step_actions[0].kind == ActionKind.click


@pytest.mark.asyncio
async def test_orchestrator_infers_launch_app_name():
    plan = ComputerUsePlan(goal="open", steps=[_step("s1", "open calculator", requires_vision=False, action_kind=ActionKind.launch_app)])
    remote = FakeRemote()
    result = await PlanExecuteComputerUseOrchestrator(planner=FakePlanner(plan), remote=remote, auto_approve_risk_threshold=RiskLevel.critical).run("open calculator", owner=_owner())

    assert result.status == StepStatus.completed
    assert remote.step_actions[0].args["app_name"] == "calc"  # normalized to the executable


@pytest.mark.asyncio
async def test_orchestrator_replans_only_when_step_blocks():
    plan = ComputerUsePlan(goal="save", steps=[_step("s1", "click Save", requires_vision=False, action_kind=ActionKind.click)])
    replan = ComputerUsePlan(goal="save", steps=[_step("s2", "press ctrl+s", requires_vision=False, action_kind=ActionKind.hotkey, args={"keys": "ctrl+s"})])
    remote = FakeRemote(target_available=False)  # first resolve returns no target -> step blocks
    planner = FakePlanner(plan, replan)

    result = await PlanExecuteComputerUseOrchestrator(planner=planner, remote=remote, auto_approve_risk_threshold=RiskLevel.critical).run("save", owner=_owner())

    assert result.status == StepStatus.completed, [s.model_dump(mode="json") for s in result.steps]
    assert planner.replan_calls == 1
    assert result.replan_count == 1
    assert remote.step_actions[-1].kind == ActionKind.hotkey


# --- orchestrator vision path -----------------------------------------------


class _FakeVisionProvider:
    def __init__(self, *args, **kwargs):
        self.calls = 0

    async def get_next_action(self, instruction, image_base64, chat_history, environment="native", max_actions=4, image_media_type="image/png"):
        self.calls += 1
        if self.calls == 1:
            action = ActionCommand(
                action_id="provider-action",
                session_id="",
                kind=ActionKind.click,
                target=TargetRef(strategy=TargetStrategy.coordinate, selector={"point": [10, 20]}, bounds=Rect(x=10, y=20, width=1, height=1), confidence=1, observation_id=""),
            )
            return action, [{"role": "assistant", "content": "clicked"}]
        return ActionCommand(action_id="provider-done", session_id="", kind=ActionKind.browser_action, args={"action": "terminate", "status": "success"}), []


def _vision_config():
    fara = type("Fara", (), {"base_url": "http://localhost:5000/v1", "api_key": "not-needed", "model_name": "fara", "timeout_seconds": 5})()
    uitars = type("UiTars", (), {"pyngrok_url": None, "api_key": "not-needed", "model_name": "ui-tars", "timeout_seconds": 5})()
    budgets = type("Budgets", (), {"vision_calls": 3})()
    cu = type("CU", (), {"vision_provider": "fara", "vision_fara_model": fara, "vision_uitars_model": uitars, "budgets": budgets, "vision_max_image_dimension": 1280, "vision_jpeg_quality": 85})()
    return type("AppConfig", (), {"computer_use": cu})()


@pytest.mark.asyncio
async def test_orchestrator_runs_vision_step_without_target_resolution(monkeypatch):
    monkeypatch.setattr("vilagent.computer_use.plan_execute.get_app_config", lambda: _vision_config())
    monkeypatch.setattr("vilagent.computer_use.plan_execute.FaraVisionActionProvider", _FakeVisionProvider)
    monkeypatch.setattr("vilagent.computer_use.plan_execute._detect_served_model_name_once", _fake_detect)

    plan = ComputerUsePlan(goal="save", steps=[_step("s1", "click the save button", requires_vision=True)])
    remote = FakeRemote()
    result = await PlanExecuteComputerUseOrchestrator(planner=FakePlanner(plan), remote=remote, auto_approve_risk_threshold=RiskLevel.critical).run("save", owner=_owner())

    assert result.status == StepStatus.completed
    assert len(remote.resolve_queries) == 0  # vision loop does not use UIA target resolution
    assert remote.step_actions[0].session_id == "session-1"
    assert remote.step_actions[0].action_id.startswith("fara-step-s1-1-")
    assert remote.step_actions[0].target.observation_id == "obs-1"


@pytest.mark.asyncio
async def test_vision_only_mode_forces_requires_vision(monkeypatch):
    monkeypatch.setattr("vilagent.computer_use.plan_execute.get_app_config", lambda: _vision_config())
    monkeypatch.setattr("vilagent.computer_use.plan_execute.FaraVisionActionProvider", _FakeVisionProvider)
    monkeypatch.setattr("vilagent.computer_use.plan_execute._detect_served_model_name_once", _fake_detect)

    # planner marks the step deterministic, but vision_only must override it
    plan = ComputerUsePlan(goal="save", steps=[_step("s1", "click the save button", requires_vision=False, action_kind=ActionKind.click)])
    remote = FakeRemote()
    result = await PlanExecuteComputerUseOrchestrator(planner=FakePlanner(plan), remote=remote, auto_approve_risk_threshold=RiskLevel.critical, execution_mode="vision_only").run("save", owner=_owner())

    assert result.status == StepStatus.completed
    assert len(remote.resolve_queries) == 0  # forced into the vision loop, no UIA resolution


async def _fake_detect(cache, base_url, api_key, default_model, *, detector=None):
    return default_model


@pytest.mark.asyncio
async def test_vision_provider_selection_is_authoritative_over_config(monkeypatch):
    """The UI-selected vision provider passed to the orchestrator wins over config.

    config default is 'fara' (see ``_vision_config``); the operator UI selected
    'ui_tars', so the UI-TARS provider must be the one constructed.
    """
    used: list[str] = []

    def _make_fake(label: str):
        class _Fake:
            def __init__(self, *args, **kwargs):
                used.append(label)

            async def get_next_action(self, instruction, image_base64, chat_history, environment="native", max_actions=4, image_media_type="image/png"):
                return ActionCommand(action_id="x", session_id="", kind=ActionKind.browser_action, args={"action": "terminate", "status": "success"}), []

        return _Fake

    monkeypatch.setattr("vilagent.computer_use.plan_execute.get_app_config", lambda: _vision_config())
    monkeypatch.setattr("vilagent.computer_use.plan_execute.FaraVisionActionProvider", _make_fake("fara"))
    monkeypatch.setattr("vilagent.computer_use.plan_execute.UiTarsVisionActionProvider", _make_fake("ui_tars"))
    monkeypatch.setattr("vilagent.computer_use.plan_execute._detect_served_model_name_once", _fake_detect)

    plan = ComputerUsePlan(goal="x", steps=[_step("s1", "click the button", requires_vision=True)])
    remote = FakeRemote()
    await PlanExecuteComputerUseOrchestrator(
        planner=FakePlanner(plan),
        remote=remote,
        auto_approve_risk_threshold=RiskLevel.critical,
        vision_provider="ui_tars",
    ).run("x", owner=_owner())

    assert used == ["ui_tars"]


@pytest.mark.asyncio
async def test_vision_wait_and_mouse_move_handled_locally_not_as_browser_action(monkeypatch):
    """'wait' / 'mouse_move' must not be submitted as browser_action (which is
    disabled in native Windows and fails with browser_action_disabled). They are
    handled locally and do not consume the real-action budget."""
    monkeypatch.setattr("vilagent.computer_use.plan_execute.get_app_config", lambda: _vision_config())
    monkeypatch.setattr("vilagent.computer_use.plan_execute._detect_served_model_name_once", _fake_detect)

    class _WaitMoveThenClick:
        def __init__(self, *args, **kwargs):
            self.calls = 0

        async def get_next_action(self, instruction, image_base64, chat_history, environment="native", max_actions=4, image_media_type="image/png"):
            self.calls += 1
            if self.calls == 1:
                return ActionCommand(action_id="x", session_id="", kind=ActionKind.browser_action, args={"action": "wait", "time": 0.01}), [{"role": "assistant", "content": "waiting"}]
            if self.calls == 2:
                return ActionCommand(action_id="x", session_id="", kind=ActionKind.browser_action, args={"action": "mouse_move", "coordinate": [5, 5]}), [{"role": "assistant", "content": "moving"}]
            return (
                ActionCommand(action_id="x", session_id="", kind=ActionKind.click, target=TargetRef(strategy=TargetStrategy.coordinate, selector={"point": [10, 20]}, bounds=Rect(x=10, y=20, width=1, height=1), confidence=1, observation_id="")),
                [{"role": "assistant", "content": "clicking"}],
            )

    monkeypatch.setattr("vilagent.computer_use.plan_execute.FaraVisionActionProvider", _WaitMoveThenClick)

    plan = ComputerUsePlan(goal="calc", steps=[_step("s1", "press 1", requires_vision=True)])
    remote = FakeRemote()
    result = await PlanExecuteComputerUseOrchestrator(
        planner=FakePlanner(plan),
        remote=remote,
        auto_approve_risk_threshold=RiskLevel.critical,
    ).run("calc", owner=_owner())

    assert result.status == StepStatus.completed
    assert all(a.kind != ActionKind.browser_action for a in remote.step_actions)  # wait/move never submitted
    assert any(a.kind == ActionKind.click for a in remote.step_actions)  # the real click ran


@pytest.mark.asyncio
async def test_type_text_step_is_deterministic_even_if_marked_vision(monkeypatch):
    """Typing must never go through the vision model (which clicks on-screen keys and
    falsely 'finishes'); it runs deterministically and the literal text is inferred
    from the instruction when the planner omitted args.text."""
    monkeypatch.setattr("vilagent.computer_use.plan_execute.get_app_config", lambda: _vision_config())

    def _no_vision(*args, **kwargs):
        raise AssertionError("typing must not use the vision provider")

    monkeypatch.setattr("vilagent.computer_use.plan_execute.FaraVisionActionProvider", _no_vision)

    plan = ComputerUsePlan(goal="calc", steps=[_step("s1", "Type calculation '12+23='", requires_vision=True, action_kind=ActionKind.type_text)])
    remote = FakeRemote()
    result = await PlanExecuteComputerUseOrchestrator(
        planner=FakePlanner(plan),
        remote=remote,
        auto_approve_risk_threshold=RiskLevel.critical,
    ).run("calc", owner=_owner())

    assert result.status == StepStatus.completed
    typed = [a for a in remote.step_actions if a.kind == ActionKind.type_text]
    assert typed, "expected a deterministic type_text action"
    assert typed[0].args.get("text") == "12+23="  # extracted from the quoted instruction


class _UncertainRemote(FakeRemote):
    """Every executed action comes back uncertain, simulating a stuck vision step."""

    async def execute_action(self, action_id, owner):
        action = next(a for a in self.submitted_actions if a.action_id == action_id)
        return ActionLifecycleRecord(
            action=action,
            owner=owner,
            status=ActionLifecycleStatus.uncertain,
            action_fingerprint=action_fingerprint(action),
            completed_at=datetime.now(UTC),
        )


@pytest.mark.asyncio
async def test_recovery_supervisor_is_consulted_when_vision_is_stuck(monkeypatch):
    """With recovery on, a stuck (uncertain) vision step escalates to the supervisor,
    whose advice is injected so the action model can recover and finish."""
    monkeypatch.setattr("vilagent.computer_use.plan_execute.get_app_config", lambda: _vision_config())
    monkeypatch.setattr("vilagent.computer_use.plan_execute._detect_served_model_name_once", _fake_detect)

    supervisor = {"calls": 0}

    class _FakeModel:
        async def ainvoke(self, messages):
            supervisor["calls"] += 1
            return type("Resp", (), {"content": "Close the popup ad, then click the search box."})()

    class _StuckThenRecover:
        def __init__(self, *args, **kwargs):
            self.calls = 0

        async def get_next_action(self, instruction, image_base64, chat_history, environment="native", max_actions=4, image_media_type="image/png"):
            self.calls += 1
            saw_supervisor = any("<supervisor>" in str(m.get("content", "")) for m in chat_history)
            if saw_supervisor:
                return ActionCommand(action_id="x", session_id="", kind=ActionKind.browser_action, args={"action": "terminate", "status": "success"}), []
            return (
                ActionCommand(
                    action_id="x",
                    session_id="",
                    kind=ActionKind.click,
                    target=TargetRef(strategy=TargetStrategy.coordinate, selector={"point": [5, 5]}, bounds=Rect(x=5, y=5, width=1, height=1), confidence=1, observation_id=""),
                ),
                [{"role": "assistant", "content": "clicking"}],
            )

    monkeypatch.setattr("vilagent.computer_use.plan_execute.FaraVisionActionProvider", _StuckThenRecover)

    plan = ComputerUsePlan(goal="search", steps=[_step("s1", "search a product", requires_vision=True)])
    remote = _UncertainRemote()
    result = await PlanExecuteComputerUseOrchestrator(
        planner=FakePlanner(plan),
        remote=remote,
        auto_approve_risk_threshold=RiskLevel.critical,
        vision_recovery=True,
        supervisor_model_factory=lambda: _FakeModel(),
    ).run("search", owner=_owner())

    assert supervisor["calls"] >= 1  # supervisor consulted on stuck
    assert result.status == StepStatus.completed  # recovered after advice


@pytest.mark.asyncio
async def test_stuck_model_gets_generic_nudge_when_supervisor_unavailable(monkeypatch):
    """When the model is stuck and the supervisor is off/rate-limited, a generic
    self-correction nudge is injected so the step can recover instead of failing."""
    monkeypatch.setattr("vilagent.computer_use.plan_execute.get_app_config", lambda: _vision_config())
    monkeypatch.setattr("vilagent.computer_use.plan_execute._detect_served_model_name_once", _fake_detect)

    class _StuckUntilNudged:
        def __init__(self, *args, **kwargs):
            self.calls = 0

        async def get_next_action(self, instruction, image_base64, chat_history, environment="native", max_actions=4, image_media_type="image/png"):
            self.calls += 1
            if any("repeated the same action" in str(m.get("content", "")) for m in chat_history):
                return ActionCommand(action_id="x", session_id="", kind=ActionKind.browser_action, args={"action": "terminate", "status": "success"}), []
            return (
                ActionCommand(action_id="x", session_id="", kind=ActionKind.click, target=TargetRef(strategy=TargetStrategy.coordinate, selector={"point": [5, 5]}, bounds=Rect(x=5, y=5, width=1, height=1), confidence=1, observation_id="")),
                [{"role": "assistant", "content": "clicking"}],
            )

    monkeypatch.setattr("vilagent.computer_use.plan_execute.FaraVisionActionProvider", _StuckUntilNudged)

    # max_actions=4 so the repeated-action guard fires before the budget is spent.
    plan = ComputerUsePlan(goal="x", steps=[_step("s1", "click compose", requires_vision=True, max_actions=4)])
    remote = _UncertainRemote()
    result = await PlanExecuteComputerUseOrchestrator(
        planner=FakePlanner(plan),
        remote=remote,
        auto_approve_risk_threshold=RiskLevel.critical,
        vision_recovery=False,  # supervisor off -> generic nudge must still kick in
    ).run("x", owner=_owner())

    assert result.status == StepStatus.completed


@pytest.mark.asyncio
async def test_recovery_off_does_not_consult_supervisor(monkeypatch):
    """With recovery off (default), the supervisor model is never created/consulted."""
    monkeypatch.setattr("vilagent.computer_use.plan_execute.get_app_config", lambda: _vision_config())
    monkeypatch.setattr("vilagent.computer_use.plan_execute._detect_served_model_name_once", _fake_detect)

    created = {"models": 0}

    def _no_supervisor():
        created["models"] += 1
        raise AssertionError("supervisor model must not be built when recovery is off")

    monkeypatch.setattr("vilagent.computer_use.plan_execute.FaraVisionActionProvider", _FakeVisionProvider)

    plan = ComputerUsePlan(goal="x", steps=[_step("s1", "do it", requires_vision=True)])
    remote = FakeRemote()
    result = await PlanExecuteComputerUseOrchestrator(
        planner=FakePlanner(plan),
        remote=remote,
        auto_approve_risk_threshold=RiskLevel.critical,
        vision_recovery=False,
        supervisor_model_factory=_no_supervisor,
    ).run("x", owner=_owner())

    assert created["models"] == 0
    assert result.status == StepStatus.completed
