from __future__ import annotations

from datetime import UTC, datetime

import pytest

from vilagent.computer_use.fara import FaraVisionActionProvider, _build_fara_system_prompt, _collapse_consecutive_roles


def test_collapse_consecutive_roles_merges_user_turns_for_strict_vllm():
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "assistant", "content": "act1"},
        {"role": "user", "content": "<tool_response>ok</tool_response>"},
        {"role": "user", "content": [{"type": "text", "text": "next"}, {"type": "image_url", "image_url": {"url": "x"}}]},
    ]
    out = _collapse_consecutive_roles(messages)
    assert [m["role"] for m in out] == ["system", "assistant", "user"]
    # the two user turns merged into one multi-part message (tool_response + text + image)
    assert isinstance(out[-1]["content"], list) and len(out[-1]["content"]) == 3
from vilagent.computer_use.models import (
    ActionCommand,
    ActionKind,
    ActionLifecycleRecord,
    ActionLifecycleStatus,
    ActionOwner,
    BlobRef,
    MonitorRef,
    Observation,
    PolicyDecision,
    Rect,
    RiskAssessment,
    RiskLevel,
    Size,
    StructuredError,
    action_fingerprint,
)
from vilagent.computer_use.plan_execute import ComputerUsePlanStep, ComputerUseStepExecutor, PlannedRiskAssessment, StepStatus, _PLANNER_SYSTEM_PROMPT, _detect_served_model_name_once, _fallback_plan, _vision_action_limit, _vision_step_command
from vilagent.computer_use.policy import DefaultActionPolicy
from vilagent.config.computer_use_config import ComputerUseFaraModelConfig


def test_fara_coordinate_click_includes_required_postcondition():
    provider = FaraVisionActionProvider(ComputerUseFaraModelConfig(enabled=True))

    action = provider._map_to_vilagent_action(
        "computer_use",
        "left_click",
        {"action": "left_click", "coordinate": [10, 20]},
    )

    assert action.kind == ActionKind.click
    assert action.postconditions[0].kind == "screen_changed"
    assert DefaultActionPolicy().evaluate(action).decision == PolicyDecision.require_approval


@pytest.mark.parametrize(
    ("action_name", "args", "expected_kind"),
    [
        ("type", {"action": "type", "text": "value", "coordinate": [10, 20]}, ActionKind.type_text),
        ("key", {"action": "key", "keys": ["ENTER"], "coordinate": [10, 20]}, ActionKind.hotkey),
    ],
)
def test_fara_non_pointer_actions_discard_model_supplied_coordinates(action_name, args, expected_kind):
    action = FaraVisionActionProvider(ComputerUseFaraModelConfig(enabled=True))._map_to_vilagent_action(
        "computer_use",
        action_name,
        args,
    )

    assert action.kind == expected_kind
    assert action.target is None


def test_fara_finish_step_maps_to_loop_termination():
    provider = FaraVisionActionProvider(ComputerUseFaraModelConfig(enabled=True))

    action = provider._map_to_vilagent_action(
        "computer_use",
        "finish_step",
        {"action": "finish_step", "status": "success"},
    )

    assert action.args["action"] == "terminate"
    assert action.args["status"] == "success"


def test_fara_visit_url_has_required_postcondition():
    action = FaraVisionActionProvider(ComputerUseFaraModelConfig(enabled=True))._map_to_vilagent_action(
        "browser_action",
        "visit_url",
        {"action": "visit_url", "url": "https://example.com"},
    )

    assert action.kind == ActionKind.browser_action
    assert action.target is None
    assert action.postconditions[0].kind == "screen_changed"
    assert DefaultActionPolicy().evaluate(action).decision == PolicyDecision.allow


def test_vision_step_command_forbids_later_steps_and_double_checks():
    command = _vision_step_command(
        ComputerUsePlanStep(
            step_id="s1",
            instruction="Click Date modified once.",
            completion_criteria="The Date modified header has been clicked once.",
            risk={"level": RiskLevel.low},
        )
    )

    assert "CURRENT STEP" in command
    assert "finish_step success" in command
    assert "pursue a later step / different user goal" in command
    assert "popups, dialogs, cookie banners" in command
    assert "smallest safe action" in command
    assert "ACTION KIND:" in command
    assert "TARGET:" in command
    assert "ACTION ARGUMENTS:" in command


