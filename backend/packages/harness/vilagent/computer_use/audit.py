"""Persistent audit storage for computer-use host actions."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from vilagent.computer_use.models import ComputerUseAuditEvent

_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")


class JsonlComputerUseAuditStore:
    """Single-process JSONL audit store with non-blocking file I/O."""

    def __init__(self, base_dir: str | Path = ".vilagent/computer-use/audit"):
        self._base_dir = Path(base_dir)
        self._locks: dict[str, asyncio.Lock] = {}

    async def append(self, event: ComputerUseAuditEvent) -> None:
        self._validate_session_id(event.session_id)
        async with self._locks.setdefault(event.session_id, asyncio.Lock()):
            await asyncio.to_thread(self._append_sync, event)

    async def list_session(self, session_id: str) -> list[ComputerUseAuditEvent]:
        self._validate_session_id(session_id)
        return await asyncio.to_thread(self._list_sync, session_id)

    def _path(self, session_id: str) -> Path:
        return self._base_dir / f"{session_id}.jsonl"

    def _append_sync(self, event: ComputerUseAuditEvent) -> None:
        path = self._path(event.session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(event.model_dump_json() + "\n")

    def _list_sync(self, session_id: str) -> list[ComputerUseAuditEvent]:
        path = self._path(session_id)
        if not path.exists():
            return []
        events = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                events.append(ComputerUseAuditEvent.model_validate(json.loads(line)))
        return events

    @staticmethod
    def _validate_session_id(session_id: str) -> None:
        if not session_id or not _SAFE_ID.fullmatch(session_id):
            raise ValueError("session_id must contain only alphanumeric characters, dash, or underscore")
