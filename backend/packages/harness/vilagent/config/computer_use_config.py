"""Configuration for the VILAGENT computer-use execution plane."""

import os
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from vilagent.computer_use.models import ActionKind


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


class ComputerUseObservationConfig(BaseModel):
    max_history_per_session: int = Field(default=100, ge=1, le=10000)
    storage_path: str | None = ".vilagent/computer-use/observations"
    max_storage_bytes_per_session: int = Field(default=536870912, ge=1048576)
    screenshot_retention_hours: int = Field(default=24, ge=0, le=8760)
    redact_sensitive_regions: bool = True
    max_export_bytes: int = Field(default=16777216, ge=1)
    max_concurrent_exports: int = Field(default=2, ge=1, le=32)
    max_exports_per_minute_per_owner: int = Field(default=30, ge=1, le=1000)


class ComputerUseBudgetConfig(BaseModel):
    planner_calls: int = Field(default=20, ge=1)
    vision_calls: int = Field(default=10, ge=0)
    total_actions: int = Field(default=100, ge=1)
    duration_seconds: int = Field(default=1800, ge=1)


class ComputerUseBrowserConfig(BaseModel):
    enabled: bool = False
    allowed_domains: list[str] = Field(default_factory=list)
    allow_subdomains: bool = True
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
    user_data_dir: str | None = Field(default_factory=lambda: os.getenv("VILAGENT_BROWSER_USER_DATA_DIR"))
    profile_directory: str = Field(default_factory=lambda: os.getenv("VILAGENT_BROWSER_PROFILE", "Default"))


class ComputerUseTextModelConfig(BaseModel):
    """Planner LLM routing for VILAGENT.

    ``provider`` records whether the model is reached through a normal API
    endpoint or a Colab/vLLM endpoint exposed with pyngrok. The actual
    LangChain model object is still loaded from the top-level ``models`` list
    via ``model_config_name`` so existing provider support stays reusable.
    """

    provider: Literal["api", "pyngrok"] = Field(default_factory=lambda: os.getenv("VILAGENT_TEXT_MODEL_PROVIDER", "api"))
    model_config_name: str | None = Field(default_factory=lambda: os.getenv("VILAGENT_TEXT_MODEL_CONFIG_NAME"))
    model_name: str | None = Field(default_factory=lambda: os.getenv("VILAGENT_TEXT_MODEL_NAME"))
    api_base_url: str | None = Field(default_factory=lambda: os.getenv("VILAGENT_TEXT_API_BASE_URL"))
    api_key: str | None = Field(default_factory=lambda: os.getenv("VILAGENT_TEXT_API_KEY"))
    pyngrok_url: str | None = Field(default_factory=lambda: os.getenv("VILAGENT_TEXT_PYNGROK_BASE_URL"))
    timeout_seconds: float = Field(default=60, gt=0, le=600)

    @model_validator(mode="after")
    def apply_env_overrides(self) -> "ComputerUseTextModelConfig":
        if provider := os.getenv("VILAGENT_TEXT_MODEL_PROVIDER"):
            if provider not in {"api", "pyngrok"}:
                raise ValueError("VILAGENT_TEXT_MODEL_PROVIDER must be 'api' or 'pyngrok'")
            self.provider = provider
        if value := os.getenv("VILAGENT_TEXT_MODEL_CONFIG_NAME"):
            self.model_config_name = value
        if value := os.getenv("VILAGENT_TEXT_MODEL_NAME"):
            self.model_name = value
        if value := os.getenv("VILAGENT_TEXT_API_BASE_URL"):
            self.api_base_url = value
        if value := os.getenv("VILAGENT_TEXT_API_KEY"):
            self.api_key = value
        if value := os.getenv("VILAGENT_TEXT_PYNGROK_BASE_URL"):
            self.pyngrok_url = value
        return self


class ComputerUseFaraModelConfig(BaseModel):
    """Vision action model endpoint used for end-to-end plan execution."""

    provider: Literal["fara_vllm"] = "fara_vllm"
    enabled: bool = Field(default_factory=lambda: _env_bool("VILAGENT_FARA_ENABLED", False))
    model_name: str = Field(default_factory=lambda: os.getenv("VILAGENT_FARA_MODEL_NAME", "microsoft/Fara-7B"))
    base_url: str | None = Field(default_factory=lambda: os.getenv("VILAGENT_FARA_BASE_URL", "http://localhost:5000/v1"))
    api_key: str | None = Field(default_factory=lambda: os.getenv("VILAGENT_FARA_API_KEY", "not-needed"))
    timeout_seconds: float = Field(default=120, gt=0, le=600)

    @model_validator(mode="after")
    def apply_env_overrides(self) -> "ComputerUseFaraModelConfig":
        if os.getenv("VILAGENT_FARA_ENABLED") is not None:
            self.enabled = _env_bool("VILAGENT_FARA_ENABLED", self.enabled)
        if value := os.getenv("VILAGENT_FARA_MODEL_NAME"):
            self.model_name = value
        if value := os.getenv("VILAGENT_FARA_BASE_URL"):
            self.base_url = value
        if value := os.getenv("VILAGENT_FARA_API_KEY"):
            self.api_key = value
        return self


