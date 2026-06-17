"""Tests for the Playwright-driven browser control of FARA browser steps."""

from __future__ import annotations

import pytest

from vilagent.computer_use.models import (
    ActionCommand,
    ActionKind,
    ActionOwner,
    Rect,
    RiskLevel,
    TargetRef,
    TargetStrategy,
)
from vilagent.computer_use.browser_playwright import PlaywrightBrowserSession
from vilagent.computer_use.plan_execute import (
    ComputerUsePlan,
    ComputerUsePlanStep,
    ComputerUseStepExecutor,
    PlannedRiskAssessment,
    StepStatus,
)


def _owner() -> ActionOwner:
    return ActionOwner(thread_id="t", run_id="r", agent_id="a")


def _coord_action(kind: ActionKind, x: int, y: int, **args) -> ActionCommand:
    return ActionCommand(
        action_id="x", session_id="", kind=kind,
        target=TargetRef(strategy=TargetStrategy.coordinate, selector={"point": [x, y]}, bounds=Rect(x=x, y=y, width=1, height=1), confidence=1, observation_id=""),
        args=args,
    )


class FakeMouse:
    def __init__(self):
        self.events = []

    async def click(self, x, y, **kwargs):
        self.events.append(("click", x, y, kwargs.get("button", "left"), kwargs.get("click_count", 1)))

    async def move(self, x, y, **kwargs):
        self.events.append(("move", x, y))

    async def wheel(self, dx, dy):
        self.events.append(("wheel", dx, dy))


class FakeKeyboard:
    def __init__(self):
        self.events = []

    async def type(self, text, **kwargs):
        self.events.append(("type", text))

    async def press(self, key):
        self.events.append(("press", key))

    async def down(self, key):
        self.events.append(("down", key))

    async def up(self, key):
        self.events.append(("up", key))


class FakePage:
    def __init__(self):
        self.mouse = FakeMouse()
        self.keyboard = FakeKeyboard()
        self.url = "about:blank"
        self.goto_calls = []

    async def goto(self, url, **kwargs):
        self.goto_calls.append(url)
        self.url = url

    async def go_back(self, **kwargs):
        self.url = "back"

    async def reload(self, **kwargs):
        pass

    async def wait_for_load_state(self, *args, **kwargs):
        pass

    def expect_event(self, name, **kwargs):
        page = self

        class _Ctx:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                # Pretend no popup happened: raise the timeout-like path is handled by caller.
                return False

            @property
            def value(self):
                raise TimeoutError("no popup")

        return _Ctx()


@pytest.mark.asyncio
async def test_shared_browser_session_persists_and_recreates_when_dead(monkeypatch):
    import vilagent.computer_use.browser_playwright as bp

    instances: list["_FakeShared"] = []

    class _FakeShared:
        def __init__(self, **kwargs):
            self.alive = True
            self.started = False
            self.closed = False
            instances.append(self)

        async def start(self):
            self.started = True

        def is_alive(self):
            return self.alive

        async def close(self):
            self.closed = True

    monkeypatch.setattr(bp, "PlaywrightBrowserSession", _FakeShared)
    monkeypatch.setattr(bp, "_shared_session", None)

    first = await bp.get_shared_browser_session(headless=True)
    second = await bp.get_shared_browser_session(headless=True)
    assert first is second  # reused, not recreated
    assert len(instances) == 1 and instances[0].started

    # Operator closed the window -> next call rebuilds a fresh session.
    first.alive = False
    third = await bp.get_shared_browser_session(headless=True)
    assert third is not first
    assert first.closed is True
    assert len(instances) == 2

    await bp.close_shared_browser_session()
    assert third.closed is True
    assert bp._shared_session is None


def _session_with_fake_page() -> tuple[PlaywrightBrowserSession, FakePage]:
    s = PlaywrightBrowserSession(viewport_width=1000, viewport_height=700)
    page = FakePage()
    s._page = page
    s._started = True
    return s, page


@pytest.mark.asyncio
async def test_browser_session_maps_click_type_key_scroll_navigate():
    s, page = _session_with_fake_page()

    ok, err = await s.run_action(_coord_action(ActionKind.click, 120, 240))
    assert ok and err is None
    assert ("click", 120.0, 240.0, "left", 1) in page.mouse.events

    assert (await s.run_action(ActionCommand(action_id="t", session_id="", kind=ActionKind.type_text, args={"text": "hello"})))[0]
    assert ("type", "hello") in page.keyboard.events

    assert (await s.run_action(ActionCommand(action_id="k", session_id="", kind=ActionKind.hotkey, args={"keys": ["Enter"]})))[0]
    assert ("down", "Enter") in page.keyboard.events and ("up", "Enter") in page.keyboard.events

    assert (await s.run_action(_coord_action(ActionKind.scroll, 500, 350, amount=-300)))[0]
    assert any(e[0] == "wheel" for e in page.mouse.events)

    ok, err = await s.run_action(ActionCommand(action_id="n", session_id="", kind=ActionKind.browser_action, args={"action": "visit_url", "url": "https://example.com"}))
    assert ok and "https://example.com" in page.goto_calls


