"""Tests for restart-safe action lifecycle persistence."""

from __future__ import annotations

import asyncio
import json
import hashlib

import pytest

from vilagent.computer_use.action_store import ActionStorePersistenceError, JsonFileActionStore
from vilagent.computer_use.models import ActionCommand, ActionKind, ActionLifecycleStatus, ActionOwner, Rect, RiskAssessment, RiskLevel, TargetRef, TargetStrategy, action_fingerprint


def _owner():
    return ActionOwner(thread_id="thread-1", run_id="run-1", agent_id="agent-1")


def _action(action_id="action-1", *, idempotency_key="retry-1"):
    return ActionCommand(
        action_id=action_id,
        session_id="session-1",
        kind=ActionKind.hotkey,
        args={"keys": ["CTRL", "L"]},
        risk=RiskAssessment(level=RiskLevel.high),
        idempotency_key=idempotency_key,
    )


def test_persistent_store_restores_actions_approvals_events_and_idempotency(tmp_path):
    async def run():
        path = tmp_path / "lifecycle.json"
        first = JsonFileActionStore(path)
        await first.initialize()
        await first.submit(_action(), owner=_owner())
        approval = await first.request_approval("action-1", owner=_owner(), reasons=["dangerous"])

        restored = JsonFileActionStore(path)
        await restored.initialize()
        action = await restored.get_action("action-1", owner=_owner())
        restored_approval = await restored.get_approval(approval.approval_id, owner=_owner())
        retry = await restored.submit(_action("action-2"), owner=_owner())
        events = await restored.events.list(owner=_owner())

        assert action.status == ActionLifecycleStatus.awaiting_approval
        assert restored_approval.approval_id == approval.approval_id
        assert retry.action.action_id == "action-1"
        assert [event.sequence for event in events] == [1, 2, 3]

    asyncio.run(run())


def test_restart_reconciles_executing_action_to_uncertain_without_reexecution(tmp_path):
    async def run():
        path = tmp_path / "lifecycle.json"
        first = JsonFileActionStore(path)
        await first.initialize()
        await first.submit(_action(), owner=_owner())
        await first.transition("action-1", ActionLifecycleStatus.approved, owner=_owner())
        await first.transition("action-1", ActionLifecycleStatus.executing, owner=_owner())

        restored = JsonFileActionStore(path)
        await restored.initialize()
        action = await restored.get_action("action-1", owner=_owner())
        events = await restored.events.list(owner=_owner())

        assert action.status == ActionLifecycleStatus.uncertain
        assert action.error is not None and action.error.code == "host_restart_during_execution"
        assert events[-1].action_status == ActionLifecycleStatus.uncertain
        assert events[-1].sequence == 4

    asyncio.run(run())


def test_restart_preserves_approved_physical_action_and_reconciles_executing_snapshot(tmp_path):
    async def run():
        path = tmp_path / "physical-lifecycle.json"
        action = ActionCommand(
            action_id="physical-1",
            session_id="session-1",
            kind=ActionKind.click,
            target=TargetRef(strategy=TargetStrategy.coordinate, bounds=Rect(x=1, y=2, width=3, height=4), confidence=1, observation_id="obs-1"),
        )
        first = JsonFileActionStore(path)
        await first.initialize()
        await first.submit(action, owner=_owner())
        await first.transition(action.action_id, ActionLifecycleStatus.approved, owner=_owner())

        approved_restart = JsonFileActionStore(path)
        await approved_restart.initialize()
        approved = await approved_restart.get_action(action.action_id, owner=_owner())
        assert approved.status == ActionLifecycleStatus.approved
        assert approved.action_fingerprint == action_fingerprint(action)

        await approved_restart.transition(action.action_id, ActionLifecycleStatus.executing, owner=_owner())
        executing_restart = JsonFileActionStore(path)
        await executing_restart.initialize()
        uncertain = await executing_restart.get_action(action.action_id, owner=_owner())
        assert uncertain.status == ActionLifecycleStatus.uncertain
        assert uncertain.owner == _owner()
        assert uncertain.action == action

    asyncio.run(run())


def test_corrupt_or_unsupported_snapshot_fails_closed(tmp_path):
    async def run():
        corrupt = tmp_path / "corrupt.json"
        corrupt.write_text("{not-json", encoding="utf-8")
        with pytest.raises(ActionStorePersistenceError, match="Unable to load"):
            await JsonFileActionStore(corrupt).initialize()

        unsupported = tmp_path / "unsupported.json"
        unsupported.write_text(json.dumps({"version": 2}), encoding="utf-8")
        with pytest.raises(ActionStorePersistenceError, match="Unable to load"):
            await JsonFileActionStore(unsupported).initialize()

    asyncio.run(run())


def test_snapshot_created_before_optional_ui_threshold_field_still_loads(tmp_path):
    async def run():
        path = tmp_path / "legacy-lifecycle.json"
        first = JsonFileActionStore(path)
        await first.initialize()
        await first.submit(_action(), owner=_owner())

        payload = json.loads(path.read_text(encoding="utf-8"))
        action_payload = payload["actions"][0]["action"]
        action_payload.pop("auto_approve_risk_threshold", None)
        encoded = json.dumps(action_payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        payload["actions"][0]["action_fingerprint"] = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        path.write_text(json.dumps(payload), encoding="utf-8")

        restored = JsonFileActionStore(path)
        await restored.initialize()
        action = await restored.get_action("action-1", owner=_owner())

        assert action.action.auto_approve_risk_threshold is None

    asyncio.run(run())


def test_snapshot_with_broken_approval_reference_fails_closed(tmp_path):
    async def run():
        path = tmp_path / "lifecycle.json"
        first = JsonFileActionStore(path)
        await first.initialize()
        await first.submit(_action(), owner=_owner())
        await first.request_approval("action-1", owner=_owner())

        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["approvals"][0]["action_id"] = "missing-action"
        path.write_text(json.dumps(payload), encoding="utf-8")

        with pytest.raises(ActionStorePersistenceError, match="invalid action approval reference"):
            await JsonFileActionStore(path).initialize()

    asyncio.run(run())


def test_persistence_failure_is_reported_fail_closed(tmp_path, monkeypatch):
    async def run():
        store = JsonFileActionStore(tmp_path / "lifecycle.json")
        await store.initialize()

        def fail_write(snapshot):
            raise OSError("disk unavailable")

        monkeypatch.setattr(store, "_write_snapshot_sync", fail_write)
        with pytest.raises(ActionStorePersistenceError, match="Unable to persist"):
            await store.submit(_action(), owner=_owner())

    asyncio.run(run())
