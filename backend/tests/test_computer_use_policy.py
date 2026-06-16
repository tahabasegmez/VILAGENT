"""Tests for deterministic VILAGENT action policy."""

from __future__ import annotations

from vilagent.computer_use.models import ActionCommand, ActionKind, PolicyDecision, Rect, RiskAssessment, RiskLevel, TargetRef, TargetStrategy
from vilagent.computer_use.policy import DefaultActionPolicy


def _action(level: RiskLevel, threshold: RiskLevel | None = None) -> ActionCommand:
    return ActionCommand(
        action_id=f"action-{level}",
        session_id="session-1",
        kind=ActionKind.hotkey,
        risk=RiskAssessment(level=level, reasons=["test reason"]),
        auto_approve_risk_threshold=threshold,
    )


def test_legacy_action_without_ui_threshold_keeps_low_risk_behavior():
    assert DefaultActionPolicy().evaluate(_action(RiskLevel.low)).decision == PolicyDecision.allow


def test_selected_threshold_auto_approves_risk_through_that_level():
    policy = DefaultActionPolicy()

    assert policy.evaluate(_action(RiskLevel.low, RiskLevel.high)).decision == PolicyDecision.allow
    assert policy.evaluate(_action(RiskLevel.medium, RiskLevel.high)).decision == PolicyDecision.allow
    assert policy.evaluate(_action(RiskLevel.high, RiskLevel.high)).decision == PolicyDecision.allow


def test_risk_above_selected_threshold_requires_approval():
    assert DefaultActionPolicy().evaluate(_action(RiskLevel.critical, RiskLevel.high)).decision == PolicyDecision.require_approval


def test_coordinate_click_obeys_explicit_threshold_when_it_has_a_postcondition():
    action = ActionCommand(
        action_id="physical-click",
        session_id="session-1",
        kind=ActionKind.click,
        target=TargetRef(strategy=TargetStrategy.coordinate, bounds=Rect(x=1, y=1, width=2, height=2), confidence=1, observation_id="obs-1"),
        postconditions=[{"kind": "screen_changed"}],
        auto_approve_risk_threshold=RiskLevel.low,
    )
    assert DefaultActionPolicy().evaluate(action).decision == PolicyDecision.allow


def test_coordinate_click_without_postcondition_requires_approval_when_threshold_is_missing():
    action = ActionCommand(
        action_id="physical-click",
        session_id="session-1",
        kind=ActionKind.click,
        target=TargetRef(strategy=TargetStrategy.coordinate, bounds=Rect(x=1, y=1, width=2, height=2), confidence=1, observation_id="obs-1"),
    )
    assert DefaultActionPolicy().evaluate(action).decision == PolicyDecision.require_approval


def test_browser_action_without_postcondition_obeys_risk_threshold():
    action = ActionCommand(
        action_id="browser-1",
        session_id="session-1",
        kind=ActionKind.browser_action,
        auto_approve_risk_threshold=RiskLevel.low,
    )
    assert DefaultActionPolicy().evaluate(action).decision == PolicyDecision.allow


def test_critical_action_without_threshold_requires_approval_instead_of_policy_denial():
    assert DefaultActionPolicy().evaluate(_action(RiskLevel.critical)).decision == PolicyDecision.require_approval
