"""Tests for safe browser action construction helpers."""

from __future__ import annotations

import pytest

from vilagent.computer_use.browser_actions import BrowserActionBuildError, build_browser_action
from vilagent.computer_use.models import BrowserStateSummary, TargetRef, TargetStrategy


def test_build_browser_action_adds_required_context_and_default_postcondition():
    target = TargetRef(strategy=TargetStrategy.browser, selector={"css": "#save"}, confidence=1, observation_id="obs-1")

    action = build_browser_action(
        session_id="session-1",
        target=target,
        browser_state=BrowserStateSummary(url="https://example.com", tab_id="tab-1", allowed_domain=True),
        browser_action="click",
        action_id="browser-1",
    )

    assert action.kind == "browser_action"
    assert action.target == target
    assert action.args["url"] == "https://example.com"
    assert action.args["tab_id"] == "tab-1"
    assert action.args["browser_action"] == "click"
    assert action.postconditions[0].kind == "browser_dom"
    assert action.postconditions[0].selector == {"css": "#save"}
    assert action.risk.level == "medium"


def test_build_browser_action_rejects_missing_browser_safety_context():
    target = TargetRef(strategy=TargetStrategy.browser, selector={"css": "#save"}, confidence=1, observation_id="obs-1")

    with pytest.raises(BrowserActionBuildError, match="allowed browser domain"):
        build_browser_action(
            session_id="session-1",
            target=target,
            browser_state=BrowserStateSummary(url="https://evil.test", tab_id="tab-1", allowed_domain=False),
        )

    with pytest.raises(BrowserActionBuildError, match="browser target"):
        build_browser_action(
            session_id="session-1",
            target=target.model_copy(update={"strategy": TargetStrategy.uia}),
            browser_state=BrowserStateSummary(url="https://example.com", tab_id="tab-1", allowed_domain=True),
        )
