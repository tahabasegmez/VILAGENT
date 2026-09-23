"""Test configuration for the backend test suite."""

from __future__ import annotations

import sys
from pathlib import Path

# `vilagent` (backend/) and the shared test doubles (tests/fakes.py).
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402

from vilagent import connections  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_connections(tmp_path, monkeypatch):
    """No test reads or writes the operator's own saved API connections."""
    monkeypatch.setattr(connections, "db_path", lambda: tmp_path / "connections.db")


# Tests must never query the live desktop for password fields.
from vilagent.env.desktop.redaction import WindowsUIAPasswordRedactor  # noqa: E402

WindowsUIAPasswordRedactor._read_password_regions = lambda self: []
