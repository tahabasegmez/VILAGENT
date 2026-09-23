"""Choosing the browser profile FARA drives: what is detected, and what the UI can select."""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from vilagent.config import app_config
from vilagent.config.computer_use_config import ComputerUseBrowserConfig
from vilagent.env import browser_profiles
from vilagent.server import browser_api, state
from vilagent.server.browser_settings import browser_settings
from vilagent.server.deps import get_runtime, internal_auth_headers

BASE = "/api/computer-use/browser"


def make_user_data(root, profiles: dict[str, str], extras: tuple[str, ...] = ("Guest Profile", "System Profile")):
    """A Chromium "User Data" folder: real profiles in Local State, plus the folders beside them."""
    for directory in (*profiles, *extras):
        (root / directory).mkdir(parents=True)
        (root / directory / "Preferences").write_text("{}", encoding="utf-8")
    cache = {directory: {"name": name} for directory, name in profiles.items()}
    (root / "Local State").write_text(json.dumps({"profile": {"info_cache": cache}}), encoding="utf-8")
    return root


class FakeBrowser:
    def __init__(self):
        self.config: ComputerUseBrowserConfig | None = None

    def configure(self, config):
        self.config = config


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # The operator's own .env points at their real browser; these tests use a made-up one.
    for name in ("VILAGENT_BROWSER_CHANNEL", "VILAGENT_BROWSER_USE_PROFILE", "VILAGENT_BROWSER_USER_DATA_DIR", "VILAGENT_BROWSER_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    config_path = tmp_path / "config.yaml"
    config_path.write_text("computer_use:\n  enabled: true\n", encoding="utf-8")
    monkeypatch.setenv("VILAGENT_CONFIG_PATH", str(config_path))
    app_config.reset_app_config()
    monkeypatch.setattr(state, "STATE_FILE_PATH", tmp_path / "state.json")
    user_data = make_user_data(tmp_path / "Chrome" / "User Data", {"Profile 1": "Günlük", "Profile 7": "chatgpt"})
    monkeypatch.setattr(browser_profiles, "installed_browsers", lambda: [{"channel": "chrome", "label": "Google Chrome", "user_data_dir": str(user_data)}])
    monkeypatch.setattr(browser_api, "installed_browsers", browser_profiles.installed_browsers)
    app = FastAPI()
    app.include_router(browser_api.router)
    browser = FakeBrowser()
    app.dependency_overrides[get_runtime] = lambda: type("R", (), {"browser": browser})()
    with TestClient(app, headers=internal_auth_headers()) as test_client:
        test_client.browser = browser  # type: ignore[attr-defined]
        test_client.user_data = str(user_data)  # type: ignore[attr-defined]
        yield test_client
    app_config.reset_app_config()


def test_only_the_operators_own_profiles_are_offered(tmp_path):
    user_data = make_user_data(tmp_path / "User Data", {"Default": "Profil 1", "Profile 1": "Work"})

    profiles = browser_profiles.list_profiles(str(user_data))

    # "Guest Profile" and "System Profile" sit in the same folder but are not the operator's.
    assert [(item["directory"], item["name"]) for item in profiles] == [("Default", "Profil 1"), ("Profile 1", "Work")]


def test_a_profile_that_does_not_exist_falls_back_to_a_real_one(tmp_path, caplog):
    # Chrome numbers its profiles, so the "Default" default would open an empty, signed-out one.
    user_data = make_user_data(tmp_path / "User Data", {"Profile 2": "Work", "Profile 1": "Home"})

    assert browser_profiles.resolve_profile(str(user_data), "Profile 2") == "Profile 2"
    assert browser_profiles.resolve_profile(str(user_data), "Default") == "Profile 1"
    assert browser_profiles.resolve_profile(None, "Default") == "Default"


def test_the_ui_sees_the_browsers_and_profiles_on_this_machine(client):
    selection = client.get(BASE).json()

    assert selection["browsers"] == [{"channel": "chrome", "label": "Google Chrome", "user_data_dir": client.user_data}]
    assert [profile["name"] for profile in selection["profiles"]] == ["Günlük", "chatgpt"]
    assert selection["use_profile"] is True


def test_choosing_a_profile_is_kept_and_closes_the_open_browser(client, monkeypatch):
    closed = []
    monkeypatch.setattr(browser_api, "close_shared_browser_session", lambda: _record(closed))

    chosen = client.post(BASE, json={"use_profile": True, "channel": "chrome", "user_data_dir": "", "profile_directory": "Profile 7"})

    assert chosen.status_code == 200
    assert (chosen.json()["profile_directory"], chosen.json()["channel"]) == ("Profile 7", "chrome")
    # The next run opens it: the choice wins over config.yaml and the open browser is let go.
    settings = browser_settings(ComputerUseBrowserConfig(channel="msedge", profile_directory="Default"))
    assert (settings.channel, settings.profile_directory, settings.user_data_dir) == ("chrome", "Profile 7", client.user_data)
    assert client.browser.config == settings and closed == ["closed"]


async def _record(closed: list[str]) -> None:
    closed.append("closed")


def test_a_profile_the_browser_does_not_have_is_refused(client):
    refused = client.post(BASE, json={"use_profile": True, "channel": "chrome", "user_data_dir": "", "profile_directory": "Profile 9"})

    assert refused.status_code == 422
    assert "Profile 1" in refused.json()["detail"]


def test_a_fresh_profile_can_be_chosen_instead(client, monkeypatch):
    monkeypatch.setattr(browser_api, "close_shared_browser_session", lambda: _record([]))

    chosen = client.post(BASE, json={"use_profile": False, "channel": "", "user_data_dir": "", "profile_directory": ""})

    assert chosen.json()["use_profile"] is False
    assert browser_settings(ComputerUseBrowserConfig()).use_user_profile is False
