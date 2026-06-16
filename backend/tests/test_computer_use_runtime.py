"""Tests for Gateway lifecycle integration of the VILAGENT host."""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

import pytest
from fastapi import FastAPI

from app.gateway.computer_use_runtime import computer_use_runtime
from vilagent.config.computer_use_config import ComputerUseConfig


def test_runtime_keeps_host_unavailable_when_feature_is_disabled():
    async def run():
        app = FastAPI()
        config = SimpleNamespace(computer_use=SimpleNamespace(enabled=False))

        async with computer_use_runtime(app, config):
            assert app.state.computer_use_host is None
            assert app.state.computer_use_remote_control is None
            assert app.state.computer_use_host_supervisor is None

    asyncio.run(run())


def test_runtime_rejects_non_windows_platform():
    async def run():
        app = FastAPI()
        config = SimpleNamespace(computer_use=SimpleNamespace(enabled=True, platform="linux"))

        try:
            async with computer_use_runtime(app, config):
                pass
        except RuntimeError as exc:
            assert "Windows" in str(exc)
        else:
            raise AssertionError("Expected unsupported platform failure")

    asyncio.run(run())


def test_runtime_starts_and_stops_host(monkeypatch):
    async def run():
        events = []

        class FakeHost:
            def __init__(self, config):
                events.append("start")

            async def initialize(self):
                events.append("initialize")

            async def shutdown(self):
                events.append("stop")

            def create_ipc_client(self):
                class Client:
                    async def heartbeat(self):
                        return SimpleNamespace(succeeded=True, error_code=None)

                return Client()

        import vilagent.computer_use.windows

        monkeypatch.setattr(vilagent.computer_use.windows, "WindowsAgentHost", FakeHost)
        app = FastAPI()
        config = SimpleNamespace(computer_use=SimpleNamespace(enabled=True, platform="windows"))

        async with computer_use_runtime(app, config):
            assert isinstance(app.state.computer_use_host, FakeHost)
            assert app.state.computer_use_remote_control is not None
        assert app.state.computer_use_host is None
        assert app.state.computer_use_remote_control is None
        assert app.state.computer_use_host_supervisor is None
        assert events == ["start", "initialize", "stop"]

    asyncio.run(run())


def test_runtime_fails_closed_and_shuts_down_when_host_initialization_fails(monkeypatch):
    async def run():
        events = []

        class FakeHost:
            def __init__(self, config):
                events.append("start")

            async def initialize(self):
                events.append("initialize")
                raise RuntimeError("corrupt lifecycle snapshot")

            async def shutdown(self):
                events.append("stop")

        import vilagent.computer_use.windows

        monkeypatch.setattr(vilagent.computer_use.windows, "WindowsAgentHost", FakeHost)
        app = FastAPI()
        config = SimpleNamespace(computer_use=SimpleNamespace(enabled=True, platform="windows"))

        try:
            async with computer_use_runtime(app, config):
                pass
        except RuntimeError as exc:
            assert "corrupt lifecycle snapshot" in str(exc)
        else:
            raise AssertionError("Expected initialization failure")

        assert app.state.computer_use_host is None
        assert events == ["start", "initialize", "stop"]

    asyncio.run(run())


def test_runtime_fails_closed_when_initial_ipc_heartbeat_fails(monkeypatch):
    async def run():
        events = []

        class FakeHost:
            def __init__(self, config):
                events.append("start")

            async def initialize(self):
                events.append("initialize")

            async def shutdown(self):
                events.append("stop")

            def create_ipc_client(self):
                class Client:
                    async def heartbeat(self):
                        return SimpleNamespace(succeeded=False, error_code="ipc_unavailable")

                return Client()

        import vilagent.computer_use.windows

        monkeypatch.setattr(vilagent.computer_use.windows, "WindowsAgentHost", FakeHost)
        app = FastAPI()
        config = SimpleNamespace(computer_use=SimpleNamespace(enabled=True, platform="windows"))

        try:
            async with computer_use_runtime(app, config):
                pass
        except RuntimeError as exc:
            assert "initial heartbeat failed" in str(exc)
        else:
            raise AssertionError("Expected initial heartbeat failure")

        assert app.state.computer_use_host is None
        assert events == ["start", "initialize", "stop"]

    asyncio.run(run())


def test_dedicated_runtime_owns_only_supervised_child_and_remote_facade(monkeypatch):
    async def run():
        events = []

        class Client:
            async def heartbeat(self):
                return SimpleNamespace(succeeded=True, error_code=None)

        class Supervisor:
            async def start(self):
                events.append("start")
                return SimpleNamespace(status="starting", child_alive=True)

            async def create_client(self):
                events.append("client")
                return Client()

            async def stop(self):
                events.append("stop")

        import vilagent.computer_use.windows

        monkeypatch.setattr(vilagent.computer_use.windows, "create_dedicated_windows_host_supervisor", lambda config: Supervisor())
        monkeypatch.setattr(
            vilagent.computer_use.windows,
            "WindowsAgentHost",
            lambda config: (_ for _ in ()).throw(AssertionError("in-process host must not be created")),
        )
        app = FastAPI()
        config = SimpleNamespace(
            computer_use=SimpleNamespace(enabled=True, platform="windows", runtime_mode="dedicated_process")
        )

        async with computer_use_runtime(app, config):
            assert app.state.computer_use_host is None
            assert app.state.computer_use_remote_control is not None
            assert isinstance(app.state.computer_use_host_supervisor, Supervisor)

        assert app.state.computer_use_host is None
        assert app.state.computer_use_remote_control is None
        assert app.state.computer_use_host_supervisor is None
        assert events == ["start", "client", "stop"]

    asyncio.run(run())


@pytest.mark.skipif(os.name != "nt", reason="Dedicated VILAGENT host currently targets Windows")
def test_real_dedicated_runtime_exposes_remote_routes_without_local_host(tmp_path):
    async def run():
        app = FastAPI()
        config = SimpleNamespace(
            computer_use=ComputerUseConfig(
                enabled=True,
                runtime_mode="dedicated_process",
                lifecycle_path=str(tmp_path / "lifecycle.json"),
                host_safety={"audit_dir": str(tmp_path / "audit")},
            )
        )

        async with computer_use_runtime(app, config):
            assert app.state.computer_use_host is None
            assert app.state.computer_use_remote_control is not None
            assert app.state.computer_use_host_supervisor is not None
            assert await app.state.computer_use_remote_control.list_sessions() == []

        assert app.state.computer_use_host is None
        assert app.state.computer_use_remote_control is None
        assert app.state.computer_use_host_supervisor is None

    asyncio.run(run())
