"""Tails of the log files the operator can read from the UI."""

from __future__ import annotations

from pathlib import Path

from vilagent.server.config import get_gateway_config

# Operator log sources -> files under <data dir>/logs. agent.log is written by this
# process; the other two are the child processes' output, written by the launcher.
LOG_SOURCES = {
    "agent": "agent.log",
    "gateway": "gateway.log",
    "ui": "ui.log",
}
LOG_TAIL_BYTES = 262144  # ~256 KiB; show the most recent log tail only.


def logs_dir() -> Path:
    return get_gateway_config().data_dir / "logs"


def read_log(source: str) -> str:
    """The tail of one log file, or a hint when the source is unknown."""
    file_name = LOG_SOURCES.get(source)
    if file_name is None:
        return f"Unknown log source '{source}'. Valid sources: {', '.join(LOG_SOURCES)}."
    path = logs_dir() / file_name
    try:
        if not path.exists():
            return f"(no log file yet at {path})"
        size = path.stat().st_size
        with open(path, "rb") as fh:
            if size > LOG_TAIL_BYTES:
                fh.seek(size - LOG_TAIL_BYTES)
            data = fh.read()
        text = data.decode("utf-8", errors="replace")
        if size > LOG_TAIL_BYTES:
            text = "... (truncated; showing the most recent output) ...\n" + text.split("\n", 1)[-1]
        return text or "(log file is empty)"
    except OSError as exc:
        return f"Failed to read {file_name}: {exc}"


def clear_log(source: str) -> str:
    file_name = LOG_SOURCES.get(source)
    if file_name is None:
        return f"Unknown log source '{source}'."
    path = logs_dir() / file_name
    try:
        if path.exists():
            path.write_text("", encoding="utf-8")
        return "cleared"
    except OSError as exc:
        return f"Failed to clear {file_name}: {exc}"
