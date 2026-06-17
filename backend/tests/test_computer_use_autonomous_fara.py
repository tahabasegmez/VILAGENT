"""Tests for the autonomous FARA orchestrator (single-brief, whole-task loop)."""

from __future__ import annotations

import pytest

from vilagent.computer_use.models import (
    ActionCommand,
    ActionKind,
    ActionLifecycleRecord,
    ActionLifecycleStatus,
    ActionOwner,
    Rect,
    RiskLevel,
    TargetRef,
    TargetStrategy,
    action_fingerprint,
)
from vilagent.computer_use.autonomous_fara import AutonomousFaraOrchestrator
from vilagent.computer_use.plan_execute import StepStatus


def _owner() -> ActionOwner:
    return ActionOwner(thread_id="t", run_id="r", agent_id="a")


def _click(x=100, y=200) -> ActionCommand:
    return ActionCommand(
        action_id="x", session_id="", kind=ActionKind.click,
        target=TargetRef(strategy=TargetStrategy.coordinate, selector={"point": [x, y]}, bounds=Rect(x=x, y=y, width=1, height=1), confidence=1, observation_id=""),
    )


def _terminate(status="success") -> ActionCommand:
    return ActionCommand(action_id="t", session_id="", kind=ActionKind.browser_action, args={"action": "terminate", "status": status})


def _config(environment_attrs=None):
    browser = type("Browser", (), {"playwright_headless": True, "viewport_width": 1000, "viewport_height": 700})()
    fara = type("Fara", (), {"base_url": "http://localhost:5000/v1", "api_key": "not-needed", "model_name": "fara", "timeout_seconds": 5})()
    cu = type("CU", (), {"vision_fara_model": fara, "browser": browser, "vision_max_image_dimension": 0, "vision_jpeg_quality": 85})()
    return type("AppConfig", (), {"computer_use": cu})()


class _DirectiveModel:
    def __init__(self, environment: str):
        self._environment = environment

    async def ainvoke(self, messages):
        return type("Msg", (), {"content": f'{{"environment":"{self._environment}","directive":"do the whole task"}}'})()


def _patch_common(monkeypatch, environment: str, actions: list[ActionCommand]):
    monkeypatch.setattr("vilagent.computer_use.autonomous_fara.get_app_config", lambda: _config())
    monkeypatch.setattr("vilagent.computer_use.autonomous_fara.create_chat_model", lambda *a, **k: _DirectiveModel(environment))

    async def _detect(cache, base_url, api_key, default_model, *, detector=None):
        return default_model

    monkeypatch.setattr("vilagent.computer_use.autonomous_fara._detect_served_model_name_once", _detect)
    monkeypatch.setattr("vilagent.computer_use.autonomous_fara.encode_image_for_vision", lambda image_bytes, **k: ("", "image/png", 1.0))

    seq = list(actions)

    class _FakeProvider:
        def __init__(self, *a, **k):
            pass

        async def get_next_action(self, **kwargs):
            assert kwargs["autonomous"] is True
            action = seq.pop(0)
            return action, [{"role": "assistant", "content": "step"}]

    monkeypatch.setattr("vilagent.computer_use.autonomous_fara.FaraVisionActionProvider", _FakeProvider)


class _FakeBrowserSession:
    def __init__(self):
        self.ran: list[ActionCommand] = []

    async def screenshot(self):
        return b"\x89PNG\r\n"

    async def run_action(self, action):
        self.ran.append(action)
        return True, None

    async def close(self):
        pass


@pytest.mark.asyncio
async def test_autonomous_browser_runs_until_terminate(monkeypatch):
    _patch_common(monkeypatch, "browser", [_click(), _click(300, 400), _terminate("success")])
    orch = AutonomousFaraOrchestrator(instruction_model_name="m", remote=object(), auto_approve_risk_threshold=RiskLevel.critical)
    fake = _FakeBrowserSession()

    async def _ensure(config):
        orch._browser_session = fake
        return fake

    monkeypatch.setattr(orch, "_ensure_browser_session", _ensure)

    result = await orch.run("book a table", owner=_owner())

    assert result.status == StepStatus.completed
    assert len(fake.ran) == 2  # two clicks executed; terminate ended the run
    assert result.plan.steps[0].instruction == "do the whole task"


class _FakeRemote:
    def __init__(self):
        self.action = None
        self.submits = 0

    async def get_session(self, sid):
        return object()

    async def observe_session(self, sid, *, owner=None):
        from vilagent.computer_use.models import BlobRef, MonitorRef, Observation, Size

        return Observation(
            observation_id="obs-1", session_id=sid,
            monitor=MonitorRef(monitor_id="primary", bounds=Rect(x=0, y=0, width=100, height=100), primary=True),
            screen_size=Size(width=100, height=100),
            screenshot_ref=BlobRef(blob_id="b", media_type="image/png", size_bytes=3, sha256="0" * 64),
        )

    async def export_observation_blob(self, *args):
        return ("image/png", b"\x89PNG\r\n")

    def _record(self, status):
        from datetime import UTC, datetime

        terminal = {ActionLifecycleStatus.succeeded, ActionLifecycleStatus.failed, ActionLifecycleStatus.denied, ActionLifecycleStatus.cancelled}
        return ActionLifecycleRecord(
            action=self.action, owner=_owner(), status=status,
            action_fingerprint=action_fingerprint(self.action),
            completed_at=datetime.now(UTC) if status in terminal else None,
        )

    async def submit_action(self, action, owner):
        self.action = action
        self.submits += 1
        return self._record(ActionLifecycleStatus.pending)

    async def execute_action(self, action_id, owner):
        return self._record(ActionLifecycleStatus.succeeded)

    async def get_action(self, action_id, owner):
        return self._record(ActionLifecycleStatus.pending)


@pytest.mark.asyncio
async def test_autonomous_native_runs_until_terminate(monkeypatch):
    _patch_common(monkeypatch, "native", [_click(), _terminate("success")])
    remote = _FakeRemote()
    orch = AutonomousFaraOrchestrator(instruction_model_name="m", remote=remote, auto_approve_risk_threshold=RiskLevel.critical)

    result = await orch.run("open notepad and type hi", owner=_owner(), session_id="s1")

    assert result.status == StepStatus.completed
    assert remote.submits == 1  # the click was submitted+executed; terminate ended the run
    assert result.plan.steps[0].environment.value == "native"


@pytest.mark.asyncio
async def test_autonomous_reports_failure_on_fara_terminate_failure(monkeypatch):
    # First failure is pushed back once; a repeated failure is honoured.
    _patch_common(monkeypatch, "browser", [_terminate("failure"), _terminate("failure")])
    orch = AutonomousFaraOrchestrator(instruction_model_name="m", remote=object(), auto_approve_risk_threshold=RiskLevel.critical)
    fake = _FakeBrowserSession()

    async def _ensure(config):
        orch._browser_session = fake
        return fake

    monkeypatch.setattr(orch, "_ensure_browser_session", _ensure)
    result = await orch.run("impossible task", owner=_owner())

    assert result.status == StepStatus.failed
    assert result.steps[0].error_code == "fara_terminate_failure"
