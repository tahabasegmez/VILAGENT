"""Fail-closed exclusive ownership for durable lifecycle state."""

from __future__ import annotations

import asyncio
import os
import secrets
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from vilagent.computer_use.models import utc_now


class LifecycleOwnershipError(RuntimeError):
    """Raised when exclusive durable lifecycle ownership cannot be proven."""


class LifecycleOwnershipMetadata(BaseModel):
    version: int = 1
    claim_id: str
    owner_name: str
    process_id: int
    created_at: datetime
    model_config = ConfigDict(extra="forbid")


class LifecycleOwnershipClaim:
    """Atomically claim one lifecycle path for the lifetime of a host."""

    def __init__(self, lifecycle_path: str | Path, *, owner_name: str = "windows-agent-host"):
        self.lifecycle_path = Path(lifecycle_path)
        self.claim_path = self.lifecycle_path.with_name(f"{self.lifecycle_path.name}.owner")
        self._metadata = LifecycleOwnershipMetadata(
            claim_id=secrets.token_urlsafe(24),
            owner_name=owner_name,
            process_id=os.getpid(),
            created_at=utc_now(),
        )
        self._owned = False
        self._lock = asyncio.Lock()

    @property
    def owned(self) -> bool:
        return self._owned

    async def acquire(self) -> None:
        async with self._lock:
            if self._owned:
                return
            try:
                await asyncio.to_thread(self._acquire_sync)
            except FileExistsError as exc:
                try:
                    existing_data = self.claim_path.read_text(encoding="utf-8")
                    existing = LifecycleOwnershipMetadata.model_validate_json(existing_data)
                    pid = existing.process_id
                    is_alive = True
                    try:
                        os.kill(pid, 0)
                    except OSError as kill_exc:
                        import errno
                        if kill_exc.errno == errno.EPERM or getattr(kill_exc, "winerror", None) == 5:
                            is_alive = True
                        else:
                            is_alive = False
                    
                    if not is_alive:
                        self.claim_path.unlink()
                        await asyncio.to_thread(self._acquire_sync)
                        self._owned = True
                        return
                except Exception:
                    pass

                raise LifecycleOwnershipError(
                    "Durable lifecycle state is already owned or requires operator reconciliation"
                ) from exc
            except Exception as exc:
                raise LifecycleOwnershipError("Unable to claim durable lifecycle state") from exc
            self._owned = True

    async def release(self) -> None:
        async with self._lock:
            if not self._owned:
                return
            try:
                await asyncio.to_thread(self._release_sync)
            except LifecycleOwnershipError:
                raise
            except Exception as exc:
                raise LifecycleOwnershipError("Unable to release durable lifecycle ownership") from exc
            self._owned = False

    def _acquire_sync(self) -> None:
        self.claim_path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        descriptor = os.open(self.claim_path, flags, 0o600)
        try:
            payload = self._metadata.model_dump_json().encode("utf-8")
            os.write(descriptor, payload)
            os.fsync(descriptor)
        except Exception:
            os.close(descriptor)
            self.claim_path.unlink(missing_ok=True)
            raise
        else:
            os.close(descriptor)

    def _release_sync(self) -> None:
        try:
            existing = LifecycleOwnershipMetadata.model_validate_json(self.claim_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise LifecycleOwnershipError("Lifecycle ownership claim cannot be verified") from exc
        if existing.claim_id != self._metadata.claim_id:
            raise LifecycleOwnershipError("Lifecycle ownership claim belongs to another host")
        self.claim_path.unlink()
