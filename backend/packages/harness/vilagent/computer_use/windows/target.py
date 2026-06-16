"""Read-only Windows UIA target resolution."""

from __future__ import annotations

from typing import Any

from vilagent.computer_use.models import Observation, TargetQuery, TargetRef, TargetStrategy, UIAElementRef, UIAQuery

_UIA_QUERY_FIELDS = frozenset({"automation_id", "name", "control_type", "process_id", "window_title"})


class WindowsUIATargetProvider:
    """Resolve one unambiguous, stable semantic UIA target."""

    name = "windows-uia-target"
    strategy = TargetStrategy.uia

    def __init__(self, uia_provider: Any):
        self._uia = uia_provider

    async def resolve(self, query: TargetQuery, *, observation: Observation) -> TargetRef | None:
        selector_hints = {key: value for key, value in query.selector_hints.items() if key in _UIA_QUERY_FIELDS}
        if not selector_hints:
            selector_hints["name"] = query.description
        matches = await self._uia.find(UIAQuery(**selector_hints, max_results=2))
        if len(matches) != 1:
            return None
        element = matches[0]
        if element.visible is False or element.enabled is False:
            return None
        return TargetRef(
            strategy=self.strategy,
            selector=self._stable_selector(element),
            bounds=element.bounds,
            confidence=self._confidence(element),
            observation_id=observation.observation_id,
        )

    @staticmethod
    def _stable_selector(element: UIAElementRef) -> dict[str, Any]:
        selector: dict[str, Any] = {"element_id": element.element_id}
        if element.process_id is not None:
            selector["process_id"] = element.process_id
        if element.automation_id:
            selector["automation_id"] = element.automation_id
        if element.control_type:
            selector["control_type"] = element.control_type
        return selector

    @staticmethod
    def _confidence(element: UIAElementRef) -> float:
        return 0.99 if element.automation_id else 0.95
