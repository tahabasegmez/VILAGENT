"""Cost-aware, fail-closed target resolution across computer-use providers."""

from __future__ import annotations

from collections.abc import Iterable

from vilagent.computer_use.models import (
    Observation,
    TargetQuery,
    TargetResolutionAttempt,
    TargetResolutionOutcome,
    TargetResolutionResult,
    TargetStrategy,
)
from vilagent.computer_use.providers import TargetProvider

DEFAULT_STRATEGY_ORDER = (
    TargetStrategy.app,
    TargetStrategy.browser,
    TargetStrategy.uia,
    TargetStrategy.vision,
    TargetStrategy.coordinate,
)


class TargetResolver:
    """Try providers in cheapest-reliable order and validate every candidate."""

    def __init__(
        self,
        providers: Iterable[TargetProvider],
        *,
        strategy_order: Iterable[TargetStrategy] = DEFAULT_STRATEGY_ORDER,
    ):
        self._providers = list(providers)
        self._strategy_order = tuple(strategy_order)
        if len(set(self._strategy_order)) != len(self._strategy_order):
            raise ValueError("strategy_order must contain unique strategies")

    async def resolve(self, query: TargetQuery, *, observation: Observation) -> TargetResolutionResult:
        attempts: list[TargetResolutionAttempt] = []
        allowed = set(query.allowed_strategies)
        providers_by_strategy = {
            strategy: [provider for provider in self._providers if provider.strategy == strategy]
            for strategy in self._strategy_order
        }

        for strategy in self._strategy_order:
            if strategy not in allowed:
                continue
            for provider in providers_by_strategy[strategy]:
                try:
                    target = await provider.resolve(query, observation=observation)
                except Exception:
                    attempts.append(
                        TargetResolutionAttempt(
                            provider_name=provider.name,
                            strategy=strategy,
                            outcome=TargetResolutionOutcome.error,
                            error_code="target_provider_error",
                        )
                    )
                    continue
                if target is None:
                    attempts.append(
                        TargetResolutionAttempt(
                            provider_name=provider.name,
                            strategy=strategy,
                            outcome=TargetResolutionOutcome.not_found,
                        )
                    )
                    continue

                error_code = self._validate_candidate(
                    target_strategy=target.strategy,
                    provider_strategy=provider.strategy,
                    target_observation_id=target.observation_id,
                    observation_id=observation.observation_id,
                    confidence=target.confidence,
                    minimum_confidence=query.minimum_confidence,
                )
                if error_code is not None:
                    attempts.append(
                        TargetResolutionAttempt(
                            provider_name=provider.name,
                            strategy=strategy,
                            outcome=TargetResolutionOutcome.rejected,
                            confidence=target.confidence,
                            error_code=error_code,
                        )
                    )
                    continue

                attempts.append(
                    TargetResolutionAttempt(
                        provider_name=provider.name,
                        strategy=strategy,
                        outcome=TargetResolutionOutcome.resolved,
                        confidence=target.confidence,
                    )
                )
                return TargetResolutionResult(target=target.model_copy(deep=True), attempts=attempts)

        return TargetResolutionResult(attempts=attempts)

    @staticmethod
    def _validate_candidate(
        *,
        target_strategy: TargetStrategy,
        provider_strategy: TargetStrategy,
        target_observation_id: str,
        observation_id: str,
        confidence: float,
        minimum_confidence: float,
    ) -> str | None:
        if target_strategy != provider_strategy:
            return "target_strategy_mismatch"
        if target_observation_id != observation_id:
            return "stale_target"
        if confidence < minimum_confidence:
            return "target_confidence_below_threshold"
        return None
