"""Tests for the policy-controlled observe-action-verify loop."""

from __future__ import annotations

import asyncio
from collections import deque

from vilagent.computer_use.engine import ComputerUseEngine
from vilagent.computer_use.lease import DesktopLease
from vilagent.computer_use.models import (
    ActionCommand,
    ActionKind,
    ActionStatus,
    ApprovalDecision,
    Condition,
    MonitorRef,
    NativeActionResult,
    Observation,
    Rect,
    RiskAssessment,
    RiskLevel,
    Size,
    TargetRef,
    TargetStrategy,
    VerificationResult,
    WindowRef,
)
from vilagent.computer_use.policy import DefaultActionPolicy


def _observation(observation_id: str, *, previous_id: str | None = None, diff: float | None = None, active_window: WindowRef | None = None) -> Observation:
    return Observation(
        observation_id=observation_id,
        previous_observation_id=previous_id,
        session_id="session-1",
        active_window=active_window,
        monitor=MonitorRef(monitor_id="primary", primary=True, bounds=Rect(x=0, y=0, width=1920, height=1080)),
        screen_size=Size(width=1920, height=1080),
        diff_from_previous=diff,
    )


def _action(*, observation_id: str = "obs-1", risk: RiskLevel = RiskLevel.low) -> ActionCommand:
    return ActionCommand(
        action_id="action-1",
        session_id="session-1",
        kind=ActionKind.click,
        target=TargetRef(strategy=TargetStrategy.uia, selector={"automation_id": "save"}, confidence=1, observation_id=observation_id),
        preconditions=[Condition(kind="window_active")],
        postconditions=[Condition(kind="saved")],
        risk=RiskAssessment(level=risk),
    )


class FakeObservationProvider:
    name = "fake-observation"

    def __init__(self, observations: list[Observation]):
        self.observations = deque(observations)
        self.immediate_synthesized = False

    async def observe(self, session_id: str, *, previous: Observation | None = None) -> Observation:
        if previous is not None and len(self.observations) == 1 and not self.immediate_synthesized:
            self.immediate_synthesized = True
            return previous.model_copy(
                update={
                    "observation_id": f"{previous.observation_id}-immediate",
                    "previous_observation_id": previous.observation_id,
                    "diff_from_previous": 0,
                }
            )
        return self.observations.popleft()


class ExplodingObservationProvider(FakeObservationProvider):
    async def observe(self, session_id: str, *, previous: Observation | None = None) -> Observation:
        raise RuntimeError("observation_unavailable")


class FakeActionProvider:
    name = "fake-action"

    def __init__(self, result: NativeActionResult | None = None):
        self.result = result or NativeActionResult(succeeded=True)
        self.calls = []

    async def execute(self, action: ActionCommand) -> NativeActionResult:
        self.calls.append(action)
        return self.result


class ExplodingActionProvider(FakeActionProvider):
    async def execute(self, action: ActionCommand) -> NativeActionResult:
        raise RuntimeError("native provider unavailable")


class FakeVerificationProvider:
    name = "fake-verification"

    def __init__(self, results: list[bool]):
        self.results = deque(results)

    async def verify(self, conditions, *, before, after) -> VerificationResult:
        succeeded = True if before.observation_id == after.observation_id and len(self.results) == 1 else self.results.popleft()
        return VerificationResult(succeeded=succeeded, checked_conditions=len(conditions), failed_conditions=[] if succeeded else list(conditions))


class FakeApprovalProvider:
    name = "fake-approval"

    def __init__(self, approved: bool):
        self.approved = approved
        self.requests = []

    async def request_approval(self, request):
        self.requests.append(request)
        return ApprovalDecision(approval_id=request.approval_id, approved=self.approved)


def _engine(observations, verification_results, *, action_provider=None, approval_provider=None, policy=None):
    return ComputerUseEngine(
        observation_provider=FakeObservationProvider(observations),
        action_provider=action_provider or FakeActionProvider(),
        verification_provider=FakeVerificationProvider(verification_results),
        policy=policy or DefaultActionPolicy(),
        desktop_lease=DesktopLease(),
        approval_provider=approval_provider,
    )


def test_successful_action_runs_closed_loop():
    async def run():
        provider = FakeActionProvider()
        engine = _engine([_observation("obs-1"), _observation("obs-2", previous_id="obs-1", diff=0.5)], [True, True], action_provider=provider)

        result = await engine.execute(_action(), owner_id="run-1")

        assert result.status == ActionStatus.succeeded
        assert result.after_observation_id == "obs-2"
        assert len(provider.calls) == 1

    asyncio.run(run())


def test_unchanged_fresh_capture_can_validate_target_from_previous_observation():
    async def run():
        engine = _engine([_observation("obs-2", previous_id="obs-1", diff=0), _observation("obs-3", previous_id="obs-2", diff=0.5)], [True, True])

        result = await engine.execute(_action(observation_id="obs-1"), owner_id="run-1")

        assert result.status == ActionStatus.succeeded

    asyncio.run(run())


