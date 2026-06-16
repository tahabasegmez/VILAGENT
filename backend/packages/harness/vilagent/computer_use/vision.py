"""Vision provider health reporting for VILAGENT computer use.

FARA is the only vision action model; it is driven through Playwright in the
browser and through pywinauto/pyautogui on the native desktop. This module keeps
the shared health-report shape used by the gateway status endpoints.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class VisionProviderHealth(BaseModel):
    provider_name: str
    enabled: bool
    healthy: bool
    endpoint_configured: bool
    model_name: str
    error_code: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
