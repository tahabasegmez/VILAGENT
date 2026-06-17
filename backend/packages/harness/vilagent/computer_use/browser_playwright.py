"""Playwright-driven browser control for FARA browser steps.

When a plan step runs in the ``browser`` environment, VILAGENT does NOT pixel-click
the visible desktop browser. Instead it drives a dedicated Playwright Chromium: it
screenshots the page, asks FARA for the next action, and executes that action
against the page through Playwright (real mouse/keyboard/navigation on the DOM
surface). This is far more reliable than coordinate-clicking a screenshot of an
arbitrary desktop browser, and the viewport screenshot maps 1:1 to page mouse
coordinates (device_scale_factor=1), so FARA's coordinates land precisely.

The session is created lazily on the first browser step and reused for the rest of
the run, then closed by the orchestrator.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any
from urllib.parse import quote_plus

from vilagent.computer_use.models import ActionCommand, ActionKind, TargetStrategy

logger = logging.getLogger(__name__)

# Map the keys FARA emits to Playwright key names. FARA uses the CUA / browser
# vocabulary ("Enter", "Tab", "ArrowDown", ...); Playwright mostly accepts these
# verbatim but a few aliases need normalising.
_KEY_TO_PLAYWRIGHT = {
    "enter": "Enter",
    "return": "Enter",
    "tab": "Tab",
    "escape": "Escape",
    "esc": "Escape",
    "backspace": "Backspace",
    "delete": "Delete",
    "del": "Delete",
    "space": " ",
    "spacebar": " ",
    "up": "ArrowUp",
    "arrowup": "ArrowUp",
    "down": "ArrowDown",
    "arrowdown": "ArrowDown",
    "left": "ArrowLeft",
    "arrowleft": "ArrowLeft",
    "right": "ArrowRight",
    "arrowright": "ArrowRight",
    "pageup": "PageUp",
    "pagedown": "PageDown",
    "home": "Home",
    "end": "End",
    "ctrl": "Control",
    "control": "Control",
    "alt": "Alt",
    "shift": "Shift",
    "cmd": "Meta",
    "meta": "Meta",
    "win": "Meta",
    "super": "Meta",
}


def _playwright_key(token: str) -> str:
    text = str(token).strip()
    if not text:
        return text
    return _KEY_TO_PLAYWRIGHT.get(text.casefold(), text if len(text) == 1 else text.capitalize())


class PlaywrightUnavailableError(RuntimeError):
    """Raised when the playwright package or its browser binary is missing."""


def _default_edge_user_data_dir() -> str | None:
    """The OS-default Microsoft Edge 'User Data' directory (holds the real profiles)."""
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            return os.path.join(local, "Microsoft", "Edge", "User Data")
        return None
    home = os.path.expanduser("~")
    mac = os.path.join(home, "Library", "Application Support", "Microsoft Edge")
    if os.path.isdir(mac):
        return mac
    return os.path.join(home, ".config", "microsoft-edge")


def _resolve_user_data_and_profile(user_data_dir: str | None, profile_directory: str) -> tuple[str | None, str]:
    """Accept either the 'User Data' parent or a specific profile folder.

    Chromium expects ``user_data_dir`` = the 'User Data' parent and ``--profile-directory``
    = the profile sub-folder name (e.g. 'Default', 'Profile 1'). If the operator points at
    the profile folder directly (it contains a 'Preferences' file), split it so the right
    profile actually opens instead of a fresh one.
    """
    if not user_data_dir:
        return user_data_dir, profile_directory
    cleaned = user_data_dir.rstrip("/\\")
    try:
        looks_like_profile = os.path.isfile(os.path.join(cleaned, "Preferences"))
    except Exception:
        looks_like_profile = False
    if looks_like_profile:
        parent = os.path.dirname(cleaned)
        name = os.path.basename(cleaned)
        if parent and name:
            return parent, name
    return user_data_dir, profile_directory


def _infer_channel(channel: str, user_data_dir: str | None) -> str:
    """Infer the browser channel from the profile path so a Chrome profile opens in Chrome."""
    path = (user_data_dir or "").lower().replace("\\", "/")
    if "/google/chrome" in path:
        return "chrome"
    if "/bravesoftware/" in path:
        return "chrome"  # Brave is Chromium; closest channel
    if "/microsoft/edge" in path:
        return "msedge"
    return channel


class PlaywrightBrowserSession:
    """A single dedicated browser page that FARA drives through Playwright.

    By default this is the installed Microsoft Edge launched with the operator's real
    user profile (their accounts/cookies/logins), not a fresh guest profile.
    """

    def __init__(
        self,
        *,
        headless: bool = False,
        viewport_width: int = 1280,
        viewport_height: int = 800,
        downloads_folder: str | None = None,
        nav_timeout_ms: int = 30000,
        channel: str = "msedge",
        use_user_profile: bool = True,
        user_data_dir: str | None = None,
        profile_directory: str = "Default",
    ):
        self._headless = headless
        self._viewport_width = viewport_width
        self._viewport_height = viewport_height
        self._downloads_folder = downloads_folder
        self._nav_timeout_ms = nav_timeout_ms
        self._channel = channel or "msedge"
        self._use_user_profile = use_user_profile
        self._user_data_dir = user_data_dir
        self._profile_directory = profile_directory or "Default"
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None
        self._persistent = False
        self._started = False

    @property
    def viewport(self) -> tuple[int, int]:
        return (self._viewport_width, self._viewport_height)

    async def start(self) -> None:
        if self._started:
            return
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise PlaywrightUnavailableError(
                "playwright is not installed; run 'python -m pip install playwright' "
                "and 'python -m playwright install msedge'."
            ) from exc
        self._playwright = await async_playwright().start()
        # device_scale_factor=1 keeps screenshot pixels == page mouse coordinates so
        # FARA's coordinates land exactly where it intends.
        common = {
            "viewport": {"width": self._viewport_width, "height": self._viewport_height},
            "device_scale_factor": 1,
            "accept_downloads": bool(self._downloads_folder),
        }
        # Keep the real Chromium sandbox ON (chromium_sandbox=True) so Playwright does NOT
        # add --no-sandbox, which otherwise shows the "unsupported command-line flag" bar.
        # Drop --enable-automation so the "controlled by automated software" banner is gone.
        common["chromium_sandbox"] = True
        common["ignore_default_args"] = ["--enable-automation"]
        # --disable-background-mode so closing the window actually exits the browser
        # instead of leaving it running as a lingering background "Browser" process.
        extra_args = [
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-blink-features=AutomationControlled",
            "--disable-background-mode",
        ]

        user_data_dir = self._user_data_dir or (_default_edge_user_data_dir() if self._use_user_profile else None)
        # Be forgiving about what the operator points at: if they gave the specific profile
        # FOLDER (e.g. "...\\User Data\\Profile 1") instead of the "User Data" parent, split
        # it so --profile-directory selects the right profile. Also infer the browser channel
        # from the path (Chrome vs Edge) so a Chrome profile opens in Chrome.
        user_data_dir, profile_directory = _resolve_user_data_and_profile(user_data_dir, self._profile_directory)
        channel = _infer_channel(self._channel, user_data_dir)

        if self._use_user_profile and user_data_dir:
            # Launch the real profile so the operator's accounts are already in.
            try:
                self._context = await self._playwright.chromium.launch_persistent_context(
                    user_data_dir,
                    channel=channel,
                    headless=self._headless,
                    args=[*extra_args, f"--profile-directory={profile_directory}"],
                    **common,
                )
                self._persistent = True
            except Exception as exc:  # pragma: no cover - environment dependent
                await self._safe_stop_playwright()
                raise PlaywrightUnavailableError(
                    f"Could not launch {channel or 'the browser'} with your profile from "
                    f"'{user_data_dir}' (profile '{profile_directory}'). Make sure that browser is fully "
                    "CLOSED (its profile is locked while it runs) and installed. Set "
                    "VILAGENT_BROWSER_USER_DATA_DIR to the 'User Data' folder and VILAGENT_BROWSER_PROFILE "
                    f"to the profile name. Underlying error: {exc}"
                ) from exc
            self._context.set_default_timeout(self._nav_timeout_ms)
            pages = [p for p in self._context.pages if not p.is_closed()]
            self._page = pages[0] if pages else await self._context.new_page()
        else:
            # Fresh-profile fallback (e.g. headless servers): a normal browser context.
            try:
                self._browser = await self._playwright.chromium.launch(channel=channel, headless=self._headless, chromium_sandbox=True, ignore_default_args=["--enable-automation"], args=extra_args)
            except Exception:
                # Edge channel unavailable -> fall back to bundled Chromium.
                try:
                    self._browser = await self._playwright.chromium.launch(headless=self._headless, chromium_sandbox=True, ignore_default_args=["--enable-automation"], args=extra_args)
                except Exception as exc:  # pragma: no cover - environment dependent
                    await self._safe_stop_playwright()
                    raise PlaywrightUnavailableError(
                        "Could not launch a browser; install Edge ('python -m playwright install msedge') "
                        f"or Chromium ('python -m playwright install chromium'). Underlying error: {exc}"
                    ) from exc
            self._context = await self._browser.new_context(**{k: v for k, v in common.items() if k not in {"chromium_sandbox", "ignore_default_args"}})
            self._context.set_default_timeout(self._nav_timeout_ms)
            self._page = await self._context.new_page()
        self._started = True

    def on_close(self, callback) -> None:
        """Register a callback fired when the browser window/context is closed."""
        def _handler(*_args):
            self._started = False
            try:
                callback()
            except Exception:
                pass
        try:
            if self._context is not None:
                self._context.on("close", _handler)
            if self._page is not None:
                self._page.on("close", _handler)
        except Exception:
            pass

    def is_alive(self) -> bool:
        """True when the browser is still started with at least one open tab."""
        if not self._started or self._context is None:
            return False
        try:
            # Persistent (real-profile) contexts have no .browser handle, so judge
            # liveness by whether any tab is still open. If the active tab was closed
            # but others remain, adopt one of them.
            open_pages = [p for p in self._context.pages if not p.is_closed()]
            if not open_pages:
                return False
            if self._page is None or self._page.is_closed():
                self._page = open_pages[-1]
            if self._browser is not None and not self._browser.is_connected():
                return False
            return True
        except Exception:
            return False

    @property
    def current_url(self) -> str:
        try:
            return self._page.url if self._page is not None else ""
        except Exception:
            return ""

    async def screenshot(self) -> bytes:
        """PNG bytes of the current viewport (not full page) for the vision model."""
        assert self._page is not None
        await self._settle()
        try:
            return await self._page.screenshot(type="png", full_page=False, timeout=15000)
        except Exception:
            # A stuck navigation can block the screenshot; stop loading and retry once.
            try:
                await self._page.evaluate("window.stop()")
            except Exception:
                pass
            return await self._page.screenshot(type="png", full_page=False, timeout=15000)

    async def run_action(self, action: ActionCommand) -> tuple[bool, str | None]:
        """Execute one FARA-derived action on the page. Returns (succeeded, error_code)."""
        assert self._page is not None
        try:
            if action.kind in (ActionKind.click, ActionKind.double_click, ActionKind.right_click):
                return await self._click(action)
            if action.kind == ActionKind.type_text:
                return await self._type(action)
            if action.kind == ActionKind.hotkey:
                return await self._keypress(action)
            if action.kind == ActionKind.scroll:
                return await self._scroll(action)
            if action.kind == ActionKind.browser_action:
                return await self._browser_action(action)
            return False, f"browser_unsupported_action:{action.kind.value}"
        except Exception as exc:
            return False, f"browser_action_error:{exc.__class__.__name__}"

    def _point(self, action: ActionCommand) -> tuple[float, float] | None:
        target = action.target
        if target is not None and target.strategy == TargetStrategy.coordinate and isinstance(target.selector, dict):
            point = target.selector.get("point")
            if isinstance(point, (list, tuple)) and len(point) == 2:
                return float(point[0]), float(point[1])
        coord = action.args.get("coordinate")
        if isinstance(coord, (list, tuple)) and len(coord) == 2:
            return float(coord[0]), float(coord[1])
        return None

    async def _click(self, action: ActionCommand) -> tuple[bool, str | None]:
        point = self._point(action)
        if point is None:
            return False, "browser_click_missing_coordinate"
        x, y = point
        button = "right" if action.kind == ActionKind.right_click else "left"
        click_count = 2 if action.kind == ActionKind.double_click else 1
        new_page = await self._click_and_capture_popup(x, y, button=button, click_count=click_count)
        if new_page is not None:
            self._page = new_page
        return True, None

    async def _click_and_capture_popup(self, x: float, y: float, *, button: str, click_count: int):
        """Click and, if it opens a new tab, adopt that tab as the active page."""
        try:
            async with self._page.expect_event("popup", timeout=1000) as popup_info:
                await self._page.mouse.click(x, y, button=button, click_count=click_count, delay=20)
                new_page = await popup_info.value
                try:
                    await new_page.bring_to_front()
                    await new_page.wait_for_load_state("domcontentloaded", timeout=self._nav_timeout_ms)
                except Exception:
                    pass
                return new_page
        except Exception:
            # No popup within the window; the click itself already happened.
            return None

    async def _type(self, action: ActionCommand) -> tuple[bool, str | None]:
        text = str(action.args.get("text", ""))
        if action.args.get("delete_existing_text"):
            await self._page.keyboard.press("Control+A")
            await self._page.keyboard.press("Backspace")
        await self._page.keyboard.type(text, delay=15)
        if action.args.get("press_enter"):
            await self._page.keyboard.press("Enter")
        return True, None

    async def _keypress(self, action: ActionCommand) -> tuple[bool, str | None]:
        raw = action.args.get("keys")
        tokens: list[str]
        if isinstance(raw, str):
            tokens = [part for part in raw.replace("+", " ").split() if part]
        elif isinstance(raw, (list, tuple)):
            tokens = [str(part) for part in raw]
        else:
            tokens = []
        if not tokens:
            return False, "browser_keypress_missing_keys"
        mapped = [_playwright_key(tok) for tok in tokens]
        # Chord: hold modifiers down, press, release in reverse (matches FARA's `key`).
        for key in mapped:
            await self._page.keyboard.down(key)
        for key in reversed(mapped):
            await self._page.keyboard.up(key)
        return True, None

    async def _scroll(self, action: ActionCommand) -> tuple[bool, str | None]:
        try:
            pixels = float(action.args.get("amount") or action.args.get("pixels") or 0)
        except (TypeError, ValueError):
            pixels = 0.0
        if pixels == 0.0:
            # Default: scroll DOWN ~one screen. FARA's convention is negative = down.
            pixels = -float(self._viewport_height) * 0.8
        # FARA convention: positive pixels = up, negative = down. Playwright deltaY:
        # positive = down. So the wheel/window delta is -pixels.
        delta_y = -pixels
        point = self._point(action)
        if point is not None:
            await self._page.mouse.move(point[0], point[1])
        else:
            # Put the cursor over the page so the wheel targets the document, not a corner.
            await self._page.mouse.move(self._viewport_width // 2, self._viewport_height // 2)
        before = None
        try:
            before = await self._page.evaluate("() => window.scrollY")
        except Exception:
            before = None
        await self._page.mouse.wheel(0, delta_y)
        # If the wheel did not move the main document (cursor not over a scrollable area,
        # or a custom scroll container), scroll the window directly so the page moves.
        if before is not None:
            try:
                await self._page.wait_for_timeout(80)
                after = await self._page.evaluate("() => window.scrollY")
                if abs(after - before) < 2:
                    await self._page.evaluate("(dy) => window.scrollBy(0, dy)", delta_y)
            except Exception:
                pass
        return True, None

    async def _browser_action(self, action: ActionCommand) -> tuple[bool, str | None]:
        op = str(action.args.get("action") or "")
        if op == "visit_url":
            url = str(action.args.get("url") or "").strip()
            if not url:
                return False, "browser_visit_missing_url"
            if "://" not in url:
                url = "https://" + url
            await self._page.goto(url, wait_until="domcontentloaded", timeout=self._nav_timeout_ms)
            await self._settle()
            return True, None
        if op == "web_search":
            query = str(action.args.get("query") or action.args.get("text") or "").strip()
            if not query:
                return False, "browser_search_missing_query"
            await self._page.goto(
                f"https://www.bing.com/search?q={quote_plus(query)}",
                wait_until="domcontentloaded",
                timeout=self._nav_timeout_ms,
            )
            await self._settle()
            return True, None
        if op in ("history_back", "go_back"):
            await self._page.go_back(timeout=self._nav_timeout_ms)
            await self._settle()
            return True, None
        if op == "go_forward":
            await self._page.go_forward(timeout=self._nav_timeout_ms)
            await self._settle()
            return True, None
        if op == "refresh":
            await self._page.reload(timeout=self._nav_timeout_ms)
            await self._settle()
            return True, None
        return False, f"browser_unsupported_operation:{op or 'none'}"

    async def _settle(self) -> None:
        # Wait for the page to actually finish loading and render before we screenshot,
        # so the vision model never sees a blank / half-loaded page and give up.
        for state in ("domcontentloaded", "load"):
            try:
                await self._page.wait_for_load_state(state, timeout=8000)
            except Exception:
                pass
        # Brief network-idle wait catches client-rendered (SPA) content; best-effort.
        try:
            await self._page.wait_for_load_state("networkidle", timeout=2500)
        except Exception:
            pass
        await asyncio.sleep(0.7)

    async def close(self) -> None:
        for closer in (
            getattr(self._context, "close", None),
            getattr(self._browser, "close", None),
        ):
            if closer is not None:
                try:
                    await closer()
                except Exception:
                    pass
        await self._safe_stop_playwright()
        self._started = False
        self._page = self._context = self._browser = self._playwright = None

    async def _safe_stop_playwright(self) -> None:
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                pass


# --- Shared (persistent) browser session -----------------------------------------
#
# A single browser is reused across runs and is NOT closed when a task finishes, so the
# operator keeps seeing the result instead of the window vanishing. It is recreated only
# if the operator manually closed it, and torn down explicitly on emergency stop / app
# shutdown via close_shared_browser_session().

_shared_session: PlaywrightBrowserSession | None = None
_shared_lock = asyncio.Lock()


async def get_shared_browser_session(
    *,
    headless: bool = False,
    viewport_width: int = 1280,
    viewport_height: int = 800,
    channel: str = "msedge",
    use_user_profile: bool = True,
    user_data_dir: str | None = None,
    profile_directory: str = "Default",
) -> PlaywrightBrowserSession:
    global _shared_session
    async with _shared_lock:
        if _shared_session is not None and not _shared_session.is_alive():
            # The window was closed (or crashed); drop the dead handle and remake it.
            try:
                await _shared_session.close()
            except Exception:
                pass
            _shared_session = None
        if _shared_session is None:
            session = PlaywrightBrowserSession(
                headless=headless,
                viewport_width=viewport_width,
                viewport_height=viewport_height,
                channel=channel,
                use_user_profile=use_user_profile,
                user_data_dir=user_data_dir,
                profile_directory=profile_directory,
            )
            await session.start()
            # When the operator closes the window, fully tear down the shared session
            # (stop the Playwright driver, null the global) so nothing lingers and the
            # next task starts a clean browser.
            session.on_close(_schedule_shared_cleanup)
            _shared_session = session
        return _shared_session


def _schedule_shared_cleanup() -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(close_shared_browser_session())


async def close_shared_browser_session() -> None:
    global _shared_session
    async with _shared_lock:
        if _shared_session is not None:
            try:
                await _shared_session.close()
            except Exception:
                pass
            _shared_session = None
