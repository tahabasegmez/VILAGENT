import os
from pathlib import Path

from pydantic import BaseModel, Field


class GatewayConfig(BaseModel):
    """Process-level gateway settings, set by the launcher through the environment."""

    host: str = Field(default="127.0.0.1", description="Host to bind the gateway server")
    port: int = Field(default=8001, description="Port to bind the gateway server")
    enable_docs: bool = Field(default=True, description="Enable Swagger/OpenAPI endpoints")
    data_dir: Path = Field(description="Where .env, the connections database, UI state, logs and runtime files live")
    static_dir: Path | None = Field(default=None, description="Built operator UI to serve at /, if any")
    dev_origin: str | None = Field(default=None, description="Dev UI origin allowed through CORS (next dev)")


_gateway_config: GatewayConfig | None = None


def get_gateway_config() -> GatewayConfig:
    global _gateway_config
    if _gateway_config is None:
        static_dir = os.getenv("VILAGENT_STATIC_DIR")
        _gateway_config = GatewayConfig(
            host=os.getenv("GATEWAY_HOST", "127.0.0.1"),
            port=int(os.getenv("GATEWAY_PORT", "8001")),
            enable_docs=os.getenv("GATEWAY_ENABLE_DOCS", "true").lower() == "true",
            # The launcher chdirs into the data dir, so cwd is the fallback.
            data_dir=Path(os.getenv("VILAGENT_DATA_DIR") or Path.cwd()).resolve(),
            static_dir=Path(static_dir).resolve() if static_dir else None,
            dev_origin=os.getenv("VILAGENT_DEV_ORIGIN") or None,
        )
    return _gateway_config