def test_vision_step_command_browser_blank_page_prompts_navigation():
    command = _vision_step_command(
        ComputerUsePlanStep(step_id="s1", instruction="search the site", environment="browser", risk={"level": RiskLevel.low}),
        current_url="about:blank",
    )
    assert "blank page" in command
    assert "visit_url" in command


def test_vision_action_limit_gives_multi_action_headroom():
    native_small = ComputerUsePlanStep(step_id="s1", instruction="click", max_actions=4, risk={"level": RiskLevel.low})
    native_big = ComputerUsePlanStep(step_id="s2", instruction="click", max_actions=12, risk={"level": RiskLevel.low})
    browser_big = ComputerUsePlanStep(step_id="s3", instruction="click", environment="browser", max_actions=12, risk={"level": RiskLevel.low})

    assert _vision_action_limit(native_small, 10) == 4
    assert _vision_action_limit(native_big, 10) == 12  # native capped at 12
    assert _vision_action_limit(browser_big, 10) == 12  # browser cap is 16; step asked for 12


def test_fara_prompt_reserves_coordinates_for_pointer_actions():
    prompt = _build_fara_system_prompt("native", 2)

    assert "screenshot coordinates only for pointer actions" in prompt
    assert "For type and key actions" in prompt
    assert "popups, ads, cookie banners" in prompt
    assert "smallest safe action" in prompt


def test_planner_prefers_generic_deterministic_actions_before_vision():
    assert "Prefer deterministic input steps over visual actions" in _PLANNER_SYSTEM_PROMPT
    assert "keyboard, text-entry, UIA, or DOM equivalent" in _PLANNER_SYSTEM_PROMPT
    assert "calculator" not in _PLANNER_SYSTEM_PROMPT.casefold()


def test_fallback_plan_uses_plan_risk_schema():
    plan = _fallback_plan("Perform a desktop operation")

    assert len(plan.steps) == 1
    assert isinstance(plan.steps[0].risk, PlannedRiskAssessment)
    assert plan.steps[0].risk.level == RiskLevel.medium


@pytest.mark.asyncio
async def test_deterministic_type_text_does_not_resolve_a_visual_target():
    class FakeRemote:
        async def resolve_target(self, *args, **kwargs):
            raise AssertionError("type_text must not resolve a visual target")

    target = await ComputerUseStepExecutor(FakeRemote())._resolve_target(
        ComputerUsePlanStep(
            step_id="s1",
            instruction="Type 10",
            action_kind=ActionKind.type_text,
            args={"text": "10"},
            requires_vision=False,
            risk={"level": RiskLevel.low},
        ),
        owner=ActionOwner(thread_id="thread", run_id="run", agent_id="agent"),
        session_id="session-1",
        action_kind=ActionKind.type_text,
    )

    assert target is None


@pytest.mark.asyncio
async def test_fara_loop_waits_for_action_approval_before_continuing(monkeypatch):
    owner = ActionOwner(thread_id="thread", run_id="run", agent_id="agent")
    provider = FaraVisionActionProvider(ComputerUseFaraModelConfig(enabled=True))
    click = provider._map_to_vilagent_action(
        "computer_use",
        "left_click",
        {"action": "left_click", "coordinate": [10, 20]},
    )
    terminate = ActionCommand(
        action_id="terminate",
        session_id="",
        kind=ActionKind.browser_action,
        args={"action": "terminate", "status": "success"},
    )

    class FakeProvider:
        def __init__(self, config):
            self.actions = iter((click, terminate))

        async def get_next_action(self, **kwargs):
            return next(self.actions), []

    class FakeRemote:
        def __init__(self):
            self.action = None
            self.get_action_calls = 0
            self.execute_calls = 0

        async def observe_session(self, session_id, *, owner=None):
            return Observation(
                observation_id="obs-1",
                session_id=session_id,
                screenshot_ref=BlobRef(blob_id="blob", media_type="image/png", size_bytes=3, sha256="0" * 64),
                monitor=MonitorRef(monitor_id="primary", bounds=Rect(x=0, y=0, width=100, height=100)),
                screen_size=Size(width=100, height=100),
            )

        async def export_observation_blob(self, *args):
            return None, b"png"

        async def submit_action(self, action, requested_owner):
            self.action = action
            return self.record(ActionLifecycleStatus.awaiting_approval)

        async def get_action(self, action_id, requested_owner):
            self.get_action_calls += 1
            return self.record(ActionLifecycleStatus.approved)

        async def execute_action(self, action_id, requested_owner):
            self.execute_calls += 1
            return self.record(ActionLifecycleStatus.succeeded)

        def record(self, status):
            return ActionLifecycleRecord(
                action=self.action,
                owner=owner,
                status=status,
                action_fingerprint=action_fingerprint(self.action),
                completed_at=datetime.now(UTC) if status == ActionLifecycleStatus.succeeded else None,
            )

    config = type(
        "AppConfig",
        (),
        {
            "computer_use": type(
                "ComputerUse",
                (),
                {
                    "vision_provider": "fara",
                    "vision_uitars_model": type("UiTars", (), {"pyngrok_url": None})(),
                    "vision_fara_model": ComputerUseFaraModelConfig(enabled=True, base_url="http://localhost:5000/v1"),
                },
            )()
        },
    )()
    monkeypatch.setattr("vilagent.computer_use.plan_execute.get_app_config", lambda: config)
    monkeypatch.setattr("vilagent.computer_use.plan_execute.FaraVisionActionProvider", FakeProvider)

    remote = FakeRemote()
    _, result = await ComputerUseStepExecutor(remote)._execute_fara_vision_loop(
        ComputerUsePlanStep(step_id="s1", instruction="click Downloads", risk={"level": RiskLevel.medium}),
        owner=owner,
        session_id="session-1",
        auto_approve_risk_threshold=RiskLevel.low,
        max_actions=2,
    )

    assert result.status == StepStatus.completed
    assert result.summary == "Fara completed the step successfully"

    assert remote.get_action_calls == 1
    assert remote.execute_calls == 1
    assert remote.action.risk.level == RiskLevel.medium
    assert remote.action.auto_approve_risk_threshold == RiskLevel.low