@pytest.mark.asyncio
async def test_browser_keypress_chord_orders_down_then_up_reversed():
    s, page = _session_with_fake_page()
    await s.run_action(ActionCommand(action_id="k", session_id="", kind=ActionKind.hotkey, args={"keys": ["ctrl", "a"]}))
    assert page.keyboard.events == [("down", "Control"), ("down", "a"), ("up", "a"), ("up", "Control")]


def _browser_config():
    browser = type("Browser", (), {"playwright_headless": True, "viewport_width": 1000, "viewport_height": 700})()
    budgets = type("Budgets", (), {"vision_calls": 3})()
    fara = type("Fara", (), {"base_url": "http://localhost:5000/v1", "api_key": "not-needed", "model_name": "fara", "timeout_seconds": 5})()
    cu = type("CU", (), {"vision_provider": "fara", "vision_fara_model": fara, "browser": browser, "budgets": budgets, "vision_max_image_dimension": 0, "vision_jpeg_quality": 85})()
    return type("AppConfig", (), {"computer_use": cu})()


def _browser_step(requires_vision: bool, **kwargs) -> ComputerUsePlanStep:
    return ComputerUsePlanStep(
        step_id="b1", instruction="open the site", requires_vision=requires_vision, environment="browser",
        risk=PlannedRiskAssessment(level=RiskLevel.low, reasons=["t"], consequences=["t"]), **kwargs,
    )


class _FakeRemoteMin:
    async def get_session(self, session_id):
        return object()


class _FakeSession:
    current_url = "https://example.com"

    def __init__(self):
        self.ran: list[ActionCommand] = []
        self.shots = 0

    async def screenshot(self):
        self.shots += 1
        return b"\x89PNG\r\n\x1a\n"  # not decoded by the fake provider path

    async def run_action(self, action):
        self.ran.append(action)
        return True, None

    async def close(self):
        pass


@pytest.mark.asyncio
async def test_deterministic_browser_step_runs_once_on_session(monkeypatch):
    monkeypatch.setattr("vilagent.computer_use.plan_execute.get_app_config", lambda: _browser_config())
    executor = ComputerUseStepExecutor(remote=_FakeRemoteMin())
    fake = _FakeSession()

    async def _ensure(config):
        executor._browser_session = fake
        return fake

    monkeypatch.setattr(executor, "_ensure_browser_session", _ensure)

    step = _browser_step(False, action_kind=ActionKind.browser_action, args={"action": "visit_url", "url": "https://example.com"})
    _, result = await executor.execute(step, owner=_owner(), session_id="s1", auto_approve_risk_threshold=RiskLevel.critical)

    assert result.status == StepStatus.completed
    assert len(fake.ran) == 1
    assert fake.ran[0].kind == ActionKind.browser_action
    assert fake.ran[0].args["url"] == "https://example.com"


@pytest.mark.asyncio
async def test_vision_browser_step_drives_fara_against_session(monkeypatch):
    monkeypatch.setattr("vilagent.computer_use.plan_execute.get_app_config", lambda: _browser_config())

    terminate = ActionCommand(action_id="t", session_id="", kind=ActionKind.browser_action, args={"action": "terminate", "status": "success"})

    class _FakeProvider:
        def __init__(self, *a, **k):
            self.calls = 0

        async def get_next_action(self, **kwargs):
            assert kwargs["environment"] == "browser"
            self.calls += 1
            # Multi-action: do one click, then declare the step finished.
            if self.calls == 1:
                return _coord_action(ActionKind.click, 100, 200), [{"role": "assistant", "content": "clicked"}]
            return terminate, [{"role": "assistant", "content": "done"}]

    async def _fake_detect(cache, base_url, api_key, default_model, *, detector=None):
        return default_model

    monkeypatch.setattr("vilagent.computer_use.plan_execute.FaraVisionActionProvider", _FakeProvider)
    monkeypatch.setattr("vilagent.computer_use.plan_execute._detect_served_model_name_once", _fake_detect)
    # Avoid PNG decode in encode_image_for_vision by stubbing it.
    monkeypatch.setattr(
        "vilagent.computer_use.plan_execute.encode_image_for_vision",
        lambda image_bytes, **k: ("", "image/png", 1.0),
    )

    executor = ComputerUseStepExecutor(remote=_FakeRemoteMin())
    fake = _FakeSession()

    async def _ensure(config):
        executor._browser_session = fake
        return fake

    monkeypatch.setattr(executor, "_ensure_browser_session", _ensure)

    step = _browser_step(True, action_kind=ActionKind.click)
    _, result = await executor.execute(step, owner=_owner(), session_id="s1", auto_approve_risk_threshold=RiskLevel.critical)

    assert result.status == StepStatus.completed
    assert len(fake.ran) == 1 and fake.ran[0].kind == ActionKind.click
