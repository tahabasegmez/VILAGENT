"""Computer-use agent factory.

``make_computer_use_agent`` creates a LangGraph-compatible agent graph that
uses the VILAGENT execution plane for Windows desktop automation.

Key differences from ``make_lead_agent``:
 - Uses ``ComputerUseState`` (no sandbox/file fields).
 - Compact system prompt (~900 tokens, not ~5000).
 - 5 CU-specific tools (not 15+ research/sandbox tools).
 - 5 middleware layers (not 14).
 - No TitleMiddleware, MemoryMiddleware, TodoMiddleware, SandboxMiddleware.
 - Host control is injected via config, never imported from app.*.

This file is importable from ``vilagent.agents`` and registered as a
LangGraph graph entry point.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_core.runnables import RunnableConfig

from vilagent.agents.middlewares.clarification_middleware import ClarificationMiddleware
from vilagent.agents.middlewares.dangling_tool_call_middleware import DanglingToolCallMiddleware
from vilagent.agents.middlewares.loop_detection_middleware import LoopDetectionMiddleware
from vilagent.agents.middlewares.token_usage_middleware import TokenUsageMiddleware
from vilagent.agents.middlewares.tool_error_handling_middleware import ToolErrorHandlingMiddleware
from vilagent.computer_use.prompt import build_system_prompt
from vilagent.computer_use.state import ComputerUseState
from vilagent.computer_use.tools._context import set_action_owner, set_host_control
from vilagent.computer_use.tools import (
    find_element_tool,
    observe_desktop_tool,
    verify_condition_tool,
)
from vilagent.config.app_config import get_app_config
from vilagent.models import create_chat_model
from vilagent.tools.builtins import ask_clarification_tool

if TYPE_CHECKING:
    from langchain_core.tools import BaseTool
    from langgraph.graph.state import CompiledStateGraph

logger = logging.getLogger(__name__)


def _resolve_cu_model_name() -> str:
    """Pick the first configured model for CU planning."""
    config = get_app_config()
    cu_config = config.computer_use
    # Allow explicit model override in config.
    planner_model = getattr(cu_config, "planner_model", None)
    if planner_model and config.get_model_config(planner_model):
        return planner_model
    text_model = getattr(cu_config, "text_model", None)
    text_model_config_name = getattr(text_model, "model_config_name", None)
    if text_model_config_name and config.get_model_config(text_model_config_name):
        return text_model_config_name
    # Fall back to the default model.
    if not config.models:
        raise ValueError("No chat models configured.")
    return config.models[0].name


def _build_cu_middleware() -> list[AgentMiddleware]:
    """Build the lean CU middleware chain (7 layers).

    Order:
      0. DanglingToolCallMiddleware — tool-call integrity.
      1. ToolErrorHandlingMiddleware — sanitise tool errors.
      2. TokenUsageMiddleware — tracking.
      3. LoopDetectionMiddleware — stall detection.
      5. ClarificationMiddleware — HITL (always last).
    """
    from vilagent.config.loop_detection_config import LoopDetectionConfig

    return [
        DanglingToolCallMiddleware(),
        ToolErrorHandlingMiddleware(),
        TokenUsageMiddleware(),
        LoopDetectionMiddleware.from_config(LoopDetectionConfig()),
        ClarificationMiddleware(),
    ]


def _build_browser_tools() -> list[BaseTool]:
    from vilagent.computer_use.tools.perform_browser_action import perform_browser_action_tool
    return [
        observe_desktop_tool,
        find_element_tool,
        perform_browser_action_tool,
        verify_condition_tool,
        ask_clarification_tool,
    ]


def _build_native_tools() -> list[BaseTool]:
    from vilagent.computer_use.tools.perform_native_action import perform_native_action_tool
    return [
        observe_desktop_tool,
        find_element_tool,
        perform_native_action_tool,
        verify_condition_tool,
        ask_clarification_tool,
    ]


def make_computer_use_agent(
    config: RunnableConfig | None = None,
) -> CompiledStateGraph:
    """Create the VILAGENT computer-use agent graph.

    This function is the LangGraph entry point registered in
    ``langgraph.json`` as ``"computer_use_agent"``.
    """
    from langgraph.graph import StateGraph, START, END
    from typing import Literal
    from pydantic import BaseModel

    app_config = get_app_config()
    cu_config = app_config.computer_use
    configurable = dict((config or {}).get("configurable", {}))
    if "computer_use_host_control" in configurable:
        set_host_control(configurable["computer_use_host_control"])
    if "computer_use_action_owner" in configurable:
        set_action_owner(configurable["computer_use_action_owner"])

    hotkey = getattr(cu_config, "emergency_stop_hotkey", "ctrl+alt+escape")
    system_prompt = build_system_prompt(emergency_stop_hotkey=hotkey)

    model_name = _resolve_cu_model_name()
    model = create_chat_model(model_name, thinking_enabled=False, attach_tracing=False)
    
    middleware = _build_cu_middleware()

    browser_agent = create_agent(
        model=model,
        tools=_build_browser_tools(),
        middleware=middleware,
        system_prompt=system_prompt,
        state_schema=ComputerUseState,
        name="browser_agent",
    )
    
    native_agent = create_agent(
        model=model,
        tools=_build_native_tools(),
        middleware=middleware,
        system_prompt=system_prompt,
        state_schema=ComputerUseState,
        name="native_agent",
    )

    class TaskCategory(BaseModel):
        category: Literal["browser", "native"]

    def categorize_task(state: ComputerUseState) -> dict:
        messages = state.get("messages", [])
        if not messages:
            return {"plan": {"category": "native"}}
        
        # Find last user message
        last_user = next((m.content for m in reversed(messages) if getattr(m, "type", "") == "user"), None)
        if not last_user:
            return {"plan": {"category": "native"}}
            
        try:
            router_model = model.with_structured_output(TaskCategory)
            res = router_model.invoke([
                {"role": "system", "content": "Classify the user task as 'browser' if it involves web navigation/scraping, or 'native' if it involves Windows desktop apps/files."},
                {"role": "user", "content": str(last_user)}
            ])
            return {"plan": {"category": res.category}}
        except Exception:
            return {"plan": {"category": "native"}}

    def route_edge(state: ComputerUseState) -> str:
        plan = state.get("plan") or {}
        cat = plan.get("category", "native")
        return cat

    builder = StateGraph(ComputerUseState)
    builder.add_node("router", categorize_task)
    builder.add_node("browser", browser_agent)
    builder.add_node("native", native_agent)

    builder.add_edge(START, "router")
    builder.add_conditional_edges("router", route_edge, {"browser": "browser", "native": "native"})
    builder.add_edge("browser", END)
    builder.add_edge("native", END)

    graph = builder.compile()

    logger.info(
        "VILAGENT computer-use agent created (model=%s, router=enabled)",
        model_name,
    )
    return graph
