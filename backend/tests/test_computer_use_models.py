"""Tests for typed VILAGENT computer-use domain contracts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from vilagent.computer_use.models import (
    ActionCommand,
    ActionKind,
    ActionLifecycleRecord,
    ActionLifecycleStatus,
    ActionOwner,
    ApprovalLifecycleStatus,
    ApprovalRecord,
    MonitorRef,
    Point,
    Rect,
    RiskAssessment,
    RiskLevel,
    Size,
    TargetQuery,
    TargetStrategy,
    action_fingerprint,
    action_intent_fingerprint,
)


def test_rect_contains_uses_exclusive_right_and_bottom_edges():
    rect = Rect(x=10, y=20, width=100, height=50)

    assert rect.contains(Point(x=10, y=20))
    assert rect.contains(Point(x=109, y=69))
    assert not rect.contains(Point(x=110, y=69))
    assert not rect.contains(Point(x=109, y=70))


def test_targeted_action_requires_target():
    with pytest.raises(ValidationError, match="requires a target"):
        ActionCommand(action_id="a1", session_id="s1", kind=ActionKind.click)


def test_non_target_action_is_valid():
    action = ActionCommand(
        action_id="a1",
        session_id="s1",
        kind=ActionKind.hotkey,
        args={"keys": ["CTRL", "L"]},
        risk=RiskAssessment(level=RiskLevel.low),
    )

    assert action.target is None


def test_targetless_text_entry_is_valid():
    action = ActionCommand(
        action_id="a1",
        session_id="s1",
        kind=ActionKind.type_text,
        args={"text": "value"},
    )

    assert action.target is None


def test_action_rejects_empty_idempotency_key():
    with pytest.raises(ValidationError):
        ActionCommand(action_id="a1", session_id="s1", kind=ActionKind.hotkey, idempotency_key="")


def test_monitor_and_size_reject_invalid_dimensions():
    with pytest.raises(ValidationError):
        MonitorRef(monitor_id="primary", bounds=Rect(x=0, y=0, width=1, height=1), dpi_scale=0)
    with pytest.raises(ValidationError):
        Size(width=0, height=1080)


def test_target_query_disables_coordinate_by_default_and_rejects_duplicate_strategies():
    query = TargetQuery(description="Save button")

    assert TargetStrategy.coordinate not in query.allowed_strategies
    with pytest.raises(ValidationError, match="must be unique"):
        TargetQuery(description="Save button", allowed_strategies=[TargetStrategy.uia, TargetStrategy.uia])


def test_action_fingerprint_is_stable_and_detects_payload_changes():
    action = ActionCommand(action_id="a1", session_id="s1", kind=ActionKind.hotkey, args={"keys": ["CTRL", "L"]})
    same = action.model_copy(deep=True)
    changed = action.model_copy(deep=True, update={"args": {"keys": ["CTRL", "R"]}})

    assert action_fingerprint(action) == action_fingerprint(same)
    assert action_fingerprint(action) != action_fingerprint(changed)


def test_action_intent_fingerprint_ignores_retry_identity_but_detects_semantic_changes():
    action = ActionCommand(action_id="a1", session_id="s1", kind=ActionKind.hotkey, args={"keys": ["CTRL", "L"]}, idempotency_key="retry-1")
    retry = action.model_copy(update={"action_id": "a2", "idempotency_key": "retry-2"})
    changed = action.model_copy(deep=True, update={"args": {"keys": ["CTRL", "R"]}})

    assert action_intent_fingerprint(action) == action_intent_fingerprint(retry)
    assert action_intent_fingerprint(action) != action_intent_fingerprint(changed)


def test_action_lifecycle_record_requires_matching_immutable_snapshot():
    action = ActionCommand(action_id="a1", session_id="s1", kind=ActionKind.hotkey)
    owner = ActionOwner(thread_id="thread-1", run_id="run-1", agent_id="agent-1")
    record = ActionLifecycleRecord(action=action, owner=owner, action_fingerprint=action_fingerprint(action))

    assert record.status == ActionLifecycleStatus.pending
    with pytest.raises(ValidationError, match="immutable fingerprint"):
        ActionLifecycleRecord(action=action, owner=owner, action_fingerprint="0" * 64)


def test_terminal_action_lifecycle_requires_completed_at():
    action = ActionCommand(action_id="a1", session_id="s1", kind=ActionKind.hotkey)
    owner = ActionOwner(thread_id="thread-1", run_id="run-1", agent_id="agent-1")

    with pytest.raises(ValidationError, match="completed_at"):
        ActionLifecycleRecord(
            action=action,
            owner=owner,
            status=ActionLifecycleStatus.cancelled,
            action_fingerprint=action_fingerprint(action),
        )


def test_approval_record_requires_expiration_and_consistent_decision_state():
    now = datetime.now(UTC)
    owner = ActionOwner(thread_id="thread-1", run_id="run-1", agent_id="agent-1")
    approval = ApprovalRecord(
        approval_id="approval-1",
        action_id="action-1",
        session_id="session-1",
        owner=owner,
        action_fingerprint="a" * 64,
        expires_at=now + timedelta(minutes=5),
    )

    assert approval.status == ApprovalLifecycleStatus.pending
    with pytest.raises(ValidationError, match="requires decided_at"):
        ApprovalRecord.model_validate({**approval.model_dump(), "status": ApprovalLifecycleStatus.approved})
