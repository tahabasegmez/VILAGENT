"""Tests for checkpoint-friendly VILAGENT graph state."""

from __future__ import annotations

from typing import get_type_hints

from vilagent.computer_use.state import ComputerUseState


def test_computer_use_state_contains_only_observation_reference():
    hints = get_type_hints(ComputerUseState, include_extras=True)

    assert "latest_observation_id" in hints
    assert "screenshot" not in hints
    assert "screenshot_history" not in hints
    assert "ui_tree" not in hints
