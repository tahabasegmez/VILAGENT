"""Deterministic action policy used before any desktop mutation."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from vilagent.computer_use.models import ActionCommand, ActionKind, PolicyDecision, PolicyVerdict, RiskLevel, TargetStrategy


@runtime_checkable
class ActionPolicy(Protocol):
    def evaluate(self, action: ActionCommand) -> PolicyVerdict:
        """Return allow, deny, or require_approval for an action."""
        ...


class DefaultActionPolicy:
    """Risk-based approval policy for the VILAGENT execution plane.

    Postconditions are verification metadata, not an authorization boundary.
    Missing verification metadata must not prevent an otherwise valid action
    from reaching its provider.
    """

    policy_id = "vilagent.ui-risk-threshold.v2"

    def evaluate(self, action: ActionCommand) -> PolicyVerdict:
        reasons = list(action.risk.reasons)
        if action.kind == ActionKind.click and action.target is not None and action.target.strategy == TargetStrategy.coordinate:
            if action.auto_approve_risk_threshold is None:
                return PolicyVerdict(
                    decision=PolicyDecision.require_approval,
                    reasons=reasons or ["Physical coordinate click requires explicit approval."],
                    policy_id=self.policy_id,
                )
        threshold = action.auto_approve_risk_threshold
        if threshold is None:
            if action.risk.level == RiskLevel.critical:
                return PolicyVerdict(
                    decision=PolicyDecision.require_approval,
                    reasons=reasons or ["Critical-risk action requires explicit approval."],
                    policy_id=self.policy_id,
                )
            if action.risk.level == RiskLevel.high:
                return PolicyVerdict(
                    decision=PolicyDecision.require_approval,
                    reasons=reasons or ["High-risk action requires explicit approval."],
                    policy_id=self.policy_id,
                )
            return PolicyVerdict(
                decision=PolicyDecision.allow,
                reasons=reasons,
                policy_id=self.policy_id,
            )
        risk_rank = [RiskLevel.low, RiskLevel.medium, RiskLevel.high, RiskLevel.critical]
        if risk_rank.index(action.risk.level) > risk_rank.index(threshold):
            return PolicyVerdict(
                decision=PolicyDecision.require_approval,
                reasons=reasons or [
                    f"{action.risk.level.value.title()}-risk action exceeds the UI auto-approval threshold."
                ],
                policy_id=self.policy_id,
            )
        return PolicyVerdict(decision=PolicyDecision.allow, reasons=reasons, policy_id=self.policy_id)
