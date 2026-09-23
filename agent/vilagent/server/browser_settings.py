"""Which browser and profile the managed browser opens: the operator's choice over the defaults.

Like every other UI selection this lives in ``<data dir>/.vilagent_state.json`` and wins over
the stored settings. Kept apart from the router so the runtime can read it too.
"""

from __future__ import annotations

from vilagent.config.computer_use_config import ComputerUseBrowserConfig
from vilagent.server.state import get_state_value

USE_PROFILE = "browser_use_profile"
CHANNEL = "browser_channel"
USER_DATA_DIR = "browser_user_data_dir"
PROFILE_DIRECTORY = "browser_profile_directory"


def browser_settings(config: ComputerUseBrowserConfig) -> ComputerUseBrowserConfig:
    """The stored settings with the operator's choices on top."""
    return config.model_copy(
        update={
            "use_user_profile": bool(get_state_value(USE_PROFILE, config.use_user_profile)),
            "channel": get_state_value(CHANNEL, "") or config.channel,
            "user_data_dir": get_state_value(USER_DATA_DIR, "") or config.user_data_dir,
            "profile_directory": get_state_value(PROFILE_DIRECTORY, "") or config.profile_directory,
        }
    )
