"""A run's pending approvals: ask the operator, wait for the answer (or the timeout), report it.

Waiting pauses the run's duration budget and marks the run ``awaiting_approval``. No answer in
time is a "no". Cancelling the run (stop, cancel) resolves the request as declined.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal

from vilagent.runs.events import EventBus

if TYPE_CHECKING:
    from vilagent.runs.budget import BudgetMeter

Scope = Literal["once", "step"]
DECLINED = "The operator declined."


@dataclass(frozen=True)
class Answer:
    approve: bool
    scope: Scope = "once"
    reason: str = ""


class Approvals:
    def __init__(self, events: EventBus, *, timeout_seconds: float, budget: BudgetMeter | None = None, on_waiting: Callable[[bool], None] | None = None):
        self._events = events
        self._timeout = timeout_seconds
        self._budget = budget
        self._on_waiting = on_waiting
        self._pending: dict[str, asyncio.Future[Answer]] = {}
        self._grants: set[tuple[str | None, str]] = set()
        # The plan step now running; "allow for this step" grants are scoped to it.
        self.current_step: str | None = None
        # For the run record: how often the operator was asked, and said no (Phase 11 measures friction).
        self.asked = 0
        self.declined = 0

    async def ask(self, *, kind: Literal["step", "action"], title: str, reasons: list[str], level: str | None = None) -> Answer:
        self.asked += 1
        approval_id = uuid.uuid4().hex[:12]
        future: asyncio.Future[Answer] = asyncio.get_running_loop().create_future()
        self._pending[approval_id] = future
        expires_at = datetime.now(UTC) + timedelta(seconds=self._timeout)
        self._events.publish(
            "approval.requested",
            {"id": approval_id, "kind": kind, "title": title, "reasons": reasons, "level": level, "step_id": self.current_step, "expires_at": expires_at.isoformat()},
        )
        self._waiting(True)
        answer = Answer(False, reason="The run was stopped.")
        try:
            answer = await asyncio.wait_for(future, self._timeout)
        except TimeoutError:
            answer = Answer(False, reason=f"No answer within {self._timeout:.0f} s.")
        finally:
            self.declined += not answer.approve
            self._pending.pop(approval_id, None)
            self._waiting(False)
            self._events.publish("approval.resolved", {"id": approval_id, "approve": answer.approve, "reason": answer.reason})
        return answer

    def answer(self, approval_id: str, *, approve: bool, scope: Scope = "once") -> bool:
        """Deliver the operator's answer; False if nothing with that id is waiting."""
        future = self._pending.get(approval_id)
        if future is None or future.done():
            return False
        future.set_result(Answer(approve, scope, "" if approve else DECLINED))
        return True

    @property
    def pending(self) -> list[str]:
        """Ids of the requests waiting for an answer."""
        return list(self._pending)

    def granted(self, rule: str) -> bool:
        return (self.current_step, rule) in self._grants

    def grant(self, rule: str) -> None:
        self._grants.add((self.current_step, rule))

    def _waiting(self, waiting: bool) -> None:
        if self._budget is not None:
            (self._budget.pause if waiting else self._budget.resume)()
        if self._on_waiting is not None:
            self._on_waiting(waiting)