def test_small_visual_drift_does_not_block_coordinate_click_before_mutation():
    async def run():
        provider = FakeActionProvider()
        action = _action(observation_id="vision-obs")
        action.target = TargetRef(
            strategy=TargetStrategy.coordinate,
            selector={"x": 500, "y": 500},
            confidence=1,
            observation_id="vision-obs",
        )
        engine = _engine(
            [
                _observation("obs-1"),
                _observation("obs-2", previous_id="obs-1", diff=0.002),
                _observation("obs-3", previous_id="obs-2", diff=0.25),
            ],
            [True, True],
            action_provider=provider,
        )
        engine._observation_provider.immediate_synthesized = True

        result = await engine.execute(action, owner_id="run-1")

        assert result.status == ActionStatus.succeeded
        assert len(provider.calls) == 1

    asyncio.run(run())


def test_stale_target_is_blocked_before_native_action():
    async def run():
        provider = FakeActionProvider()
        engine = _engine([_observation("obs-new", previous_id="obs-other", diff=0.5)], [], action_provider=provider)

        result = await engine.execute(_action(observation_id="obs-old"), owner_id="run-1")

        assert result.status == ActionStatus.blocked
        assert result.error is not None and result.error.code == "stale_target"
        assert provider.calls == []

    asyncio.run(run())


def test_desktop_change_after_validation_blocks_immediately_before_mutation():
    async def run():
        provider = FakeActionProvider()
        engine = _engine(
            [
                _observation("obs-1"),
                _observation("obs-2", previous_id="obs-1", diff=0.25),
            ],
            [True],
            action_provider=provider,
        )
        engine._observation_provider.immediate_synthesized = True

        result = await engine.execute(_action(), owner_id="run-1")

        assert result.status == ActionStatus.blocked
        assert result.error is not None and result.error.code == "desktop_changed_before_mutation"
        assert provider.calls == []

    asyncio.run(run())


def test_expected_window_mismatch_blocks_action_before_preconditions_and_mutation():
    async def run():
        provider = FakeActionProvider()
        action = _action()
        action.target.expected_window = WindowRef(window_id="hwnd:1", title="Editor", process_id=42)
        actual = WindowRef(window_id="hwnd:2", title="Browser", process_id=7)
        engine = _engine([_observation("obs-1", active_window=actual)], [], action_provider=provider)

        result = await engine.execute(action, owner_id="run-1")

        assert result.status == ActionStatus.blocked
        assert result.error is not None and result.error.code == "expected_window_mismatch"
        assert result.error.details == {}
        assert provider.calls == []

    asyncio.run(run())


def test_expected_window_match_allows_action():
    async def run():
        expected = WindowRef(window_id="hwnd:1", title="Editor", process_id=42)
        action = _action()
        action.target.expected_window = expected
        engine = _engine([_observation("obs-1", active_window=expected), _observation("obs-2")], [True, True])

        result = await engine.execute(action, owner_id="run-1")

        assert result.status == ActionStatus.succeeded

    asyncio.run(run())


def test_engine_does_not_gate_on_approval():
    """Approval/policy gating moved to the action-lifecycle service.

    The engine is the low-level executor and is only reached after the service
    has approved the action (``submit_action`` -> ``awaiting_approval`` ->
    ``execute_action``), so it must not request approval or block on risk itself.
    Service-layer approval is covered by the router/action-store tests.
    """

    async def run():
        provider = FakeActionProvider()
        approval = FakeApprovalProvider(approved=False)
        engine = _engine([_observation("obs-1"), _observation("obs-2")], [True, True], action_provider=provider, approval_provider=approval)

        result = await engine.execute(_action(risk=RiskLevel.high), owner_id="run-1")

        assert result.status == ActionStatus.succeeded
        assert approval.requests == []  # engine never consults the approval provider
        assert len(provider.calls) == 1

    asyncio.run(run())


def test_failed_postcondition_returns_uncertain():
    async def run():
        engine = _engine([_observation("obs-1"), _observation("obs-2")], [True, False])

        result = await engine.execute(_action(), owner_id="run-1")

        assert result.status == ActionStatus.uncertain
        assert result.error is not None and result.error.code == "postcondition_failed"

    asyncio.run(run())


def test_action_provider_exception_returns_structured_failure_and_releases_lease():
    async def run():
        lease = DesktopLease()
        engine = ComputerUseEngine(
            observation_provider=FakeObservationProvider([_observation("obs-1")]),
            action_provider=ExplodingActionProvider(),
            verification_provider=FakeVerificationProvider([True]),
            policy=DefaultActionPolicy(),
            desktop_lease=lease,
        )

        result = await engine.execute(_action(), owner_id="run-1")

        assert result.status == ActionStatus.failed
        assert result.error is not None and result.error.code == "action_provider_error"
        assert (await lease.snapshot()).owner_id is None

    asyncio.run(run())


def test_targetless_launch_app_does_not_require_observation():
    async def run():
        provider = FakeActionProvider()
        lease = DesktopLease()
        engine = ComputerUseEngine(
            observation_provider=ExplodingObservationProvider([]),
            action_provider=provider,
            verification_provider=FakeVerificationProvider([]),
            policy=DefaultActionPolicy(),
            desktop_lease=lease,
        )
        action = ActionCommand(
            action_id="action-1",
            session_id="session-1",
            kind=ActionKind.launch_app,
            args={"app_name": "calculator"},
        )

        result = await engine.execute(action, owner_id="run-1")

        assert result.status == ActionStatus.succeeded
        assert result.before_observation_id == "not-required"
        assert provider.calls == [action]
        assert (await lease.snapshot()).owner_id is None

    asyncio.run(run())
