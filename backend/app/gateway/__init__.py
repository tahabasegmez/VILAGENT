"""Gateway package with lazy exports to keep router imports lightweight."""

from __future__ import annotations

__all__ = ["app", "create_app", "GatewayConfig", "get_gateway_config"]


def __getattr__(name: str):
    if name in {"app", "create_app"}:
        from .app import app, create_app

        return {"app": app, "create_app": create_app}[name]
    if name in {"GatewayConfig", "get_gateway_config"}:
        from .config import GatewayConfig, get_gateway_config

        return {"GatewayConfig": GatewayConfig, "get_gateway_config": get_gateway_config}[name]
    raise AttributeError(name)
