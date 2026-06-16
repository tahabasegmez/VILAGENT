"""Single-writer lease for all desktop-mutating actions."""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass


class DesktopLeaseError(RuntimeError):
    """Base error for desktop lease operations."""


class DesktopLeaseTimeoutError(DesktopLeaseError):
    """Raised when the desktop lease cannot be acquired before timeout."""


class DesktopLeaseOwnershipError(DesktopLeaseError):
    """Raised when a caller attempts to mutate a lease it does not own."""


@dataclass(frozen=True, slots=True)
class DesktopLeaseToken:
    token_id: str
    owner_id: str
    acquired_at: float


@dataclass(frozen=True, slots=True)
class DesktopLeaseSnapshot:
    owner_id: str | None
    acquired_at: float | None
    last_heartbeat_at: float | None


class DesktopLease:
    """An async, heartbeat-aware lease that serializes desktop mutations."""

    def __init__(self, *, stale_after_seconds: float = 30):
        if stale_after_seconds <= 0:
            raise ValueError("stale_after_seconds must be positive")
        self._stale_after_seconds = stale_after_seconds
        self._condition = asyncio.Condition()
        self._token: DesktopLeaseToken | None = None
        self._last_heartbeat_at: float | None = None

    async def acquire(self, owner_id: str, *, timeout_seconds: float | None = None) -> DesktopLeaseToken:
        if not owner_id.strip():
            raise ValueError("owner_id must not be empty")
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

        deadline = None if timeout_seconds is None else time.monotonic() + timeout_seconds
        async with self._condition:
            while True:
                self._release_if_stale_locked()
                if self._token is None:
                    now = time.monotonic()
                    self._token = DesktopLeaseToken(token_id=uuid.uuid4().hex, owner_id=owner_id, acquired_at=now)
                    self._last_heartbeat_at = now
                    return self._token

                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise DesktopLeaseTimeoutError(f"Timed out waiting for desktop lease held by '{self._token.owner_id}'")
                try:
                    await asyncio.wait_for(self._condition.wait(), timeout=remaining)
                except TimeoutError as exc:
                    raise DesktopLeaseTimeoutError(f"Timed out waiting for desktop lease held by '{self._token.owner_id}'") from exc

    async def heartbeat(self, token: DesktopLeaseToken) -> None:
        async with self._condition:
            self._assert_owner_locked(token)
            self._last_heartbeat_at = time.monotonic()

    async def release(self, token: DesktopLeaseToken) -> None:
        async with self._condition:
            self._assert_owner_locked(token)
            self._clear_locked()

    async def force_release(self) -> None:
        async with self._condition:
            self._clear_locked()

    async def snapshot(self) -> DesktopLeaseSnapshot:
        async with self._condition:
            self._release_if_stale_locked()
            return DesktopLeaseSnapshot(
                owner_id=self._token.owner_id if self._token else None,
                acquired_at=self._token.acquired_at if self._token else None,
                last_heartbeat_at=self._last_heartbeat_at,
            )

    def _assert_owner_locked(self, token: DesktopLeaseToken) -> None:
        if self._token is None or self._token.token_id != token.token_id:
            raise DesktopLeaseOwnershipError("Desktop lease token is not the active owner")

    def _release_if_stale_locked(self) -> None:
        if self._token is None or self._last_heartbeat_at is None:
            return
        if time.monotonic() - self._last_heartbeat_at >= self._stale_after_seconds:
            self._clear_locked()

    def _clear_locked(self) -> None:
        self._token = None
        self._last_heartbeat_at = None
        self._condition.notify_all()
