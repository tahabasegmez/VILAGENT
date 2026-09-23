"""The Chromium browsers installed here and the profiles inside their user-data directories.

Chromium keeps profiles as sub-folders ("Default", "Profile 1", …) of a "User Data" directory,
with their display names in ``Local State``. Asking for a folder that does not exist makes it
create an empty one, which looks like a guest session with none of the operator's accounts — so
the operator picks a real profile in the UI, and :func:`resolve_profile` catches the rest.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# (channel Playwright launches, label, path parts under the per-user app-data directory)
BROWSERS = (
    ("msedge", "Microsoft Edge", ("Microsoft", "Edge", "User Data")),
    ("chrome", "Google Chrome", ("Google", "Chrome", "User Data")),
    ("chrome", "Brave", ("BraveSoftware", "Brave-Browser", "User Data")),
)


def _app_data() -> Path | None:
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        return Path(local) if local else None
    home = Path.home()
    mac = home / "Library" / "Application Support"
    return mac if mac.is_dir() else home / ".config"


def installed_browsers() -> list[dict[str, str]]:
    """Every browser with a user-data directory on this machine, newest-used first."""
    root = _app_data()
    if root is None:
        return []
    found = []
    for channel, label, parts in BROWSERS:
        directory = root.joinpath(*parts)
        if directory.is_dir():
            found.append({"channel": channel, "label": label, "user_data_dir": str(directory)})
    return found


def list_profiles(user_data_dir: str | None) -> list[dict[str, str]]:
    """The operator's own profiles in a "User Data" directory, "Default" first.

    ``Local State`` lists exactly the profiles the browser shows in its profile menu, so the
    guest and system folders that also sit there never turn up as something to pick.
    """
    if not user_data_dir or not Path(user_data_dir).is_dir():
        return []
    named = _named_profiles(Path(user_data_dir))
    on_disk = {entry.name for entry in Path(user_data_dir).iterdir() if entry.is_dir() and (entry / "Preferences").is_file()}
    directories = [directory for directory in named if directory in on_disk] or sorted(on_disk - _NOT_A_PROFILE)
    return [{"directory": directory, "name": named.get(directory) or directory} for directory in sorted(directories, key=_order)]


# Folders that live beside the profiles but are not one of the operator's.
_NOT_A_PROFILE = {"Guest Profile", "System Profile"}


def _order(directory: str) -> tuple[int, int, str]:
    """"Default" first, then "Profile 2" before "Profile 10"."""
    number = directory.removeprefix("Profile ").strip()
    return (0, 0, "") if directory == "Default" else (1, int(number) if number.isdigit() else 9999, directory)


def _named_profiles(user_data_dir: Path) -> dict[str, str]:
    try:
        cache = json.loads((user_data_dir / "Local State").read_text(encoding="utf-8")).get("profile", {}).get("info_cache", {})
    except (OSError, ValueError, AttributeError):
        return {}
    return {directory: str(info.get("name") or directory) for directory, info in cache.items() if isinstance(info, dict)}


def resolve_profile(user_data_dir: str | None, profile_directory: str) -> str:
    """The wanted profile, or the first real one when it does not exist.

    Chromium would otherwise quietly make an empty profile under that name, and the operator
    gets a browser signed in to nothing.
    """
    if not user_data_dir:
        return profile_directory
    profiles = list_profiles(user_data_dir)
    if not profiles or any(profile["directory"] == profile_directory for profile in profiles):
        return profile_directory
    first = profiles[0]["directory"]
    logger.warning("Browser profile %r does not exist in %s; using %r instead", profile_directory, user_data_dir, first)
    return first
