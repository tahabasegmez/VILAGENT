"""Tests for owner-scoped browser and DOM contracts."""

from __future__ import annotations

import asyncio

import pytest

from vilagent.computer_use.browser import BrowserActionProvider, BrowserHealth, BrowserPolicyError, BrowserRuntimeActionProvider, BrowserRuntimeDOMTargetProvider, BrowserStateObservationProvider, BrowserStateVerificationProvider, BrowserDOMTargetProvider, BrowserDomainPolicy, BrowserSessionOwnershipError, BrowserSessionRegistry, BrowserTabLifecycleService
from vilagent.computer_use.models import ActionCommand, ActionKind, ActionOwner, BrowserStateSummary, Condition, ConditionOperator, MonitorRef, Observation, Rect, Size, TargetQuery, TargetRef, TargetStrategy


def _observation(url="https://mail.example.com/inbox"):
    return Observation(
        observation_id="obs-1", session_id="session-1", browser_state=BrowserStateSummary(url=url, tab_id="tab-1"),
        monitor=MonitorRef(monitor_id="primary", bounds=Rect(x=0, y=0, width=10, height=10)), screen_size=Size(width=10, height=10),
    )


def test_domain_policy_allows_exact_and_subdomain_but_blocks_suffix_attack():
    policy = BrowserDomainPolicy(["example.com"])
    assert policy.allows("https://example.com")
    assert policy.allows("https://mail.example.com/inbox")
    assert not policy.allows("https://example.com.evil.test")


def test_browser_session_registry_requires_exact_owner():
    async def run():
        registry = BrowserSessionRegistry()
        owner = ActionOwner(thread_id="t", run_id="r", agent_id="a")
        await registry.bind("tab-1", owner)
        await registry.assert_owner("tab-1", owner)
        with pytest.raises(BrowserSessionOwnershipError):
            await registry.assert_owner("tab-1", ActionOwner(thread_id="t", run_id="r", agent_id="other"))
    asyncio.run(run())


def test_dom_target_and_action_are_domain_gated():
    async def run():
        policy = BrowserDomainPolicy(["example.com"])
        target_provider = BrowserDOMTargetProvider(lambda tab, hints: asyncio.sleep(0, result={"css": "#save"}), policy)
        target = await target_provider.resolve(TargetQuery(description="Save", selector_hints={"text": "Save"}), observation=_observation())
        blocked = await target_provider.resolve(TargetQuery(description="Save"), observation=_observation("https://evil.test"))
        calls = []
        action_provider = BrowserActionProvider(lambda tab, selector, args: asyncio.sleep(0, result=not calls.append((tab, selector))), policy)
        result = await action_provider.execute(ActionCommand(
            action_id="browser-1", session_id="session-1", kind=ActionKind.browser_action, target=target,
            args={"url": "https://mail.example.com/inbox", "tab_id": "tab-1"},
        ))
        assert target is not None and target.strategy == TargetStrategy.browser
        assert blocked is None
        assert result.succeeded is True
        assert calls == [("tab-1", {"css": "#save"})]
    asyncio.run(run())


def test_browser_action_blocks_disallowed_domain_without_executor_call():
    async def run():
        calls = []
        provider = BrowserActionProvider(lambda tab, selector, args: asyncio.sleep(0, result=not calls.append(tab)), BrowserDomainPolicy(["example.com"]))
        result = await provider.execute(ActionCommand(
            action_id="browser-1", session_id="session-1", kind=ActionKind.browser_action,
            target=TargetRef(strategy=TargetStrategy.browser, selector={"css": "#save"}, confidence=1, observation_id="obs-1"),
            args={"url": "https://evil.test", "tab_id": "tab-1"},
        ))
        assert result.error_code == "browser_domain_blocked"
        assert calls == []
    asyncio.run(run())


class FakeBrowserRuntime:
    def __init__(self):
        self.executed = []
        self.closed = []

    async def health(self):
        return BrowserHealth(enabled=True, healthy=True)

    async def create_session(self, url):
        return BrowserStateSummary(url=url, title="Created", tab_id="created-tab")

    async def close_session(self, browser_session_id):
        self.closed.append(browser_session_id)

    async def observe(self, browser_session_id):
        return BrowserStateSummary(url="https://mail.example.com/inbox", title="Inbox", tab_id=browser_session_id)

    async def resolve_dom(self, browser_session_id, hints):
        return {"css": "#save", "text": hints.get("text")}

    async def execute(self, browser_session_id, selector, args):
        self.executed.append((browser_session_id, selector, args))
        return True

    async def query_state(self, browser_session_id, selector):
        return {"exists": selector.get("css") == "#save"}


def test_runtime_adapter_attaches_state_resolves_executes_and_verifies():
    async def run():
        runtime = FakeBrowserRuntime()
        policy = BrowserDomainPolicy(["example.com"])
        observation = await BrowserStateObservationProvider(runtime, policy).attach(_observation(), "tab-1")
        target = await BrowserRuntimeDOMTargetProvider(runtime, policy).resolve(TargetQuery(description="Save", selector_hints={"text": "Save"}), observation=observation)
        action_provider = BrowserRuntimeActionProvider(runtime, policy)
        result = await action_provider.execute(ActionCommand(
            action_id="browser-1", session_id="session-1", kind=ActionKind.browser_action, target=target,
            args={"url": observation.browser_state.url, "tab_id": observation.browser_state.tab_id},
            postconditions=[Condition(kind="browser_dom", operator=ConditionOperator.exists, selector={"css": "#save"})],
        ))
        verification = await BrowserStateVerificationProvider(runtime, policy).verify(
            [Condition(kind="browser_allowed_domain", operator=ConditionOperator.exists), Condition(kind="browser_dom", operator=ConditionOperator.exists, selector={"css": "#save"})],
            before=observation,
            after=observation,
        )
        assert observation.browser_state.allowed_domain is True
        assert target.selector["css"] == "#save"
        assert result.succeeded is True
        assert verification.succeeded is True
    asyncio.run(run())


def test_tab_lifecycle_is_owner_scoped_and_domain_gated():
    async def run():
        runtime = FakeBrowserRuntime()
        owner = ActionOwner(thread_id="t", run_id="r", agent_id="a")
        other = ActionOwner(thread_id="t", run_id="r", agent_id="other")
        service = BrowserTabLifecycleService(runtime, BrowserSessionRegistry(), BrowserDomainPolicy(["example.com"]))
        state = await service.create("https://example.com", owner)
        assert state.allowed_domain is True
        assert await service.list(owner) == ["created-tab"]
        with pytest.raises(BrowserSessionOwnershipError):
            await service.close("created-tab", other)
        await service.close("created-tab", owner)
        assert runtime.closed == ["created-tab"]
        with pytest.raises(BrowserPolicyError):
            await service.create("https://evil.test", owner)
    asyncio.run(run())
