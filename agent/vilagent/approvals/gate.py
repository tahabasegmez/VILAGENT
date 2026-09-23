"""The action gate that asks the operator before an action a rule flags (``Control.perform`` calls it)."""

from __future__ import annotations

from vilagent.actions import Action
from vilagent.approvals.policy import matched_rule
from vilagent.config.computer_use_config import ComputerUseApprovalConfig
from vilagent.runs.session import current_session

DENIED = "denied by the operator"


class ApprovalGate:
    def __init__(self, rules: ComputerUseApprovalConfig):
        self._rules = rules

    async def check(self, action: Action, environment: str) -> str | None:
        session = current_session()
        approvals = session.approvals if session is not None else None
        if approvals is None:
            return None  # outside a run nobody can be asked, and nothing runs outside a run
        rule = matched_rule(action, self._rules)
        if rule is None or approvals.granted(rule):
            return None
        title = action.thought or f"{action.kind.value} in the {environment} environment"
        answer = await approvals.ask(kind="action", title=title, reasons=[rule])
        if not answer.approve:
            return DENIED
        if answer.scope == "step":
            approvals.grant(rule)
        return None
