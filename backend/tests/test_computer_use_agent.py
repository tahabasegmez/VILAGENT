"""Tests for the VILAGENT CU agent prompt, tools, context, and factory."""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# Prompt tests
# ---------------------------------------------------------------------------


class TestCUPrompt:
    def test_system_prompt_under_token_budget(self):
        from vilagent.computer_use.prompt import build_system_prompt

        prompt = build_system_prompt()
        # Rough token estimate: ~4 chars per token for English.
        estimated_tokens = len(prompt) / 4
        assert estimated_tokens < 1200, f"System prompt too large: ~{estimated_tokens:.0f} tokens"

    def test_system_prompt_contains_key_sections(self):
        from vilagent.computer_use.prompt import build_system_prompt

        prompt = build_system_prompt()
        assert "<role>" in prompt
        assert "<loop>" in prompt
        assert "<targeting>" in prompt
        assert "<planning>" in prompt
        assert "<tools>" in prompt
        assert "<safety>" in prompt
        assert "UIA" in prompt
        assert "browser DOM" in prompt
        assert "FARA vision" in prompt
        assert "Do not plan before every click" in prompt

    def test_system_prompt_hotkey_injection(self):
        from vilagent.computer_use.prompt import build_system_prompt

        prompt = build_system_prompt(emergency_stop_hotkey="Ctrl+Shift+Q")
        assert "Ctrl+Shift+Q" in prompt

    def test_session_context_format(self):
        from vilagent.computer_use.prompt import build_session_context

        ctx = build_session_context(
            session_id="s-1",
            platform="windows",
            current_time="2026-06-10T12:00:00Z",
            active_window="Notepad",
            actions_used=5,
            max_actions=50,
        )
        assert "s-1" in ctx
        assert "Notepad" in ctx
        assert "5/50" in ctx

    def test_system_prompt_no_deep_research_content(self):
        from vilagent.computer_use.prompt import build_system_prompt

        prompt = build_system_prompt()
        assert "open-source super agent" not in prompt.lower()
        assert "citation" not in prompt.lower()
        assert "sandbox" not in prompt.lower()
        assert "web_search" not in prompt.lower()
        assert "str_replace" not in prompt.lower()
        assert "deep research" not in prompt.lower()


# ---------------------------------------------------------------------------
# Context helper tests
# ---------------------------------------------------------------------------


class TestCUToolContext:
    def test_get_host_control_not_set_raises(self):
        from vilagent.computer_use.tools._context import _lock, get_host_control

        import vilagent.computer_use.tools._context as ctx_mod
        # Ensure it's None.
        with _lock:
            old = ctx_mod._host_control
            ctx_mod._host_control = None
        try:
            with pytest.raises(RuntimeError, match="not initialized"):
                get_host_control()
        finally:
            with _lock:
                ctx_mod._host_control = old

    def test_get_host_control_set_success(self):
        from vilagent.computer_use.tools._context import get_host_control, set_host_control

        sentinel = object()
        set_host_control(sentinel)
        try:
            assert get_host_control() is sentinel
        finally:
            set_host_control(None)  # type: ignore[arg-type]

    def test_get_action_owner_default(self):
        from vilagent.computer_use.tools._context import get_action_owner

        owner = get_action_owner()
        assert owner["agent_id"] == "cu-lead"

    def test_get_action_owner_explicit(self):
        from vilagent.computer_use.tools._context import get_action_owner, set_action_owner

        explicit = {"thread_id": "a", "run_id": "b", "agent_id": "c"}
        set_action_owner(explicit)
        try:
            assert get_action_owner() == explicit
        finally:
            set_action_owner(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Tool import tests
# ---------------------------------------------------------------------------


class TestCUToolImports:
    def test_all_tools_importable(self):
        from vilagent.computer_use.tools import (
            find_element_tool,
            observe_desktop_tool,
            perform_native_action_tool,
            perform_browser_action_tool,
            verify_condition_tool,
        )

        assert observe_desktop_tool.name == "observe_desktop"
        assert find_element_tool.name == "find_element"
        assert perform_native_action_tool.name == "perform_native_action"
        assert perform_browser_action_tool.name == "perform_browser_action"
        assert verify_condition_tool.name == "verify_condition"

    def test_tool_count(self):
        from vilagent.computer_use.tools import __all__

        assert len(__all__) == 5

    def test_tool_descriptions_are_compact(self):
        from vilagent.computer_use.tools import (
            find_element_tool,
            observe_desktop_tool,
            perform_native_action_tool,
            perform_browser_action_tool,
            verify_condition_tool,
        )

        for t in [observe_desktop_tool, find_element_tool, perform_native_action_tool, perform_browser_action_tool, verify_condition_tool]:
            desc = t.description or ""
            assert len(desc) < 900, f"{t.name} description too long: {len(desc)} chars"


# ---------------------------------------------------------------------------
# State schema tests
# ---------------------------------------------------------------------------


class TestComputerUseState:
    def test_state_has_cu_fields(self):
        from vilagent.computer_use.state import ComputerUseState

        annotations = ComputerUseState.__annotations__
        assert "desktop_session" in annotations
        assert "plan" in annotations
        assert "latest_observation_id" in annotations
        assert "cost_budget" in annotations

    def test_state_does_not_have_sandbox_fields(self):
        from vilagent.computer_use.state import ComputerUseState

        annotations = ComputerUseState.__annotations__
        assert "sandbox" not in annotations
        assert "files" not in annotations
        assert "working_directory" not in annotations
