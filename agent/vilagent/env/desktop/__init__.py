"""The native Windows desktop as an agent environment."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from vilagent.actions import Action, ActionKind, ActionOutcome, Target, TargetStrategy
from vilagent.agents.common import exception_summary
from vilagent.config.computer_use_config import ComputerUseConfig
from vilagent.control import Control
from vilagent.env.base import Environment
from vilagent.env.desktop import keyboard, pointer, screen, uia
from vilagent.env.desktop.redaction import WindowsUIAPasswordRedactor

logger = logging.getLogger(__name__)


class DesktopDriver:
    """The blocking Windows operations behind :class:`DesktopEnvironment` (tests swap in a fake)."""

    def __init__(self, *, comtypes_cache_dir: str | None = None, redact_passwords: bool = True):
        self._cache_dir = comtypes_cache_dir
        self._redact = WindowsUIAPasswordRedactor(comtypes_cache_dir=comtypes_cache_dir) if redact_passwords else None

    def init_thread(self) -> None:
        """Runs first on the desktop thread: UI Automation COM objects stay bound to it."""
        uia.prepare_comtypes_cache(self._cache_dir)
        import pywinauto  # noqa: F401

    def screenshot(self) -> bytes:
        return screen.capture_png(self._redact)

    def click(self, x: int, y: int) -> None:
        pointer.click(x, y)

    def double_click(self, x: int, y: int) -> None:
        pointer.double_click(x, y)

    def right_click(self, x: int, y: int) -> None:
        pointer.right_click(x, y)

    def scroll(self, x: int, y: int, pixels: int) -> None:
        pointer.scroll(x, y, pixels)

    def screen_center(self) -> tuple[int, int]:
        return pointer.screen_center()

    def launch_app(self, app_name: str) -> None:
        keyboard.launch_app(app_name)

    def type_text(self, text: str) -> None:
        keyboard.type_text(text)

    def press_hotkey(self, keys: Any) -> None:
        keyboard.press_hotkey(keys)

    def navigate(self, url: str) -> None:
        keyboard.navigate_focused_browser(url)

    def resolve_target(self, description: str, hints: dict[str, Any]) -> Target | None:
        return uia.resolve_target(description, hints, cache_dir=self._cache_dir)

    def invoke(self, selector: dict[str, Any], *, focus_only: bool) -> None:
        uia.invoke(selector, focus_only=focus_only, cache_dir=self._cache_dir)


class DesktopEnvironment(Environment):
    """Screenshots of the primary display; mouse via pyautogui, keyboard and UIA via pywinauto.

    Every driver call runs on one dedicated thread: pywinauto's UI Automation objects
    only work on the thread that created them (elsewhere lookups silently find
    nothing), and one thread also keeps desktop input strictly sequential.
    """

    name = "native"

    def __init__(self, config: ComputerUseConfig, control: Control, *, driver: DesktopDriver | None = None):
        super().__init__(control)
        self._physical_input = config.physical_input_enabled
        self._driver = driver or DesktopDriver(
            comtypes_cache_dir=config.uia_comtypes_cache_dir,
            redact_passwords=config.redact_passwords,
        )
        self._thread = ThreadPoolExecutor(max_workers=1, thread_name_prefix="vilagent-desktop", initializer=self._driver.init_thread)

    async def _call(self, fn: Callable[..., Any], *args: Any) -> Any:
        return await asyncio.get_running_loop().run_in_executor(self._thread, fn, *args)

    async def screenshot(self) -> bytes:
        return await self._call(self._driver.screenshot)

    async def resolve_target(self, description: str, hints: dict[str, Any] | None = None) -> Target | None:
        """Find the one UI Automation element a step refers to (None if absent or ambiguous)."""
        try:
            return await self._call(self._driver.resolve_target, description, dict(hints or {}))
        except Exception:
            return None

    async def _execute(self, action: Action) -> ActionOutcome:
        driver = self._driver
        args = action.args
        kind = action.kind
        if kind == ActionKind.launch_app:
            app_name = str(args.get("app_name") or "").strip()
            if not app_name:
                return ActionOutcome.failure("app_name_required")
            await self._call(driver.launch_app, app_name)
        elif kind == ActionKind.type_text:
            await self._call(driver.type_text, str(args.get("text", "")))
        elif kind == ActionKind.hotkey:
            await self._call(driver.press_hotkey, args.get("keys"))
        elif kind == ActionKind.browser_action:
            url = str(args.get("url") or "").strip()
            if args.get("action") != "visit_url" or not url:
                return ActionOutcome.failure("browser_action_unsupported_on_desktop")
            await self._call(driver.navigate, url)
        elif action.target is not None and action.target.strategy == TargetStrategy.uia:
            if kind not in {ActionKind.click, ActionKind.focus_window}:
                return ActionOutcome.failure(f"uia_unsupported_action:{kind.value}")
            try:
                await self._call(lambda: driver.invoke(action.target.selector, focus_only=kind == ActionKind.focus_window))
            except Exception as exc:
                # Tree items and list rows often implement no Invoke pattern; they are still in a
                # known place, so click where the lookup found them.
                point = action.target.center
                if kind == ActionKind.focus_window or point is None or not self._physical_input:
                    return ActionOutcome.failure(f"uia_invoke_failed:{exception_summary(exc)}")
                logger.info("UIA invoke failed (%s); clicking the element where it was found", exc)
                await self._call(driver.click, *point)
        elif kind in {ActionKind.click, ActionKind.double_click, ActionKind.right_click, ActionKind.scroll}:
            if not self._physical_input:
                return ActionOutcome.failure("physical_input_disabled")
            point = action.point
            if kind == ActionKind.scroll:
                x, y = point or await self._call(driver.screen_center)
                pixels = int(args.get("amount") or 0)
                await self._call(driver.scroll, x, y, pixels)
            elif point is None:
                return ActionOutcome.failure("coordinate_target_required")
            else:
                press = {ActionKind.click: driver.click, ActionKind.double_click: driver.double_click, ActionKind.right_click: driver.right_click}[kind]
                await self._call(press, *point)
        else:
            return ActionOutcome.failure(f"desktop_unsupported_action:{kind.value}")
        return ActionOutcome.success()
