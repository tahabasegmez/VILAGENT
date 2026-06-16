"""Optional browser-use runtime adapter for VILAGENT browser contracts."""

from __future__ import annotations

import importlib
import inspect
import uuid
from collections.abc import Callable
from typing import Any

from vilagent.computer_use.browser import BrowserHealth, BrowserUnavailableError
from vilagent.computer_use.models import BrowserStateSummary


class BrowserUseRuntime:
    """Duck-typed adapter around browser-use or a Playwright-like session.

    The core computer-use package intentionally does not require browser-use at
    import time. When browser-use is unavailable, construction still succeeds
    and every operation fails closed with ``BrowserUnavailableError``.
    """

    def __init__(
        self,
        *,
        session_factory: Callable[[], Any] | None = None,
        provider_name: str = "browser-use",
    ):
        self._session_factory = session_factory
        self._provider_name = provider_name
        self._session: Any | None = None
        self._tabs: dict[str, Any] = {}
        self._last_state: dict[str, BrowserStateSummary] = {}

    async def health(self) -> BrowserHealth:
        if self._session_factory is None and _resolve_browser_session_class() is None:
            return BrowserHealth(enabled=True, healthy=False, provider_name=self._provider_name, active_sessions=len(self._tabs), error_code="browser_use_not_installed")
        return BrowserHealth(enabled=True, healthy=True, provider_name=self._provider_name, active_sessions=len(self._tabs))

    async def create_session(self, url: str) -> BrowserStateSummary:
        session = await self._ensure_session()
        page = await self._open_page(session, url)
        tab_id = _string_attr(page, "tab_id", "id", "page_id", "guid") or uuid.uuid4().hex
        self._tabs[tab_id] = page
        state = await self._state_for(tab_id, page=page, fallback_url=url)
        self._last_state[tab_id] = state
        return state

    async def close_session(self, browser_session_id: str) -> None:
        page = self._tabs.pop(browser_session_id, None)
        self._last_state.pop(browser_session_id, None)
        if page is None:
            raise BrowserUnavailableError("Browser session is unavailable")
        await _call_first(page, ("close",), required=False)

    async def observe(self, browser_session_id: str) -> BrowserStateSummary:
        page = self._tabs.get(browser_session_id)
        if page is None:
            raise BrowserUnavailableError("Browser session is unavailable")
        state = await self._state_for(browser_session_id, page=page)
        self._last_state[browser_session_id] = state
        return state

    async def resolve_dom(self, browser_session_id: str, hints: dict) -> dict | None:
        page = self._tabs.get(browser_session_id)
        if page is None:
            raise BrowserUnavailableError("Browser session is unavailable")
        for key in ("css", "selector", "xpath", "text", "role", "label"):
            value = hints.get(key)
            if isinstance(value, str) and value.strip():
                selector = {key: value.strip()}
                if await self._selector_exists(page, selector):
                    return selector
        return None

    async def execute(self, browser_session_id: str, selector: dict, args: dict) -> bool:
        page = self._tabs.get(browser_session_id)
        if page is None:
            raise BrowserUnavailableError("Browser session is unavailable")
        locator = await self._locator(page, selector)
        if locator is None:
            return False
        action = str(args.get("browser_action") or args.get("action") or "click")
        if action == "type":
            text = str(args.get("text") or args.get("typed_text") or "")
            if not text:
                return False
            return await _call_first(locator, ("fill", "type"), text, required=False) is not _MISSING
        if action == "press":
            key = str(args.get("key") or "")
            if not key:
                return False
            return await _call_first(locator, ("press",), key, required=False) is not _MISSING
        return await _call_first(locator, ("click",), required=False) is not _MISSING

    async def query_state(self, browser_session_id: str, selector: dict) -> dict:
        page = self._tabs.get(browser_session_id)
        if page is None:
            raise BrowserUnavailableError("Browser session is unavailable")
        return {"exists": await self._selector_exists(page, selector)}

    async def _ensure_session(self) -> Any:
        if self._session is not None:
            return self._session
        factory = self._session_factory
        if factory is None:
            session_class = _resolve_browser_session_class()
            if session_class is None:
                raise BrowserUnavailableError("browser-use is not installed")
            factory = session_class
        self._session = await _maybe_await(factory())
        await _call_first(self._session, ("start", "launch", "initialize"), required=False)
        return self._session

    async def _open_page(self, session: Any, url: str) -> Any:
        page = await _call_first(
            session,
            ("create_new_tab", "new_tab", "open_tab", "new_page", "create_page"),
            url,
            required=False,
        )
        if page is _MISSING:
            page = await _call_first(session, ("page", "current_page", "get_current_page"), required=False)
        if page is _MISSING:
            page = session
        navigated = await _call_first(page, ("goto", "navigate", "open"), url, required=False)
        if navigated is not _MISSING and navigated is not None and navigated is not page:
            page = navigated
        return page

    async def _state_for(self, tab_id: str, *, page: Any, fallback_url: str | None = None) -> BrowserStateSummary:
        raw_state = await _call_first(page, ("get_state", "state", "get_current_state"), required=False)
        url = _value_from_state(raw_state, "url") or _string_attr(page, "url") or fallback_url
        title = _value_from_state(raw_state, "title") or await _title(page)
        return BrowserStateSummary(url=url, title=title, tab_id=tab_id)

    async def _selector_exists(self, page: Any, selector: dict) -> bool:
        locator = await self._locator(page, selector)
        if locator is None:
            return False
        count = await _call_first(locator, ("count",), required=False)
        if isinstance(count, int):
            return count > 0
        visible = await _call_first(locator, ("is_visible",), required=False)
        if isinstance(visible, bool):
            return visible
        return True

    async def _locator(self, page: Any, selector: dict) -> Any | None:
        if "css" in selector:
            return await _call_first(page, ("locator", "query_selector"), selector["css"], required=False, missing_as_none=True)
        if "selector" in selector:
            return await _call_first(page, ("locator", "query_selector"), selector["selector"], required=False, missing_as_none=True)
        if "xpath" in selector:
            return await _call_first(page, ("locator", "query_selector"), f"xpath={selector['xpath']}", required=False, missing_as_none=True)
        if "text" in selector:
            return await _call_first(page, ("get_by_text",), selector["text"], required=False, missing_as_none=True)
        if "role" in selector:
            role = selector["role"]
            name = selector.get("name")
            if name is not None:
                return await _call_first(page, ("get_by_role",), role, name=name, required=False, missing_as_none=True)
            return await _call_first(page, ("get_by_role",), role, required=False, missing_as_none=True)
        if "label" in selector:
            return await _call_first(page, ("get_by_label",), selector["label"], required=False, missing_as_none=True)
        return None


