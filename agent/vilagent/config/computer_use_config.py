"""``computer_use`` settings: models, budgets, browser and desktop input."""

import os

from pydantic import BaseModel, Field, model_validator


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_path(name: str) -> str | None:
    """Read a path env var, expanding %VARS%/$VARS and ~ (os.getenv does not)."""
    value = (os.getenv(name) or "").strip()
    if not value:
        return None
    return os.path.expanduser(os.path.expandvars(value))


class ComputerUseBudgetConfig(BaseModel):
    # Per-run limits, enforced by runs.budget.BudgetMeter. planner_calls covers the plan,
    # up to two replans and the autonomous brief.
    planner_calls: int = Field(default=6, ge=1)
    vision_calls: int = Field(default=200, ge=0)
    supervisor_calls: int = Field(default=10, ge=0)
    total_actions: int = Field(default=150, ge=1)
    duration_seconds: int = Field(default=1800, ge=1)


class ComputerUseApprovalConfig(BaseModel):
    # Ask before steps at or above this planned risk: off | critical | high | medium.
    # The operator's UI choice (state key approval_threshold) overrides it.
    threshold: str = Field(default="high", pattern="^(off|critical|high|medium)$")
    timeout_seconds: int = Field(default=120, ge=10)
    # Action rules (independent of any model): words in FARA's note before a click or Enter...
    keywords: list[str] = Field(
        default_factory=lambda: ["pay", "buy", "purchase", "checkout", "place order", "send", "delete", "remove", "transfer", "submit", "unsubscribe", "confirm"]
    )
    # ...sites outside this list (empty = no site rule)...
    allowed_domains: list[str] = Field(default_factory=list)
    # ...and destructive hotkeys.
    hotkeys: list[str] = Field(default_factory=lambda: ["alt+f4", "ctrl+w", "shift+delete"])


class ComputerUseBrowserConfig(BaseModel):
    # Playwright-driven browser control for FARA browser steps. Headed by default so
    # the operator sees the browser on the desktop; flip to headless for servers.
    playwright_headless: bool = Field(default_factory=lambda: _env_bool("VILAGENT_BROWSER_HEADLESS", False))
    viewport_width: int = Field(default=1280, ge=320, le=7680)
    viewport_height: int = Field(default=800, ge=240, le=4320)
    # Use the installed Microsoft Edge (not Playwright's bundled Chromium) with the
    # operator's REAL profile, so their accounts/cookies/logins are already present —
    # not a fresh guest profile. Requires Edge to be CLOSED at launch (profile lock).
    channel: str = Field(default_factory=lambda: os.getenv("VILAGENT_BROWSER_CHANNEL", "msedge"))
    use_user_profile: bool = Field(default_factory=lambda: _env_bool("VILAGENT_BROWSER_USE_PROFILE", True))
    # None -> resolve the OS-default Edge "User Data" directory at launch time.
    user_data_dir: str | None = Field(default_factory=lambda: _env_path("VILAGENT_BROWSER_USER_DATA_DIR"))
    profile_directory: str = Field(default_factory=lambda: os.getenv("VILAGENT_BROWSER_PROFILE", "Default"))


class ComputerUseConfig(BaseModel):
    # On: operating the computer is what the app is for. Turn it off in the state file's
    # `settings` block to start the gateway without the desktop runtime (tasks then cannot run).
    enabled: bool = True
    platform: str = "windows"
    # FARA is the vision action model for both the desktop and the browser.
    # The computer-use model's name, for the activity panel; the client itself is a connection.
    vision_model_name: str = ""
    # Screenshots sent to the vision model can be downscaled to this longest edge (JPEG);
    # coordinates are mapped back. 0 = full-resolution PNG with 1:1 coordinates (safe
    # default: some models do not answer in the sent-image space, which misclicks).
    vision_max_image_dimension: int = Field(default=0, ge=0, le=8192)
    vision_jpeg_quality: int = Field(default=85, ge=1, le=95)
    emergency_stop_hotkey: str = "ctrl+alt+escape"
    # Desktop mouse input (the vision model's clicks and scrolls).
    physical_input_enabled: bool = True
    # Black out password fields in desktop screenshots before they leave the machine.
    redact_passwords: bool = True
    uia_comtypes_cache_dir: str | None = ".vilagent/comtypes-cache"
    action_log_path: str | None = ".vilagent/actions.jsonl"
    browser: ComputerUseBrowserConfig = Field(default_factory=ComputerUseBrowserConfig)
    budgets: ComputerUseBudgetConfig = Field(default_factory=ComputerUseBudgetConfig)
    approvals: ComputerUseApprovalConfig = Field(default_factory=ComputerUseApprovalConfig)

    @model_validator(mode="before")
    @classmethod
    def _accept_legacy_keys(cls, data):
        """Map settings from older config.yaml layouts onto the flat fields."""
        if not isinstance(data, dict):
            return data
        data = dict(data)
        host_safety = data.pop("host_safety", None) or {}
        if "physical_input_enabled" in host_safety:
            data.setdefault("physical_input_enabled", host_safety["physical_input_enabled"])
        observation = data.pop("observation", None) or {}
        if "redact_sensitive_regions" in observation:
            data.setdefault("redact_passwords", observation["redact_sensitive_regions"])
        return data
