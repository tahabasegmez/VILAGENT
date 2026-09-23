"""The agent runtime owned by the gateway process: control, environments, runs, the run graph, emergency hotkey."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI

from vilagent.approvals.gate import ApprovalGate
from vilagent.config.app_config import AppConfig
from vilagent.control import ActionLog, Control
from vilagent.env.browser import BrowserEnvironment, close_shared_browser_session
from vilagent.env.desktop import DesktopEnvironment
from vilagent.graph.build import build_graph
from vilagent.memory.embeddings import reembed
from vilagent.memory.store import MemoryStore
from vilagent.runs.manager import RunManager
from vilagent.runs.records import RunRecords
from vilagent.server.browser_settings import browser_settings
from vilagent.server.config import get_gateway_config
from vilagent.server.memory import build_learner, current_embedder

logger = logging.getLogger(__name__)


@dataclass
class Runtime:
    control: Control
    desktop: DesktopEnvironment
    browser: BrowserEnvironment
    runs: RunManager
    # Without a checkpointer until the gateway lifespan attaches the SQLite one.
    graph: Any = field(default_factory=build_graph)
    # Experience memory (opened by the gateway lifespan).
    memory: MemoryStore | None = None


def create_runtime(config: AppConfig) -> Runtime:
    cu = config.computer_use
    control = Control(gate=ApprovalGate(cu.approvals), log=ActionLog(cu.action_log_path))
    # The emergency stop also closes the managed browser.
    control.on_stop(close_shared_browser_session)
    records = RunRecords(get_gateway_config().data_dir / "runs")
    if interrupted := records.recover():
        logger.warning("%d run(s) were cut off by the last shutdown; marked interrupted", interrupted)
    return Runtime(
        control=control,
        desktop=DesktopEnvironment(cu, control),
        browser=BrowserEnvironment(browser_settings(cu.browser), control),
        runs=RunManager(records),
    )


@asynccontextmanager
async def agent_runtime(app: FastAPI, config: AppConfig) -> AsyncGenerator[None, None]:
    app.state.runtime = None
    cu = config.computer_use
    if not cu.enabled:
        logger.warning("computer_use.enabled is off in the settings; tasks cannot run")
        yield
        return
    if cu.platform.casefold() != "windows":
        raise RuntimeError("VILAGENT currently supports only Windows")

    runtime = create_runtime(config)
    from vilagent.env.desktop.hotkey import WindowsGlobalHotkeyListener

    hotkey = WindowsGlobalHotkeyListener(cu.emergency_stop_hotkey, on_trigger=lambda: runtime.control.engage("Global emergency-stop hotkey"))
    try:
        await hotkey.start()
        logger.info("Emergency-stop hotkey %s registered", cu.emergency_stop_hotkey)
    except Exception:
        # Another app may own the combination; the UI stop button still works.
        logger.warning("Could not register the emergency-stop hotkey %s", cu.emergency_stop_hotkey, exc_info=True)
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    storage = get_gateway_config().data_dir / ".vilagent"
    storage.mkdir(parents=True, exist_ok=True)
    app.state.runtime = runtime
    try:
        async with AsyncSqliteSaver.from_conn_string(str(storage / "checkpoints.sqlite")) as saver:
            runtime.graph = build_graph(saver)
            runtime.runs.forget = saver.adelete_thread
            await runtime.runs.expire_interrupted()
            runtime.memory = await MemoryStore.open(storage / "memory.sqlite")
            runtime.runs.after_run = build_learner(runtime.memory).after_run
            # Rows embedded by another model (the planner preset changed) get new vectors.
            reembedding = asyncio.create_task(reembed(runtime.memory, current_embedder()))
            try:
                yield
            finally:
                # Before the stores close: a run still going becomes resumable, not lost.
                await runtime.runs.shutdown()
                reembedding.cancel()
                await asyncio.gather(reembedding, return_exceptions=True)
                await runtime.memory.close()
    finally:
        app.state.runtime = None
        await hotkey.stop()
        await close_shared_browser_session()
