"""Owner-scoped browser-use adapter contracts and domain policy."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit

from pydantic import BaseModel

from vilagent.computer_use.models import ActionCommand, ActionKind, ActionOwner, BrowserStateSummary, Condition, ConditionOperator, NativeActionResult, Observation, TargetQuery, TargetRef, TargetStrategy, VerificationResult


class BrowserPolicyError(PermissionError):
    pass


class BrowserSessionOwnershipError(PermissionError):
    pass


class BrowserUnavailableError(RuntimeError):
    pass


class BrowserHealth(BaseModel):
    enabled: bool
    healthy: bool
    provider_name: str = "browser-runtime"
    active_sessions: int = 0
    error_code: str | None = None


class BrowserDomainPolicy:
    def __init__(self, allowed_domains: list[str], *, allow_subdomains: bool = True):
        self._allowed = frozenset(self._normalize(domain) for domain in allowed_domains)
        self._allow_subdomains = allow_subdomains

    def allows(self, url: str) -> bool:
        try:
            host = self._normalize(urlsplit(url).hostname or "")
        except ValueError:
            return False
        return any(host == domain or (self._allow_subdomains and host.endswith(f".{domain}")) for domain in self._allowed)

    @staticmethod
    def _normalize(domain: str) -> str:
        return domain.strip().lower().rstrip(".")


class BrowserSessionRegistry:
    """Bind each browser tab/session to one exact action owner."""

    def __init__(self):
        self._owners: dict[str, ActionOwner] = {}
        self._lock = asyncio.Lock()

    async def bind(self, browser_session_id: str, owner: ActionOwner) -> None:
        async with self._lock:
            existing = self._owners.get(browser_session_id)
            if existing is not None and existing != owner:
                raise BrowserSessionOwnershipError("Browser session belongs to another owner")
            self._owners[browser_session_id] = owner

    async def assert_owner(self, browser_session_id: str, owner: ActionOwner) -> None:
        async with self._lock:
            if self._owners.get(browser_session_id) != owner:
                raise BrowserSessionOwnershipError("Browser session is not visible to this owner")

    async def list_for_owner(self, owner: ActionOwner) -> list[str]:
        async with self._lock:
            return sorted(session_id for session_id, existing in self._owners.items() if existing == owner)

    async def count(self) -> int:
        async with self._lock:
            return len(self._owners)

    async def close(self, browser_session_id: str, owner: ActionOwner) -> None:
        async with self._lock:
            if self._owners.get(browser_session_id) != owner:
                raise BrowserSessionOwnershipError("Browser session is not visible to this owner")
            self._owners.pop(browser_session_id, None)


@runtime_checkable
class BrowserRuntimePort(Protocol):
    async def health(self) -> BrowserHealth:
        ...

    async def create_session(self, url: str) -> BrowserStateSummary:
        ...

    async def close_session(self, browser_session_id: str) -> None:
        ...

    async def observe(self, browser_session_id: str) -> BrowserStateSummary:
        ...

    async def resolve_dom(self, browser_session_id: str, hints: dict) -> dict | None:
        ...

    async def execute(self, browser_session_id: str, selector: dict, args: dict) -> bool:
        ...

    async def query_state(self, browser_session_id: str, selector: dict) -> dict:
        ...


class BrowserDOMTargetProvider:
    name = "browser-dom"
    strategy = TargetStrategy.browser

    def __init__(self, resolver: Callable[[str, dict], Awaitable[dict | None]], policy: BrowserDomainPolicy):
        self._resolver = resolver
        self._policy = policy

    async def resolve(self, query: TargetQuery, *, observation: Observation) -> TargetRef | None:
        state = observation.browser_state
        if state is None or state.url is None or state.tab_id is None or not self._policy.allows(state.url):
            return None
        selector = await self._resolver(state.tab_id, dict(query.selector_hints))
        if selector is None:
            return None
        return TargetRef(strategy=self.strategy, selector=selector, confidence=1, observation_id=observation.observation_id)


class BrowserRuntimeDOMTargetProvider(BrowserDOMTargetProvider):
    def __init__(self, runtime: BrowserRuntimePort, policy: BrowserDomainPolicy):
        super().__init__(runtime.resolve_dom, policy)


class BrowserStateObservationProvider:
    """Merge browser runtime state into an existing desktop observation."""

    def __init__(self, runtime: BrowserRuntimePort, policy: BrowserDomainPolicy):
        self._runtime = runtime
        self._policy = policy

    async def attach(self, observation: Observation, browser_session_id: str) -> Observation:
        state = await self._runtime.observe(browser_session_id)
        allowed = bool(state.url and self._policy.allows(state.url))
        return observation.model_copy(deep=True, update={"browser_state": state.model_copy(update={"allowed_domain": allowed})})


class BrowserTabLifecycleService:
    """Owner-scoped browser tab lifecycle guarded by domain policy."""

    def __init__(self, runtime: BrowserRuntimePort, registry: BrowserSessionRegistry, policy: BrowserDomainPolicy):
        self._runtime = runtime
        self._registry = registry
        self._policy = policy

    async def health(self) -> BrowserHealth:
        try:
            health = await self._runtime.health()
            return health.model_copy(update={"active_sessions": await self._registry.count()})
        except Exception:
            return BrowserHealth(enabled=True, healthy=False, error_code="browser_unavailable")

    async def create(self, url: str, owner: ActionOwner) -> BrowserStateSummary:
        if not self._policy.allows(url):
            raise BrowserPolicyError("Browser URL is not allowed")
        state = await self._runtime.create_session(url)
        if state.tab_id is None or state.url is None or not self._policy.allows(state.url):
            raise BrowserPolicyError("Browser runtime returned a disallowed session")
        await self._registry.bind(state.tab_id, owner)
        return state.model_copy(update={"allowed_domain": True})

    async def list(self, owner: ActionOwner) -> list[str]:
        return await self._registry.list_for_owner(owner)

    async def close(self, browser_session_id: str, owner: ActionOwner) -> None:
        await self._registry.assert_owner(browser_session_id, owner)
        await self._runtime.close_session(browser_session_id)
        await self._registry.close(browser_session_id, owner)

    async def observe(self, browser_session_id: str, owner: ActionOwner) -> BrowserStateSummary:
        await self._registry.assert_owner(browser_session_id, owner)
        state = await self._runtime.observe(browser_session_id)
        allowed = bool(state.url and self._policy.allows(state.url))
        return state.model_copy(update={"allowed_domain": allowed})


class BrowserActionProvider:
    name = "browser-action"

    def __init__(self, executor: Callable[[str, dict, dict], Awaitable[bool]], policy: BrowserDomainPolicy):
        self._executor = executor
        self._policy = policy

    async def execute(self, action: ActionCommand) -> NativeActionResult:
        if action.kind != ActionKind.browser_action or action.target is None or action.target.strategy != TargetStrategy.browser:
            return NativeActionResult(succeeded=False, error_code="browser_action_unsupported", error_message="Browser action requires a browser target.")
        url = str(action.args.get("url", ""))
        tab_id = str(action.args.get("tab_id", ""))
        if not tab_id or not self._policy.allows(url):
            return NativeActionResult(succeeded=False, error_code="browser_domain_blocked", error_message="Browser domain is not allowed.")
        try:
            succeeded = await self._executor(tab_id, dict(action.target.selector), dict(action.args))
        except Exception:
            return NativeActionResult(succeeded=False, error_code="browser_action_failed", error_message="Browser action failed.")
        return NativeActionResult(succeeded=succeeded, error_code=None if succeeded else "browser_action_failed")


class BrowserRuntimeActionProvider(BrowserActionProvider):
    def __init__(self, runtime: BrowserRuntimePort, policy: BrowserDomainPolicy):
        super().__init__(runtime.execute, policy)


class BrowserStateVerificationProvider:
    name = "browser-state-verification"

    def __init__(self, runtime: BrowserRuntimePort, policy: BrowserDomainPolicy):
        self._runtime = runtime
        self._policy = policy

    async def verify(self, conditions: list[Condition], *, before: Observation, after: Observation) -> VerificationResult:
        failed = []
        for condition in conditions:
            if condition.kind == "browser_allowed_domain":
                state = after.browser_state
                ok = state is not None and state.url is not None and self._policy.allows(state.url)
                if (condition.operator == ConditionOperator.exists and not ok) or (condition.operator == ConditionOperator.not_exists and ok):
                    failed.append(condition)
            elif condition.kind == "browser_dom":
                state = after.browser_state
                if state is None or state.tab_id is None:
                    failed.append(condition)
                    continue
                try:
                    result = await self._runtime.query_state(state.tab_id, dict(condition.selector))
                except Exception:
                    failed.append(condition)
                    continue
                exists = bool(result.get("exists"))
                if (condition.operator == ConditionOperator.exists and not exists) or (condition.operator == ConditionOperator.not_exists and exists):
                    failed.append(condition)
        return VerificationResult(succeeded=not failed, checked_conditions=len(conditions), failed_conditions=failed)
