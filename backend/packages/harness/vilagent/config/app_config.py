import logging
import os
from collections.abc import Mapping
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Self

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, field_validator

from vilagent.config.acp_config import ACPAgentConfig, load_acp_config_from_dict
from vilagent.config.agents_api_config import AgentsApiConfig, load_agents_api_config_from_dict
from vilagent.config.checkpointer_config import CheckpointerConfig, load_checkpointer_config_from_dict
from vilagent.config.computer_use_config import ComputerUseConfig
from vilagent.config.database_config import DatabaseConfig
from vilagent.config.extensions_config import ExtensionsConfig
from vilagent.config.guardrails_config import GuardrailsConfig, load_guardrails_config_from_dict
from vilagent.config.loop_detection_config import LoopDetectionConfig
from vilagent.config.memory_config import MemoryConfig, load_memory_config_from_dict
from vilagent.config.model_config import ModelConfig
from vilagent.config.reload_boundary import format_field_description
from vilagent.config.run_events_config import RunEventsConfig
from vilagent.config.runtime_paths import existing_project_file
from vilagent.config.safety_finish_reason_config import SafetyFinishReasonConfig
from vilagent.config.sandbox_config import SandboxConfig
from vilagent.config.skill_evolution_config import SkillEvolutionConfig
from vilagent.config.skills_config import SkillsConfig
from vilagent.config.stream_bridge_config import StreamBridgeConfig, load_stream_bridge_config_from_dict
from vilagent.config.subagents_config import SubagentsAppConfig, load_subagents_config_from_dict
from vilagent.config.summarization_config import SummarizationConfig, load_summarization_config_from_dict
from vilagent.config.title_config import TitleConfig, load_title_config_from_dict
from vilagent.config.token_usage_config import TokenUsageConfig
from vilagent.config.tool_config import ToolConfig, ToolGroupConfig
from vilagent.config.tool_output_config import ToolOutputConfig
from vilagent.config.tool_search_config import ToolSearchConfig, load_tool_search_config_from_dict

def _find_dotenv_files() -> list[Path]:
    candidates: list[Path] = []
    try:
        if env_file := existing_project_file((".env",)):
            candidates.append(env_file)
    except Exception:
        pass
    for directory in (Path.cwd(), *Path.cwd().parents):
        candidate = directory / ".env"
        if candidate.is_file() and candidate not in candidates:
            candidates.append(candidate)
    return list(reversed(candidates))


for _dotenv_file in _find_dotenv_files():
    load_dotenv(_dotenv_file, override=False)

logger = logging.getLogger(__name__)


_OPTIONAL_VILAGENT_ENV_DEFAULTS: dict[str, str | None] = {
    "VILAGENT_UITARS_ENABLED": "false",
    "VILAGENT_UITARS_MODEL_NAME": "UI-TARS-1.5-7B",
    "VILAGENT_UITARS_PYNGROK_URL": None,
    "VILAGENT_UITARS_API_KEY": None,
    "VILAGENT_UITARS_ENDPOINT_PATH": "/resolve",
    "VILAGENT_UITARS_HEALTH_ENDPOINT_PATH": "/health",
    "VILAGENT_TEXT_MODEL_PROVIDER": "api",
    "VILAGENT_TEXT_MODEL_NAME": None,
    "VILAGENT_TEXT_API_KEY": None,
    "VILAGENT_FARA_ENABLED": "false",
    "VILAGENT_FARA_MODEL_NAME": "microsoft/Fara-7B",
    "VILAGENT_FARA_BASE_URL": "http://localhost:5000/v1",
    "VILAGENT_FARA_API_KEY": "not-needed",
}


CONFIG_FILE_DATABASE_DEFAULTS = {
    "backend": "sqlite",
    "sqlite_dir": ".vilagent/data",
}


class CircuitBreakerConfig(BaseModel):
    """Configuration for the LLM Circuit Breaker."""

    failure_threshold: int = Field(default=5, description="Number of consecutive failures before tripping the circuit")
    recovery_timeout_sec: int = Field(default=60, description="Time in seconds before attempting to recover the circuit")


def _legacy_config_candidates() -> tuple[Path, ...]:
    """Return source-tree config.yaml locations for monorepo compatibility."""
    backend_dir = Path(__file__).resolve().parents[4]
    repo_root = backend_dir.parent
    return (backend_dir / "config.yaml", repo_root / "config.yaml")