@pytest.mark.asyncio
async def test_fara_loop_retries_transient_desktop_change_and_reports_final_error(monkeypatch):
    owner = ActionOwner(thread_id="thread", run_id="run", agent_id="agent")
    click = FaraVisionActionProvider(ComputerUseFaraModelConfig(enabled=True))._map_to_vilagent_action(
        "computer_use",
        "left_click",
        {"action": "left_click", "coordinate": [10, 20]},
    )

    class FakeProvider:
        def __init__(self, config):
            self.calls = 0

        async def get_next_action(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return click, [{"role": "assistant", "content": "click"}, {"role": "user", "content": "success"}]
            return ActionCommand(
                action_id="terminate",
                session_id="",
                kind=ActionKind.browser_action,
                args={"action": "terminate", "status": "success"},
            ), []

    class FakeRemote:
        def __init__(self):
            self.action = None
            self.execute_calls = 0

        async def observe_session(self, session_id, *, owner=None):
            return Observation(
                observation_id=f"obs-{self.execute_calls}",
                session_id=session_id,
                screenshot_ref=BlobRef(blob_id="blob", media_type="image/png", size_bytes=3, sha256="0" * 64),
                monitor=MonitorRef(monitor_id="primary", bounds=Rect(x=0, y=0, width=100, height=100)),
                screen_size=Size(width=100, height=100),
            )

        async def export_observation_blob(self, *args):
            return None, b"png"

        async def submit_action(self, action, requested_owner):
            self.action = action
            return self.record(ActionLifecycleStatus.approved)

        async def execute_action(self, action_id, requested_owner):
            self.execute_calls += 1
            return self.record(
                ActionLifecycleStatus.failed,
                error=StructuredError(
                    code="desktop_changed_before_mutation",
                    message="Desktop changed after validation and before mutation.",
                ),
            )

        def record(self, status, error=None):
            return ActionLifecycleRecord(
                action=self.action,
                owner=owner,
                status=status,
                action_fingerprint=action_fingerprint(self.action),
                error=error,
                completed_at=datetime.now(UTC) if status == ActionLifecycleStatus.failed else None,
            )

    config = type(
        "AppConfig",
        (),
        {
            "computer_use": type(
                "ComputerUse",
                (),
                {
                    "vision_provider": "fara",
                    "vision_uitars_model": type("UiTars", (), {"pyngrok_url": None})(),
                    "vision_fara_model": ComputerUseFaraModelConfig(enabled=True, base_url="http://localhost:5000/v1"),
                },
            )()
        },
    )()
    monkeypatch.setattr("vilagent.computer_use.plan_execute.get_app_config", lambda: config)
    monkeypatch.setattr("vilagent.computer_use.plan_execute.FaraVisionActionProvider", FakeProvider)

    _, result = await ComputerUseStepExecutor(FakeRemote())._execute_fara_vision_loop(
        ComputerUsePlanStep(step_id="s1", instruction="click Downloads", risk={"level": RiskLevel.low}),
        owner=owner,
        session_id="session-1",
        auto_approve_risk_threshold=RiskLevel.critical,
        max_actions=2,
    )

    assert result.status == StepStatus.completed

    _, retried_with_low_configured_budget = await ComputerUseStepExecutor(FakeRemote())._execute_fara_vision_loop(
        ComputerUsePlanStep(step_id="s1", instruction="click Downloads", risk={"level": RiskLevel.low}),
        owner=owner,
        session_id="session-1",
        auto_approve_risk_threshold=RiskLevel.critical,
        max_actions=1,
    )

    assert retried_with_low_configured_budget.status == StepStatus.failed
    assert retried_with_low_configured_budget.summary == "Step failed after 1 action(s) (desktop_changed_before_mutation)."


@pytest.mark.asyncio
async def test_fara_loop_retries_empty_message_model_error_and_reports_type(monkeypatch):
    owner = ActionOwner(thread_id="thread", run_id="run", agent_id="agent")

    class EmptyModelError(Exception):
        pass

    class FakeProvider:
        calls = 0

        def __init__(self, config):
            pass

        async def get_next_action(self, **kwargs):
            FakeProvider.calls += 1
            raise EmptyModelError()

    class FakeRemote:
        async def observe_session(self, session_id, *, owner=None):
            return Observation(
                observation_id="obs-1",
                session_id=session_id,
                screenshot_ref=BlobRef(blob_id="blob", media_type="image/png", size_bytes=3, sha256="0" * 64),
                monitor=MonitorRef(monitor_id="primary", bounds=Rect(x=0, y=0, width=100, height=100)),
                screen_size=Size(width=100, height=100),
            )

        async def export_observation_blob(self, *args):
            return None, b"png"

    config = type(
        "AppConfig",
        (),
        {
            "computer_use": type(
                "ComputerUse",
                (),
                {
                    "vision_provider": "fara",
                    "vision_uitars_model": type("UiTars", (), {"pyngrok_url": None})(),
                    "vision_fara_model": ComputerUseFaraModelConfig(enabled=True, base_url="http://localhost:5000/v1"),
                },
            )()
        },
    )()
    monkeypatch.setattr("vilagent.computer_use.plan_execute.get_app_config", lambda: config)
    monkeypatch.setattr("vilagent.computer_use.plan_execute.FaraVisionActionProvider", FakeProvider)

    _, result = await ComputerUseStepExecutor(FakeRemote())._execute_fara_vision_loop(
        ComputerUsePlanStep(step_id="s3", instruction="click 0", risk={"level": RiskLevel.low}),
        owner=owner,
        session_id="session-1",
        auto_approve_risk_threshold=RiskLevel.critical,
        max_actions=2,
    )

    assert FakeProvider.calls == 2
    assert result.status == StepStatus.failed
    assert result.error_code == "vision_action_failed"
    assert result.summary == "Failed to get next vision action after 2 action(s): EmptyModelError"


@pytest.mark.asyncio
async def test_served_model_detection_is_cached_per_endpoint():
    detection_calls = 0

    async def detect_model(*args):
        nonlocal detection_calls
        detection_calls += 1
        return "served-fara"

    cache = {}
    for _ in range(2):
        assert await _detect_served_model_name_once(
            cache,
            "https://example.ngrok-free.dev/v1",
            "key",
            "default",
            detector=detect_model,
        )

    assert detection_calls == 1


@pytest.mark.asyncio
async def test_fara_loop_makes_final_failed_decision_at_hard_action_cap(monkeypatch):
    owner = ActionOwner(thread_id="thread", run_id="run", agent_id="agent")
    click = FaraVisionActionProvider(ComputerUseFaraModelConfig(enabled=True))._map_to_vilagent_action(
        "computer_use",
        "left_click",
        {"action": "left_click", "coordinate": [10, 20]},
    )

    class FakeProvider:
        def __init__(self, config):
            self.calls = 0

        async def get_next_action(self, **kwargs):
            self.calls += 1
            action = click.model_copy(
                deep=True,
                update={
                    "target": click.target.model_copy(
                        deep=True,
                        update={
                            "bounds": Rect(x=10 + self.calls, y=20, width=1, height=1),
                            "selector": {"point": [10 + self.calls, 20]},
                        },
                    )
                },
            )
            return action, [{"role": "assistant", "content": "click"}, {"role": "user", "content": "success"}]

    class FakeRemote:
        def __init__(self):
            self.action = None
            self.submit_calls = 0

        async def observe_session(self, session_id, *, owner=None):
            return Observation(
                observation_id=f"obs-{self.submit_calls}",
                session_id=session_id,
                screenshot_ref=BlobRef(blob_id="blob", media_type="image/png", size_bytes=3, sha256="0" * 64),
                monitor=MonitorRef(monitor_id="primary", bounds=Rect(x=0, y=0, width=100, height=100)),
                screen_size=Size(width=100, height=100),
            )

        async def export_observation_blob(self, *args):
            return None, b"png"

        async def submit_action(self, action, requested_owner):
            self.action = action
            self.submit_calls += 1
            return self.record(ActionLifecycleStatus.approved)

        async def execute_action(self, action_id, requested_owner):
            return self.record(
                ActionLifecycleStatus.uncertain,
                error=StructuredError(
                    code="postcondition_failed",
                    message="Action completed but postconditions were not satisfied.",
                ),
            )

        def record(self, status, error=None):
            return ActionLifecycleRecord(
                action=self.action,
                owner=owner,
                status=status,
                action_fingerprint=action_fingerprint(self.action),
                error=error,
                completed_at=datetime.now(UTC) if status == ActionLifecycleStatus.uncertain else None,
            )

    config = type(
        "AppConfig",
        (),
        {
            "computer_use": type(
                "ComputerUse",
                (),
                {
                    "vision_provider": "fara",
                    "vision_uitars_model": type("UiTars", (), {"pyngrok_url": None})(),
                    "vision_fara_model": ComputerUseFaraModelConfig(enabled=True, base_url="http://localhost:5000/v1"),
                },
            )()
        },
    )()
    monkeypatch.setattr("vilagent.computer_use.plan_execute.get_app_config", lambda: config)
    monkeypatch.setattr("vilagent.computer_use.plan_execute.FaraVisionActionProvider", FakeProvider)

    remote = FakeRemote()
    _, result = await ComputerUseStepExecutor(remote)._execute_fara_vision_loop(
        ComputerUsePlanStep(step_id="s1", instruction="click Date modified", max_actions=2, risk={"level": RiskLevel.low}),
        owner=owner,
        session_id="session-1",
        auto_approve_risk_threshold=RiskLevel.critical,
        max_actions=10,
    )

    assert remote.submit_calls == 10
    assert result.status == StepStatus.failed
    assert result.error_code == "step_uncertain_after_action_limit"
    assert result.summary == "Step failed after 10 action(s) because the model could not verify completion (postcondition_failed)."


@pytest.mark.asyncio
async def test_explicit_launch_app_step_bypasses_vision_executor(monkeypatch):
    owner = ActionOwner(thread_id="thread", run_id="run", agent_id="agent")

    class FakeRemote:
        def __init__(self):
            self.action = None

        async def get_session(self, session_id):
            return object()

        async def submit_action(self, action, requested_owner):
            self.action = action
            return self.record(ActionLifecycleStatus.approved)

        async def execute_action(self, action_id, requested_owner):
            return self.record(ActionLifecycleStatus.succeeded)

        def record(self, status):
            return ActionLifecycleRecord(
                action=self.action,
                owner=owner,
                status=status,
                action_fingerprint=action_fingerprint(self.action),
                completed_at=datetime.now(UTC) if status == ActionLifecycleStatus.succeeded else None,
            )

    config = type(
        "AppConfig",
        (),
        {"computer_use": type("ComputerUse", (), {"vision_provider": "fara"})()},
    )()
    monkeypatch.setattr("vilagent.computer_use.plan_execute.get_app_config", lambda: config)

    remote = FakeRemote()
    _, result = await ComputerUseStepExecutor(remote).execute(
        ComputerUsePlanStep(
            step_id="s1",
            instruction="Open Windows File Explorer",
            requires_vision=True,
            action_kind=ActionKind.launch_app,
            args={"app_name": "explorer.exe"},
            risk={"level": RiskLevel.low},
        ),
        owner=owner,
        session_id="session-1",
        auto_approve_risk_threshold=RiskLevel.low,
    )

    assert result.status == StepStatus.completed
    assert remote.action.kind == ActionKind.launch_app
    assert remote.action.args["app_name"] == "explorer.exe"
