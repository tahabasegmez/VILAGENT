"""Fail-closed baseline verification for computer-use actions."""

from __future__ import annotations

from vilagent.computer_use.models import Condition, Observation, VerificationResult
from vilagent.computer_use.providers import VerificationProvider


class ConservativeVerificationProvider:
    """Verify only simple screen conditions and reject unknown conditions."""

    name = "conservative-verification"

    async def verify(
        self,
        conditions: list[Condition],
        *,
        before: Observation,
        after: Observation,
    ) -> VerificationResult:
        failed = [condition for condition in conditions if not self._verify_condition(condition, before=before, after=after)]
        return VerificationResult(
            succeeded=not failed,
            checked_conditions=len(conditions),
            failed_conditions=failed,
        )

    @staticmethod
    def _verify_condition(condition: Condition, *, before: Observation, after: Observation) -> bool:
        if condition.kind == "screen_changed":
            return after.observation_id != before.observation_id and bool(after.diff_from_previous and after.diff_from_previous > 0)
        if condition.kind == "screen_unchanged":
            return after.observation_id != before.observation_id and after.diff_from_previous == 0
        return False


class RoutedVerificationProvider:
    """Route known condition kinds and fail closed for everything else."""

    name = "routed-verification"

    def __init__(self, routes: dict[str, VerificationProvider]):
        self._routes = dict(routes)

    async def verify(
        self,
        conditions: list[Condition],
        *,
        before: Observation,
        after: Observation,
    ) -> VerificationResult:
        failed: list[Condition] = []
        details: dict[str, object] = {}
        grouped: dict[int, tuple[VerificationProvider, list[Condition]]] = {}
        for condition in conditions:
            provider = self._routes.get(condition.kind)
            if provider is None:
                failed.append(condition)
                continue
            grouped.setdefault(id(provider), (provider, []))[1].append(condition)

        for provider, provider_conditions in grouped.values():
            try:
                result = await provider.verify(provider_conditions, before=before, after=after)
            except Exception:
                failed.extend(provider_conditions)
                details[provider.name] = {"error_code": "verification_provider_error"}
                continue
            failed.extend(result.failed_conditions)
            if result.details:
                details[provider.name] = result.details

        return VerificationResult(
            succeeded=not failed,
            checked_conditions=len(conditions),
            failed_conditions=failed,
            details=details,
        )