def logging_level_from_config(name: str | None) -> int:
    """Map ``config.yaml`` ``log_level`` string to a :mod:`logging` level constant."""
    mapping = logging.getLevelNamesMapping()
    return mapping.get((name or "info").strip().upper(), logging.INFO)


def apply_logging_level(name: str | None) -> None:
    """Resolve *name* to a logging level and apply it to the ``vilagent``/``app`` logger hierarchies.

    Only the ``vilagent`` and ``app`` logger levels are changed so that
    third-party library verbosity (e.g. uvicorn, sqlalchemy) is not
    affected. Root handler levels are lowered (never raised) so that
    messages from the configured loggers can propagate through without
    being filtered, while preserving handler thresholds that may be
    intentionally restrictive for third-party log output.
    """
    level = logging_level_from_config(name)
    for logger_name in ("vilagent", "app"):
        logging.getLogger(logger_name).setLevel(level)
    for handler in logging.root.handlers:
        if level < handler.level:
            handler.setLevel(level)


class AppConfig(BaseModel):
    """Config for the VILAGENT application"""

    log_level: str = Field(
        default="info",
        description=format_field_description(
            "log_level",
            field_doc="Logging level for vilagent and app modules (debug/info/warning/error); third-party libraries are not affected.",
        ),
    )
    token_usage: TokenUsageConfig = Field(default_factory=TokenUsageConfig, description="Token usage tracking configuration")
    computer_use: ComputerUseConfig = Field(
        default_factory=ComputerUseConfig,
        description=format_field_description(
            "computer_use",
            field_doc="VILAGENT computer-use execution plane configuration.",
        ),
    )
    models: list[ModelConfig] = Field(default_factory=list, description="Available models")
    sandbox: SandboxConfig = Field(
        default_factory=SandboxConfig,
        description=format_field_description(
            "sandbox",
            field_doc="Sandbox provider configuration (local filesystem or Docker-based aio sandbox).",
        ),
    )
    tools: list[ToolConfig] = Field(default_factory=list, description="Available tools")
    tool_groups: list[ToolGroupConfig] = Field(default_factory=list, description="Available tool groups")
    skills: SkillsConfig = Field(default_factory=SkillsConfig, description="Skills configuration")
    skill_evolution: SkillEvolutionConfig = Field(default_factory=SkillEvolutionConfig, description="Agent-managed skill evolution configuration")
    extensions: ExtensionsConfig = Field(default_factory=ExtensionsConfig, description="Extensions configuration (MCP servers and skills state)")
    tool_output: ToolOutputConfig = Field(default_factory=ToolOutputConfig, description="Tool output budget protection configuration")
    tool_search: ToolSearchConfig = Field(default_factory=ToolSearchConfig, description="Tool search / deferred loading configuration")
    title: TitleConfig = Field(default_factory=TitleConfig, description="Automatic title generation configuration")
    summarization: SummarizationConfig = Field(default_factory=SummarizationConfig, description="Conversation summarization configuration")
    memory: MemoryConfig = Field(default_factory=MemoryConfig, description="Memory subsystem configuration")
    agents_api: AgentsApiConfig = Field(default_factory=AgentsApiConfig, description="Custom-agent management API configuration")
    acp_agents: dict[str, ACPAgentConfig] = Field(default_factory=dict, description="ACP-compatible agent configuration")
    subagents: SubagentsAppConfig = Field(default_factory=SubagentsAppConfig, description="Subagent runtime configuration")
    guardrails: GuardrailsConfig = Field(default_factory=GuardrailsConfig, description="Guardrail middleware configuration")
    circuit_breaker: CircuitBreakerConfig = Field(default_factory=CircuitBreakerConfig, description="LLM circuit breaker configuration")
    loop_detection: LoopDetectionConfig = Field(default_factory=LoopDetectionConfig, description="Loop detection middleware configuration")
    safety_finish_reason: SafetyFinishReasonConfig = Field(default_factory=SafetyFinishReasonConfig, description="Provider safety-filter finish_reason interception middleware configuration")
    model_config = ConfigDict(extra="allow")
    database: DatabaseConfig = Field(
        default_factory=DatabaseConfig,
        description=format_field_description(
            "database",
            field_doc="Unified database backend for run/feedback metadata (memory, sqlite, or postgres).",
        ),
    )
    run_events: RunEventsConfig = Field(
        default_factory=RunEventsConfig,
        description=format_field_description(
            "run_events",
            field_doc="Run-event store backend (memory for dev, db for production queries, jsonl for lightweight single-node persistence).",
        ),
    )
    checkpointer: CheckpointerConfig | None = Field(
        default=None,
        description=format_field_description(
            "checkpointer",
            field_doc="LangGraph state-persistence checkpointer configuration.",
        ),
    )
    stream_bridge: StreamBridgeConfig | None = Field(
        default=None,
        description=format_field_description(
            "stream_bridge",
            field_doc="Stream bridge connecting agent workers to SSE endpoints.",
        ),
    )

    @field_validator("models", "tools", "tool_groups", mode="before")
    @classmethod
    def _none_list_fields_to_empty(cls, value):
        """Treat YAML keys with only commented examples as empty lists.

        A plain ``models:`` section whose items are all commented is parsed by
        YAML as ``None``. For VILAGENT first-run configs this should not crash
        config loading; readiness endpoints can then report that no model is
        configured yet.
        """
        return [] if value is None else value

    @classmethod
    def resolve_config_path(cls, config_path: str | None = None) -> Path:
        """Resolve the config file path.

        Priority:
        1. If provided `config_path` argument, use it.
        2. If provided `VILAGENT_CONFIG_PATH` environment variable, use it.
        3. Otherwise, search the caller project root.
        4. Finally, search legacy backend/repository-root defaults for monorepo compatibility.
        """
        if config_path:
            path = Path(config_path)
            if not Path.exists(path):
                raise FileNotFoundError(f"Config file specified by param `config_path` not found at {path}")
            return path
        elif os.getenv("VILAGENT_CONFIG_PATH"):
            path = Path(os.getenv("VILAGENT_CONFIG_PATH"))
            if not Path.exists(path):
                raise FileNotFoundError(f"Config file specified by environment variable `VILAGENT_CONFIG_PATH` not found at {path}")
            return path
        else:
            project_config = existing_project_file(("config.yaml",))
            if project_config is not None:
                return project_config

            for path in _legacy_config_candidates():
                if path.exists():
                    return path
            raise FileNotFoundError("`config.yaml` file not found in the project root or legacy backend/repository root locations")

    @classmethod
    def from_file(cls, config_path: str | None = None) -> Self:
        """Load config from YAML file.

        See `resolve_config_path` for more details.

        Args:
            config_path: Path to the config file.

        Returns:
            AppConfig: The loaded config.
        """
        resolved_path = cls.resolve_config_path(config_path)
        with open(resolved_path, encoding="utf-8") as f:
            config_data = yaml.safe_load(f) or {}

        # Check config version before processing
        cls._check_config_version(config_data, resolved_path)

        config_data = cls.resolve_env_variables(config_data)
        cls._apply_database_defaults(config_data)

        # Load circuit_breaker config if present
        if "circuit_breaker" in config_data:
            config_data["circuit_breaker"] = config_data["circuit_breaker"]

        # Load extensions config separately (it's in a different file)
        extensions_config = ExtensionsConfig.from_file()
        config_data["extensions"] = extensions_config.model_dump()

        result = cls.model_validate(config_data)
        acp_agents = cls._validate_acp_agents(config_data.get("acp_agents", {}))
        cls._apply_singleton_configs(result, acp_agents)
        return result

    @classmethod
    def _validate_acp_agents(
        cls,
        config_data: Mapping[str, Mapping[str, object]] | None,
    ) -> dict[str, ACPAgentConfig]:
        if config_data is None:
            config_data = {}
        return {name: ACPAgentConfig(**cfg) for name, cfg in config_data.items()}

    @classmethod
    def _apply_singleton_configs(cls, config: Self, acp_agents: dict[str, ACPAgentConfig]) -> None:
        from vilagent.config.checkpointer_config import get_checkpointer_config

        previous_checkpointer_config = get_checkpointer_config()

        load_title_config_from_dict(config.title.model_dump())
        load_summarization_config_from_dict(config.summarization.model_dump())
        load_memory_config_from_dict(config.memory.model_dump())
        load_agents_api_config_from_dict(config.agents_api.model_dump())
        load_subagents_config_from_dict(config.subagents.model_dump())
        load_tool_search_config_from_dict(config.tool_search.model_dump())
        load_guardrails_config_from_dict(config.guardrails.model_dump())
        load_checkpointer_config_from_dict(config.checkpointer.model_dump() if config.checkpointer is not None else None)
        load_stream_bridge_config_from_dict(config.stream_bridge.model_dump() if config.stream_bridge is not None else None)
        load_acp_config_from_dict({name: agent.model_dump() for name, agent in acp_agents.items()})

        if previous_checkpointer_config != config.checkpointer:
            # These runtime singletons derive their backend from checkpointer config.
            # Keep imports local to avoid cycles: both providers import get_app_config.
            from vilagent.runtime.checkpointer import reset_checkpointer
            from vilagent.runtime.store import reset_store

            reset_checkpointer()
            reset_store()

    @classmethod
    def _apply_database_defaults(cls, config_data: dict[str, Any]) -> None:
        """Apply config.yaml defaults for persistence when the section is absent."""
        database_config = config_data.get("database")
        if database_config is None:
            database_config = {}
            config_data["database"] = database_config
        if not isinstance(database_config, dict):
            return
        for key, value in CONFIG_FILE_DATABASE_DEFAULTS.items():
            database_config.setdefault(key, value)

    @classmethod
    def _check_config_version(cls, config_data: dict, config_path: Path) -> None:
        """Check if the user's config.yaml is outdated compared to config.example.yaml.

        Emits a warning if the user's config_version is lower than the example's.
        Missing config_version is treated as version 0 (pre-versioning).
        """
        try:
            user_version = int(config_data.get("config_version", 0))
        except (TypeError, ValueError):
            user_version = 0

        # Find config.example.yaml by searching config.yaml's directory and its parents
        example_path = None
        search_dir = config_path.parent
        for _ in range(5):  # search up to 5 levels
            candidate = search_dir / "config.example.yaml"
            if candidate.exists():
                example_path = candidate
                break
            parent = search_dir.parent
            if parent == search_dir:
                break
            search_dir = parent
        if example_path is None:
            return

        try:
            with open(example_path, encoding="utf-8") as f:
                example_data = yaml.safe_load(f)
            raw = example_data.get("config_version", 0) if example_data else 0
            try:
                example_version = int(raw)
            except (TypeError, ValueError):
                example_version = 0
        except Exception:
            return

        if user_version < example_version:
            logger.warning(
                "Your config.yaml (version %d) is outdated — the latest version is %d. Run `make config-upgrade` to merge new fields into your config.",
                user_version,
                example_version,
            )

    @classmethod
    def resolve_env_variables(cls, config: Any) -> Any:
        """Recursively resolve environment variables in the config.

        Environment variables are resolved using the `os.getenv` function. Example: $OPENAI_API_KEY

        Args:
            config: The config to resolve environment variables in.

        Returns:
            The config with environment variables resolved.
        """
        if isinstance(config, str):
            if config.startswith("$"):
                env_name = config[1:]
                env_value = os.getenv(env_name)
                if env_value is None:
                    if env_name in _OPTIONAL_VILAGENT_ENV_DEFAULTS:
                        return _OPTIONAL_VILAGENT_ENV_DEFAULTS[env_name]
                    raise ValueError(f"Environment variable {env_name} not found for config value {config}")
                return env_value
            return config
        elif isinstance(config, dict):
            return {k: cls.resolve_env_variables(v) for k, v in config.items()}
        elif isinstance(config, list):
            return [cls.resolve_env_variables(item) for item in config]
        return config

    def get_model_config(self, name: str) -> ModelConfig | None:
        """Get the model config by name.

        Args:
            name: The name of the model to get the config for.

        Returns:
            The model config if found, otherwise None.
        """
        return next((model for model in self.models if model.name == name), None)

    def get_tool_config(self, name: str) -> ToolConfig | None:
        """Get the tool config by name.

        Args:
            name: The name of the tool to get the config for.

        Returns:
            The tool config if found, otherwise None.
        """
        return next((tool for tool in self.tools if tool.name == name), None)

    def get_tool_group_config(self, name: str) -> ToolGroupConfig | None:
        """Get the tool group config by name.

        Args:
            name: The name of the tool group to get the config for.

        Returns:
            The tool group config if found, otherwise None.
        """
        return next((group for group in self.tool_groups if group.name == name), None)