class ComputerUseSupervisorModelConfig(BaseModel):
    """Dedicated recovery-supervisor model (env-configured, OpenAI-compatible).

    Defaults target Zhipu.ai GLM-V (vision) on the BigModel OpenAI-compatible API.
    Used when the operator selects the "api" supervisor source instead of the
    currently-selected planner model.
    """

    model_name: str = Field(default_factory=lambda: os.getenv("VILAGENT_SUPERVISOR_MODEL_NAME", "glm-4v-flash"))
    base_url: str | None = Field(default_factory=lambda: os.getenv("VILAGENT_SUPERVISOR_BASE_URL", "https://open.bigmodel.cn/api/paas/v4/"))
    api_key: str | None = Field(default_factory=lambda: os.getenv("VILAGENT_SUPERVISOR_API_KEY"))
    timeout_seconds: float = Field(default=60, gt=0, le=600)

    @model_validator(mode="after")
    def apply_env_overrides(self) -> "ComputerUseSupervisorModelConfig":
        if value := os.getenv("VILAGENT_SUPERVISOR_MODEL_NAME"):
            self.model_name = value
        if value := os.getenv("VILAGENT_SUPERVISOR_BASE_URL"):
            self.base_url = value
        if value := os.getenv("VILAGENT_SUPERVISOR_API_KEY"):
            self.api_key = value
        return self

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.base_url and self.model_name)


class ComputerUseHostSafetyConfig(BaseModel):
    allowed_actions: list[ActionKind] | None = None
    audit_dir: str = ".vilagent/computer-use/audit"
    # Operator-owned machine: native coordinate input (FARA's desktop clicks/scroll
    # via pyautogui) is enabled by default. Set false to disable physical input.
    physical_input_enabled: bool = True


class ComputerUseConfig(BaseModel):
    """Top-level settings for host desktop automation.

    The feature remains disabled until the Windows Agent Host and gateway
    lifecycle are connected in a later phase.
    """

    enabled: bool = False
    # Operator-owned single-machine automation: run without HITL/security gates by
    # default (auto-approve every action incl. the plan, allow all browser domains,
    # do not fail-closed on desktop-safety). Set false to restore approvals/guards.
    unrestricted: bool = True
    agent_mode: Literal["vilagent", "vilagent_legacy"] = "vilagent"
    architecture: Literal["react_graph", "plan_execute"] = "plan_execute"
    planner_model: str | None = None
    text_model: ComputerUseTextModelConfig = Field(default_factory=ComputerUseTextModelConfig)
    # FARA is the sole vision action model. The browser path drives it through
    # Playwright; the native path drives it through pywinauto/pyautogui.
    vision_provider: Literal["fara"] = "fara"
    vision_fara_model: ComputerUseFaraModelConfig = Field(default_factory=ComputerUseFaraModelConfig)
    # Dedicated recovery-supervisor model (env-configured GLM-V); used when the
    # operator selects the "api" supervisor source.
    supervisor_model: ComputerUseSupervisorModelConfig = Field(default_factory=ComputerUseSupervisorModelConfig)
    # Screenshots sent to the remote vision model can be downscaled to this longest
    # edge (and JPEG-encoded) to cut transfer latency; coordinates are mapped back to
    # screen pixels. Default 0 = full-resolution PNG, 1:1 coordinates (safe). Enable
    # (e.g. 1280) only if the action model returns coords in the sent-image space;
    # some models do not, which causes misclicks.
    vision_max_image_dimension: int = Field(default=0, ge=0, le=8192)
    vision_jpeg_quality: int = Field(default=85, ge=1, le=95)
    prompt_profile: Literal["compact"] = "compact"
    platform: str = "windows"
    runtime_mode: Literal["in_process", "dedicated_process"] = "dedicated_process"
    monitor_mode: str = "primary"
    desktop_lease_timeout_seconds: float = Field(default=30, gt=0, le=3600)
    desktop_lease_stale_after_seconds: float = Field(default=30, gt=0, le=3600)
    emergency_stop_hotkey: str = "ctrl+alt+escape"
    uia_comtypes_cache_dir: str | None = ".vilagent/comtypes-cache"
    lifecycle_path: str | None = ".vilagent/computer-use/lifecycle.json"
    observation: ComputerUseObservationConfig = Field(default_factory=ComputerUseObservationConfig)
    browser: ComputerUseBrowserConfig = Field(default_factory=ComputerUseBrowserConfig)
    budgets: ComputerUseBudgetConfig = Field(default_factory=ComputerUseBudgetConfig)
    host_safety: ComputerUseHostSafetyConfig = Field(default_factory=ComputerUseHostSafetyConfig)

    @model_validator(mode="after")
    def apply_env_overrides(self) -> "ComputerUseConfig":
        return self

    @model_validator(mode="before")
    @classmethod
    def drop_legacy_vision_model(cls, data):
        # Legacy configs carried a UI-TARS ``vision_model``/``vision_uitars_model``
        # block; FARA is now the only vision model, so silently drop them.
        if isinstance(data, dict) and ("vision_model" in data or "vision_uitars_model" in data):
            data = {k: v for k, v in data.items() if k not in {"vision_model", "vision_uitars_model"}}
        return data
