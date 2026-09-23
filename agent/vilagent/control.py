"""Run control: every desktop/browser action passes through :meth:`Control.perform`.

That single choke point is where the emergency stop is enforced, where an
:class:`ActionGate` can veto an action, and where each action is written to the
action log. Approvals (ask the operator before a risky action) belong in a gate.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from vilagent.actions import Action, ActionOutcome
from vilagent.runs.budget import charge

logger = logging.getLogger(__name__)

EMERGENCY_STOP = "emergency_stop_engaged"
ACTION_BLOCKED = "action_blocked"


class RunStopped(Exception):
    """The emergency stop cancelled a running task."""


class ActionGate(Protocol):
    async def check(self, action: Action, environment: str) -> str | None:
        """Return a reason to block the action, or None to let it run."""


class AllowAll:
    async def check(self, action: Action, environment: str) -> str | None:
        return None


class ActionLog:
    """Append-only JSONL record of every attempted action (no screenshots or text)."""

    def __init__(self, path: str | Path | None):
        self._path = Path(path) if path else None

    def append(self, environment: str, action: Action, outcome: ActionOutcome) -> None:
        if self._path is None:
            return
        record = {
            "at": datetime.now(UTC).isoformat(),
            "environment": environment,
            "kind": action.kind.value,
            "arg_keys": sorted(action.args),
            "ok": outcome.ok,
            "error": outcome.error,
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record) + "\n")
        except OSError:
            logger.warning("Could not write the action log", exc_info=True)


class Control:
    """Emergency stop + action gate + action log, shared by all runs."""

    def __init__(self, *, gate: ActionGate | None = None, log: ActionLog | None = None):
        self._gate = gate or AllowAll()
        self._log = log or ActionLog(None)
        self._stop_reason: str | None = None
        self._runs: set[asyncio.Task] = set()
        self._on_stop: list[Callable[[], Awaitable[None]]] = []

    @property
    def stopped(self) -> bool:
        return self._stop_reason is not None

    @property
    def stop_reason(self) -> str | None:
        return self._stop_reason

    def on_stop(self, callback: Callable[[], Awaitable[None]]) -> None:
        """Register cleanup to run when the emergency stop engages (e.g. close the browser)."""
        self._on_stop.append(callback)

    async def engage(self, reason: str = "Emergency stop") -> None:
        """Block further actions and cancel every running task."""
        self._stop_reason = reason
        for task in list(self._runs):
            task.cancel()
        for callback in self._on_stop:
            try:
                await callback()
            except Exception:
                logger.warning("Emergency-stop cleanup failed", exc_info=True)

    def reset(self) -> None:
        self._stop_reason = None

    async def run(self, coro: Awaitable):
        """Run one agent task so the emergency stop can cancel it (raises :class:`RunStopped`)."""
        task = asyncio.ensure_future(coro)
        self._runs.add(task)
        try:
            return await task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if task.cancelled() and not (current and current.cancelling()):
                raise RunStopped(self._stop_reason or "Emergency stop") from None
            raise
        finally:
            self._runs.discard(task)

    async def perform(self, environment: str, action: Action, execute: Callable[[Action], Awaitable[ActionOutcome]]) -> ActionOutcome:
        if self.stopped:
            outcome = ActionOutcome.failure(EMERGENCY_STOP)
        elif reason := await self._gate.check(action, environment):
            outcome = ActionOutcome.failure(f"{ACTION_BLOCKED}: {reason}")
        else:
            charge("action")
            try:
                outcome = await execute(action)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("%s action %s failed", environment, action.kind.value, exc_info=True)
                outcome = ActionOutcome.failure(f"action_error: {exc.__class__.__name__}")
        self._log.append(environment, action, outcome)
        return outcome