# Compatibility singleton layer for code paths that have not yet been
# migrated to explicit ``AppConfig`` threading. New composition roots should
# prefer constructing ``AppConfig`` once and passing it down directly.
_app_config: AppConfig | None = None
_app_config_path: Path | None = None
_app_config_mtime: float | None = None
_app_config_is_custom = False
_current_app_config: ContextVar[AppConfig | None] = ContextVar("vilagent_current_app_config", default=None)
_current_app_config_stack: ContextVar[tuple[AppConfig | None, ...]] = ContextVar("vilagent_current_app_config_stack", default=())


def _get_config_mtime(config_path: Path) -> float | None:
    """Get the modification time of a config file if it exists."""
    try:
        return config_path.stat().st_mtime
    except OSError:
        return None


def _load_and_cache_app_config(config_path: str | None = None) -> AppConfig:
    """Load config from disk and refresh cache metadata."""
    global _app_config, _app_config_path, _app_config_mtime, _app_config_is_custom

    resolved_path = AppConfig.resolve_config_path(config_path)
    _app_config = AppConfig.from_file(str(resolved_path))
    _app_config_path = resolved_path
    _app_config_mtime = _get_config_mtime(resolved_path)
    _app_config_is_custom = False
    return _app_config


def get_app_config() -> AppConfig:
    """Get the VILAGENT config instance.

    Returns a cached singleton instance and automatically reloads it when the
    underlying config file path or modification time changes. Use
    `reload_app_config()` to force a reload, or `reset_app_config()` to clear
    the cache.
    """
    global _app_config, _app_config_path, _app_config_mtime

    runtime_override = _current_app_config.get()
    if runtime_override is not None:
        return runtime_override

    if _app_config is not None and _app_config_is_custom:
        return _app_config

    resolved_path = AppConfig.resolve_config_path()
    current_mtime = _get_config_mtime(resolved_path)

    should_reload = _app_config is None or _app_config_path != resolved_path or _app_config_mtime != current_mtime
    if should_reload:
        if _app_config_path == resolved_path and _app_config_mtime is not None and current_mtime is not None and _app_config_mtime != current_mtime:
            logger.info(
                "Config file has been modified (mtime: %s -> %s), reloading AppConfig",
                _app_config_mtime,
                current_mtime,
            )
        _load_and_cache_app_config(str(resolved_path))
    return _app_config