def create_browser_use_runtime() -> BrowserUseRuntime:
    return BrowserUseRuntime()


class _Missing:
    pass


_MISSING = _Missing()


def _resolve_browser_session_class() -> Any | None:
    try:
        module = importlib.import_module("browser_use")
    except Exception:
        return None
    for name in ("BrowserSession", "Browser", "BrowserContext"):
        value = getattr(module, name, None)
        if value is not None:
            return value
    return None


async def _call_first(obj: Any, names: tuple[str, ...], *args: Any, required: bool = True, missing_as_none: bool = False, **kwargs: Any) -> Any:
    for name in names:
        candidate = getattr(obj, name, None)
        if candidate is None:
            continue
        try:
            if callable(candidate):
                return await _maybe_await(candidate(*args, **kwargs))
            if not args and not kwargs:
                return await _maybe_await(candidate)
        except TypeError:
            continue
    if missing_as_none:
        return None
    if required:
        raise BrowserUnavailableError(f"Browser runtime does not provide any of: {', '.join(names)}")
    return _MISSING


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _string_attr(obj: Any, *names: str) -> str | None:
    for name in names:
        value = getattr(obj, name, None)
        if isinstance(value, str) and value:
            return value
    return None


def _value_from_state(state: Any, key: str) -> str | None:
    if isinstance(state, dict):
        value = state.get(key)
    else:
        value = getattr(state, key, None)
    return value if isinstance(value, str) and value else None


async def _title(page: Any) -> str | None:
    value = await _call_first(page, ("title",), required=False)
    if isinstance(value, str):
        return value
    return None
