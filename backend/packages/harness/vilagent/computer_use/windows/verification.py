"""Semantic Windows UIA condition verification."""

from __future__ import annotations

from typing import Any

from vilagent.computer_use.models import Condition, ConditionOperator, Observation, UIAQuery, VerificationResult


class WindowsUIAVerificationProvider:
    """Verify UIA existence conditions without mutating the desktop."""

    name = "windows-uia-verification"

    def __init__(self, uia_provider: Any):
        self._uia = uia_provider

    async def verify(
        self,
        conditions: list[Condition],
        *,
        before: Observation,
        after: Observation,
    ) -> VerificationResult:
        failed: list[Condition] = []
        for condition in conditions:
            if not await self._verify_condition(condition):
                failed.append(condition)
        return VerificationResult(
            succeeded=not failed,
            checked_conditions=len(conditions),
            failed_conditions=failed,
        )

    async def _verify_condition(self, condition: Condition) -> bool:
        if condition.kind != "uia_element" or condition.operator not in {ConditionOperator.exists, ConditionOperator.not_exists}:
            return False
        try:
            query = UIAQuery.model_validate({**condition.selector, "max_results": 1})
            exists = bool(await self._uia.find(query))
        except Exception:
            return False
        return exists if condition.operator == ConditionOperator.exists else not exists
