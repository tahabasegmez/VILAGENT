"""Which browser and profile the managed browser opens (the UI's Settings → Browser).

Like every other UI selection, the choice lives in ``<data dir>/.vilagent_state.json`` and wins
over the stored settings. Changing it closes the open browser, so the next task starts a
new one with the chosen profile.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from vilagent.config.app_config import AppConfig
from vilagent.env.browser import close_shared_browser_session
from vilagent.env.browser_profiles import installed_browsers, list_profiles
from vilagent.server.browser_settings import CHANNEL, PROFILE_DIRECTORY, USE_PROFILE, USER_DATA_DIR, browser_settings
from vilagent.server.deps import get_config, get_runtime, require_internal_request
from vilagent.server.runtime import Runtime
from vilagent.server.state import set_state_value

router = APIRouter(prefix="/api/computer-use", tags=["browser"], dependencies=[Depends(require_internal_request)])


class InstalledBrowser(BaseModel):
    channel: str
    label: str
    user_data_dir: str


class BrowserProfile(BaseModel):
    directory: str
    name: str


class BrowserSelection(BaseModel):
    """What the next browser task opens, and what it could open instead."""

    use_profile: bool
    channel: str
    user_data_dir: str
    profile_directory: str
    browsers: list[InstalledBrowser]
    profiles: list[BrowserProfile]


class BrowserUpdate(BaseModel):
    use_profile: bool
    channel: str = Field(default="", max_length=64)
    user_data_dir: str = Field(default="", max_length=1024)
    profile_directory: str = Field(default="", max_length=256)
    model_config = ConfigDict(extra="forbid")


def _default_user_data_dir(channel: str) -> str:
    """Where that browser keeps its profiles; any installed one when that channel is not here."""
    browsers = installed_browsers()
    for browser in browsers:
        if browser["channel"] == channel:
            return browser["user_data_dir"]
    return browsers[0]["user_data_dir"] if browsers else ""


def _selection(config: AppConfig) -> BrowserSelection:
    settings = browser_settings(config.computer_use.browser)
    user_data_dir = settings.user_data_dir or _default_user_data_dir(settings.channel)
    return BrowserSelection(
        use_profile=settings.use_user_profile,
        channel=settings.channel,
        user_data_dir=user_data_dir,
        profile_directory=settings.profile_directory,
        browsers=[InstalledBrowser(**browser) for browser in installed_browsers()],
        profiles=[BrowserProfile(**profile) for profile in list_profiles(user_data_dir)],
    )


@router.get("/browser", response_model=BrowserSelection, summary="The browser and profile tasks open")
async def get_browser(config: AppConfig = Depends(get_config)) -> BrowserSelection:
    return _selection(config)


@router.post("/browser", response_model=BrowserSelection, summary="Choose the browser and profile")
async def set_browser(body: BrowserUpdate, config: AppConfig = Depends(get_config), runtime: Runtime = Depends(get_runtime)) -> BrowserSelection:
    user_data_dir = body.user_data_dir.strip() or (_default_user_data_dir(body.channel) if body.channel else "")
    if body.use_profile and user_data_dir and not Path(user_data_dir).is_dir():
        raise HTTPException(status_code=422, detail=f"There is no folder at {user_data_dir}.")
    profile = body.profile_directory.strip()
    known = [item["directory"] for item in list_profiles(user_data_dir)]
    if body.use_profile and profile and known and profile not in known:
        raise HTTPException(status_code=422, detail=f"That browser has no profile {profile!r}. It has: {', '.join(known)}.")

    set_state_value(USE_PROFILE, body.use_profile)
    set_state_value(CHANNEL, body.channel.strip())
    set_state_value(USER_DATA_DIR, user_data_dir)
    set_state_value(PROFILE_DIRECTORY, profile)
    # The open browser still holds the old profile; the next task opens the chosen one.
    runtime.browser.configure(browser_settings(config.computer_use.browser))
    await close_shared_browser_session()
    return _selection(config)