def reload_app_config(config_path: str | None = None) -> AppConfig:
    """Reload the config from file and update the cached instance.

    This is useful when the config file has been modified and you want
    to pick up the changes without restarting the application.

    Args:
        config_path: Optional path to config file. If not provided,
                     uses the default resolution strategy.

    Returns:
        The newly loaded AppConfig instance.
    """
    return _load_and_cache_app_config(config_path)


def reset_app_config() -> None:
    """Reset the cached config instance.

    This clears the singleton cache, causing the next call to
    `get_app_config()` to reload from file. Useful for testing
    or when switching between different configurations.
    """
    global _app_config, _app_config_path, _app_config_mtime, _app_config_is_custom
    _app_config = None
    _app_config_path = None
    _app_config_mtime = None
    _app_config_is_custom = False


def set_app_config(config: AppConfig) -> None:
    """Set a custom config instance.

    This allows injecting a custom or mock config for testing purposes.

    Args:
        config: The AppConfig instance to use.
    """
    global _app_config, _app_config_path, _app_config_mtime, _app_config_is_custom
    _app_config = config
    _app_config_path = None
    _app_config_mtime = None
    _app_config_is_custom = True


def peek_current_app_config() -> AppConfig | None:
    """Return the runtime-scoped AppConfig override, if one is active."""
    return _current_app_config.get()


def push_current_app_config(config: AppConfig) -> None:
    """Push a runtime-scoped AppConfig override for the current execution context."""
    stack = _current_app_config_stack.get()
    _current_app_config_stack.set(stack + (_current_app_config.get(),))
    _current_app_config.set(config)


def pop_current_app_config() -> None:
    """Pop the latest runtime-scoped AppConfig override for the current execution context."""
    stack = _current_app_config_stack.get()
    if not stack:
        _current_app_config.set(None)
        return
    previous = stack[-1]
    _current_app_config_stack.set(stack[:-1])
    _current_app_config.set(previous)
