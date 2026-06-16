"""Helpers for constructing safe browser action submissions."""

from __future__ import annotations

import uuid
from typing import Any

from vilagent.computer_use.models import (
    ActionCommand,
    ActionKind,
    BrowserStateSummary,
    Condition,
    ConditionOperator,
    RiskAssessment,
    RiskLevel,
    TargetRef,
    TargetStrategy,
)


class BrowserActionBuildError(ValueError):
    pass


def build_browser_action(
    *,
    session_id: str,
    target: TargetRef,
    browser_state: BrowserStateSummary,
    browser_action: str = "click",
    action_id: str | None = None,
    args: dict[str, Any] | None = None,
    postconditions: list[Condition] | None = None,
    idempotency_key: str | None = None,
    timeout_seconds: float = 30,
) -> ActionCommand:
    """Build a browser action that cannot omit tab/url/postcondition context."""

    if target.strategy != TargetStrategy.browser:
        raise BrowserActionBuildError("Browser action requires a browser target")
    if browser_state.tab_id is None or not browser_state.tab_id:
        raise BrowserActionBuildError("Browser action requires a browser tab_id")
    if browser_state.url is None or not browser_state.url:
        raise BrowserActionBuildError("Browser action requires a browser URL")
    if browser_state.allowed_domain is not True:
        raise BrowserActionBuildError("Browser action requires an allowed browser domain")
    resolved_postconditions = list(postconditions or [])
    if not resolved_postconditions:
        resolved_postconditions = [
            Condition(kind="browser_dom", operator=ConditionOperator.exists, selector=dict(target.selector))
        ]
    merged_args = dict(args or {})
    merged_args.setdefault("browser_action", browser_action)
    merged_args["tab_id"] = browser_state.tab_id
    merged_args["url"] = browser_state.url
    return ActionCommand(
        action_id=action_id or f"browser-{uuid.uuid4().hex}",
        session_id=session_id,
        kind=ActionKind.browser_action,
        target=target,
        args=merged_args,
        postconditions=resolved_postconditions,
        risk=RiskAssessment(
            level=RiskLevel.medium,
            reasons=["Browser DOM action created from an owned browser target."],
            consequences=["May mutate the current browser tab."],
        ),
        idempotency_key=idempotency_key,
        timeout_seconds=timeout_seconds,
    )
