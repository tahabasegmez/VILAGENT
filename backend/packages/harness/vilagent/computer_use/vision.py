"""Vision target providers for VILAGENT computer use."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urljoin

import httpx
from pydantic import BaseModel, Field

from vilagent.computer_use.models import Observation, Rect, TargetQuery, TargetRef, TargetStrategy
from vilagent.config.computer_use_config import ComputerUseVisionModelConfig

JsonPost = Callable[[str, dict[str, Any], dict[str, str], float], Awaitable[dict[str, Any]]]
JsonGet = Callable[[str, dict[str, str], float], Awaitable[dict[str, Any]]]


class VisionProviderHealth(BaseModel):
    provider_name: str
    enabled: bool
    healthy: bool
    endpoint_configured: bool
    model_name: str
    error_code: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


async def _httpx_post_json(url: str, payload: dict[str, Any], headers: dict[str, str], timeout: float) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(url, json=payload, headers=headers)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("UI-TARS response must be a JSON object")
        return data


async def _httpx_get_json(url: str, headers: dict[str, str], timeout: float) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        data = response.json()
        return data if isinstance(data, dict) else {"status": data}


class UiTarsPyngrokTargetProvider:
    """Resolve visual targets through a UI-TARS endpoint exposed by pyngrok.

    The provider is intentionally fail-closed: when no endpoint is configured,
    no screenshot is available, or the response shape is invalid, it returns
    ``None`` so cheaper semantic providers can remain the primary path.
    """

    name = "ui-tars-pyngrok"
    strategy = TargetStrategy.vision

    def __init__(
        self,
        config: ComputerUseVisionModelConfig,
        *,
        post_json: JsonPost | None = None,
        get_json: JsonGet | None = None,
    ):
        self._config = config
        self._post_json = post_json or _httpx_post_json
        self._get_json = get_json or _httpx_get_json

    async def health(self) -> VisionProviderHealth:
        if not self._config.enabled:
            return VisionProviderHealth(
                provider_name=self.name,
                enabled=False,
                healthy=False,
                endpoint_configured=bool(self._config.pyngrok_url),
                model_name=self._config.model_name,
                error_code="vision_disabled",
            )
        if not self._config.pyngrok_url:
            return VisionProviderHealth(
                provider_name=self.name,
                enabled=True,
                healthy=False,
                endpoint_configured=False,
                model_name=self._config.model_name,
                error_code="vision_endpoint_missing",
            )
        try:
            data = await self._get_json(self._health_url(), self._headers(), min(self._config.timeout_seconds, 10))
        except Exception:
            return VisionProviderHealth(
                provider_name=self.name,
                enabled=True,
                healthy=False,
                endpoint_configured=True,
                model_name=self._config.model_name,
                error_code="vision_health_unavailable",
            )
        return VisionProviderHealth(
            provider_name=self.name,
            enabled=True,
            healthy=True,
            endpoint_configured=True,
            model_name=str(data.get("model") or self._config.model_name),
            details={key: value for key, value in data.items() if key not in {"api_key", "token"}},
        )

    async def resolve(self, query: TargetQuery, *, observation: Observation) -> TargetRef | None:
        if not self._config.enabled or not self._config.pyngrok_url:
            return None

        image_base64 = query.selector_hints.get("screenshot_base64")
        screenshot_url = query.selector_hints.get("screenshot_url")
        if not image_base64 and not screenshot_url:
            return None

        payload = {
            "model": self._config.model_name,
            "task": "target_resolution",
            "instruction": (
                "Find the UI target described by 'description' in the provided screenshot. "
                "Return JSON with found, confidence, bounds, and selector. Bounds may be "
                "[x1,y1,x2,y2] or {x,y,width,height}. Do not execute the action."
            ),
            "response_schema": {
                "found": "boolean",
                "confidence": "number 0..1",
                "bounds": "[x1,y1,x2,y2] or {x,y,width,height}",
                "selector": "object with label/text/point when available",
            },
            "description": query.description,
            "selector_hints": dict(query.selector_hints),
            "minimum_confidence": query.minimum_confidence,
            "observation": {
                "observation_id": observation.observation_id,
                "session_id": observation.session_id,
                "screen_size": observation.screen_size.model_dump(),
                "monitor": observation.monitor.model_dump(mode="json"),
                "active_window": observation.active_window.model_dump(mode="json") if observation.active_window else None,
                "summary": observation.summary,
            },
        }
        if image_base64:
            payload["image_base64"] = image_base64
        if screenshot_url:
            payload["screenshot_url"] = screenshot_url

        data = await self._post_json(
            self._endpoint_url(),
            payload,
            self._headers(),
            self._config.timeout_seconds,
        )
        return self._parse_response(data, observation_id=observation.observation_id)

    def _endpoint_url(self) -> str:
        return self._url(self._config.endpoint_path)

    def _health_url(self) -> str:
        return self._url(self._config.health_endpoint_path)

    def _url(self, path_value: str) -> str:
        base = self._config.pyngrok_url.rstrip("/") + "/"
        path = path_value.lstrip("/")
        return urljoin(base, path)

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "ngrok-skip-browser-warning": "true",
        }
        if self._config.api_key and self._config.api_key != "not-needed":
            headers["Authorization"] = f"Bearer {self._config.api_key}"
        return headers

    @staticmethod
    def _parse_response(data: dict[str, Any], *, observation_id: str) -> TargetRef | None:
        if data.get("found") is False:
            return None
        raw_target = data.get("target") if isinstance(data.get("target"), dict) else data
        confidence = raw_target.get("confidence")
        if confidence is None:
            return None

        bounds = UiTarsPyngrokTargetProvider._parse_bounds(raw_target.get("bounds"))
        selector = raw_target.get("selector") if isinstance(raw_target.get("selector"), dict) else {}
        if "label" in raw_target and "label" not in selector:
            selector = {**selector, "label": raw_target["label"]}
        if "point" in raw_target and "point" not in selector:
            selector = {**selector, "point": raw_target["point"]}

        return TargetRef(
            strategy=TargetStrategy.vision,
            selector=selector,
            bounds=bounds,
            confidence=float(confidence),
            observation_id=str(raw_target.get("observation_id") or observation_id),
        )

    @staticmethod
    def _parse_bounds(raw_bounds: Any) -> Rect | None:
        if raw_bounds is None:
            return None
        if isinstance(raw_bounds, dict):
            width = raw_bounds.get("width")
            height = raw_bounds.get("height")
            if width is None and raw_bounds.get("x2") is not None and raw_bounds.get("x") is not None:
                width = int(raw_bounds["x2"]) - int(raw_bounds["x"])
            if height is None and raw_bounds.get("y2") is not None and raw_bounds.get("y") is not None:
                height = int(raw_bounds["y2"]) - int(raw_bounds["y"])
            return Rect(x=int(raw_bounds["x"]), y=int(raw_bounds["y"]), width=int(width), height=int(height))
        if isinstance(raw_bounds, (list, tuple)) and len(raw_bounds) == 4:
            x1, y1, x2, y2 = raw_bounds
            return Rect(x=int(x1), y=int(y1), width=int(x2) - int(x1), height=int(y2) - int(y1))
        return None
