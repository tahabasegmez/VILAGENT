"""Tests for VILAGENT vision target providers."""

from __future__ import annotations

import asyncio

from vilagent.computer_use.models import MonitorRef, Observation, Rect, Size, TargetQuery, TargetStrategy
from vilagent.computer_use.vision import UiTarsPyngrokTargetProvider
from vilagent.config.computer_use_config import ComputerUseVisionModelConfig


def _observation():
    return Observation(
        observation_id="obs-1",
        session_id="session-1",
        monitor=MonitorRef(monitor_id="primary", bounds=Rect(x=0, y=0, width=100, height=100), primary=True),
        screen_size=Size(width=100, height=100),
    )


def _clear_vilagent_env(monkeypatch):
    for name in (
        "VILAGENT_UITARS_ENABLED",
        "VILAGENT_UITARS_MODEL_NAME",
        "VILAGENT_UITARS_PYNGROK_URL",
        "VILAGENT_UITARS_API_KEY",
        "VILAGENT_UITARS_ENDPOINT_PATH",
        "VILAGENT_UITARS_HEALTH_ENDPOINT_PATH",
    ):
        monkeypatch.delenv(name, raising=False)


def test_ui_tars_provider_is_disabled_without_endpoint_or_image(monkeypatch):
    _clear_vilagent_env(monkeypatch)

    async def run():
        calls = []

        async def fake_post(url, payload, headers, timeout):
            calls.append((url, payload, headers, timeout))
            return {"confidence": 1, "bounds": [1, 2, 11, 22]}

        disabled = UiTarsPyngrokTargetProvider(
            ComputerUseVisionModelConfig(enabled=False, pyngrok_url="https://example.ngrok-free.app"),
            post_json=fake_post,
        )
        no_image = UiTarsPyngrokTargetProvider(
            ComputerUseVisionModelConfig(enabled=True, pyngrok_url="https://example.ngrok-free.app"),
            post_json=fake_post,
        )

        assert await disabled.resolve(TargetQuery(description="Save"), observation=_observation()) is None
        assert await no_image.resolve(TargetQuery(description="Save"), observation=_observation()) is None
        assert calls == []

    asyncio.run(run())


def test_ui_tars_provider_resolves_target_from_pyngrok_response(monkeypatch):
    _clear_vilagent_env(monkeypatch)

    async def run():
        calls = []

        async def fake_post(url, payload, headers, timeout):
            calls.append((url, payload, headers, timeout))
            return {
                "target": {
                    "confidence": 0.91,
                    "bounds": {"x": 5, "y": 7, "width": 20, "height": 10},
                    "selector": {"text": "Save"},
                }
            }

        provider = UiTarsPyngrokTargetProvider(
            ComputerUseVisionModelConfig(
                enabled=True,
                pyngrok_url="https://example.ngrok-free.app",
                api_key="secret",
                endpoint_path="/resolve-target",
            ),
            post_json=fake_post,
        )

        target = await provider.resolve(
            TargetQuery(description="Save", selector_hints={"screenshot_base64": "abc"}),
            observation=_observation(),
        )

        assert target is not None
        assert target.strategy == TargetStrategy.vision
        assert target.confidence == 0.91
        assert target.bounds == Rect(x=5, y=7, width=20, height=10)
        assert target.selector == {"text": "Save"}
        assert calls[0][0] == "https://example.ngrok-free.app/resolve-target"
        assert calls[0][2]["Authorization"] == "Bearer secret"
        payload = calls[0][1]
        assert payload["task"] == "target_resolution"
        assert payload["instruction"].startswith("Find the UI target")
        assert payload["response_schema"]["found"] == "boolean"
        assert payload["image_base64"] == "abc"
        assert payload["observation"]["observation_id"] == "obs-1"

    asyncio.run(run())


def test_ui_tars_health_reports_disabled_and_healthy_states(monkeypatch):
    _clear_vilagent_env(monkeypatch)

    async def run():
        disabled = await UiTarsPyngrokTargetProvider(ComputerUseVisionModelConfig(enabled=False)).health()
        assert disabled.healthy is False
        assert disabled.error_code == "vision_disabled"

        async def fake_get(url, headers, timeout):
            assert url == "https://example.ngrok-free.app/healthz"
            assert headers["Authorization"] == "Bearer secret"
            assert timeout == 10
            return {"model": "ui-tars-test", "status": "ok", "token": "redacted"}

        healthy = await UiTarsPyngrokTargetProvider(
            ComputerUseVisionModelConfig(
                enabled=True,
                pyngrok_url="https://example.ngrok-free.app",
                api_key="secret",
                health_endpoint_path="/healthz",
            ),
            get_json=fake_get,
        ).health()

        assert healthy.healthy is True
        assert healthy.model_name == "ui-tars-test"
        assert healthy.details == {"model": "ui-tars-test", "status": "ok"}

    asyncio.run(run())
