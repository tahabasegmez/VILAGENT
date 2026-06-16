"""Observation metadata and blob storage kept outside LangGraph checkpoints."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import uuid
from collections import defaultdict, deque
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from vilagent.computer_use.models import BlobRef, Observation

_BLOB_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


class ObservationNotFoundError(KeyError):
    pass


class BlobNotFoundError(KeyError):
    pass


class ObservationStoreCorruptError(RuntimeError):
    pass


class BlobExportDeniedError(PermissionError):
    pass


class ObservationStorageQuotaError(RuntimeError):
    pass


class InMemoryObservationStore:
    """Bounded in-memory store suitable for tests and initial local execution."""

    def __init__(self, *, max_observations_per_session: int = 100):
        if max_observations_per_session < 1:
            raise ValueError("max_observations_per_session must be at least 1")
        self._max_observations_per_session = max_observations_per_session
        self._observations: dict[str, Observation] = {}
        self._session_observation_ids: dict[str, deque[str]] = defaultdict(deque)
        self._blobs: dict[str, bytes] = {}
        self._lock = asyncio.Lock()

    async def put_blob(self, data: bytes, *, media_type: str) -> BlobRef:
        digest = hashlib.sha256(data).hexdigest()
        blob_id = uuid.uuid4().hex
        async with self._lock:
            self._blobs[blob_id] = bytes(data)
        return BlobRef(blob_id=blob_id, media_type=media_type, size_bytes=len(data), sha256=digest)

    async def get_blob(self, blob_id: str) -> bytes:
        async with self._lock:
            data = self._blobs.get(blob_id)
        if data is None:
            raise BlobNotFoundError(blob_id)
        return bytes(data)

    async def get_exportable_blob(self, observation_id: str, blob_id: str) -> bytes:
        async with self._lock:
            observation = self._observations.get(observation_id)
            if observation is None:
                raise ObservationNotFoundError(observation_id)
            self._assert_export_allowed_locked(observation, blob_id)
            data = self._blobs.get(blob_id)
        if data is None:
            raise BlobNotFoundError(blob_id)
        return bytes(data)

    async def save(self, observation: Observation) -> None:
        async with self._lock:
            self._assert_blob_exists_locked(observation.screenshot_ref)
            self._assert_blob_exists_locked(observation.ui_tree_ref)
            is_new = observation.observation_id not in self._observations
            self._observations[observation.observation_id] = observation.model_copy(deep=True)
            if not is_new:
                return
            ids = self._session_observation_ids[observation.session_id]
            ids.append(observation.observation_id)
            while len(ids) > self._max_observations_per_session:
                evicted_id = ids.popleft()
                self._observations.pop(evicted_id, None)

    async def get(self, observation_id: str) -> Observation:
        async with self._lock:
            observation = self._observations.get(observation_id)
        if observation is None:
            raise ObservationNotFoundError(observation_id)
        return observation.model_copy(deep=True)

    async def list_session(self, session_id: str) -> list[Observation]:
        async with self._lock:
            result = [self._observations[observation_id].model_copy(deep=True) for observation_id in self._session_observation_ids.get(session_id, ()) if observation_id in self._observations]
        return result

    async def delete_session(self, session_id: str) -> None:
        async with self._lock:
            ids = self._session_observation_ids.pop(session_id, ())
            for observation_id in ids:
                self._observations.pop(observation_id, None)

    def _assert_blob_exists_locked(self, ref: BlobRef | None) -> None:
        if ref is not None and ref.blob_id not in self._blobs:
            raise BlobNotFoundError(ref.blob_id)

    @staticmethod
    def _assert_export_allowed_locked(observation: Observation, blob_id: str) -> None:
        referenced_ids = {ref.blob_id for ref in (observation.screenshot_ref, observation.ui_tree_ref) if ref is not None}
        if blob_id not in referenced_ids:
            raise BlobNotFoundError(blob_id)
        if not observation.redaction_applied:
            raise BlobExportDeniedError("Observation blob export requires applied redaction")


class JsonFileObservationStore(InMemoryObservationStore):
    """Restart-safe observation metadata and blobs stored outside checkpoints."""

    def __init__(
        self,
        path: str | Path,
        *,
        max_observations_per_session: int = 100,
        retention_hours: int | None = None,
        max_storage_bytes: int | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        super().__init__(max_observations_per_session=max_observations_per_session)
        if retention_hours is not None and retention_hours < 0:
            raise ValueError("retention_hours must be non-negative")
        if max_storage_bytes is not None and max_storage_bytes < 1:
            raise ValueError("max_storage_bytes must be positive")
        self._path = Path(path)
        self._metadata_path = self._path / "observations.json"
        self._blob_dir = self._path / "blobs"
        self._retention_hours = retention_hours
        self._max_storage_bytes = max_storage_bytes
        self._clock = clock or (lambda: datetime.now(UTC))
        self._load()

    async def put_blob(self, data: bytes, *, media_type: str) -> BlobRef:
        payload = bytes(data)
        digest = hashlib.sha256(payload).hexdigest()
        blob_id = uuid.uuid4().hex
        await asyncio.to_thread(self._write_blob, blob_id, payload)
        return BlobRef(blob_id=blob_id, media_type=media_type, size_bytes=len(payload), sha256=digest)

    async def get_blob(self, blob_id: str) -> bytes:
        return await asyncio.to_thread(self._read_blob, blob_id)

    async def get_exportable_blob(self, observation_id: str, blob_id: str) -> bytes:
        async with self._lock:
            observation = self._observations.get(observation_id)
            if observation is None:
                raise ObservationNotFoundError(observation_id)
            self._assert_export_allowed_locked(observation, blob_id)
            return await asyncio.to_thread(self._read_blob, blob_id)

    async def save(self, observation: Observation) -> None:
        async with self._lock:
            await asyncio.to_thread(self._assert_blob_ref, observation.screenshot_ref)
            await asyncio.to_thread(self._assert_blob_ref, observation.ui_tree_ref)
            try:
                self._assert_observation_fits_quota(observation)
            except ObservationStorageQuotaError:
                await asyncio.to_thread(self._delete_orphan_blobs_locked)
                raise
            is_new = observation.observation_id not in self._observations
            self._observations[observation.observation_id] = observation.model_copy(deep=True)
            if is_new:
                ids = self._session_observation_ids[observation.session_id]
                ids.append(observation.observation_id)
                while len(ids) > self._max_observations_per_session:
                    self._observations.pop(ids.popleft(), None)
            self._prune_expired_locked()
            self._prune_to_quota_locked(protected_observation_id=observation.observation_id)
            await asyncio.to_thread(self._persist_locked)
            await asyncio.to_thread(self._delete_orphan_blobs_locked)

    async def delete_session(self, session_id: str) -> None:
        async with self._lock:
            ids = self._session_observation_ids.pop(session_id, ())
            for observation_id in ids:
                self._observations.pop(observation_id, None)
            await asyncio.to_thread(self._persist_locked)
            await asyncio.to_thread(self._delete_orphan_blobs_locked)

    def _load(self) -> None:
        self._metadata_path.with_suffix(".json.tmp").unlink(missing_ok=True)
        if not self._metadata_path.exists():
            self._delete_orphan_blobs_locked()
            return
        try:
            payload = self._metadata_path.read_text(encoding="utf-8")
            observations = [Observation.model_validate_json(item) for item in json.loads(payload)["observations"]]
            for observation in observations:
                self._assert_blob_ref(observation.screenshot_ref)
                self._assert_blob_ref(observation.ui_tree_ref)
                self._observations[observation.observation_id] = observation
                self._session_observation_ids[observation.session_id].append(observation.observation_id)
            changed = self._prune_expired_locked()
            changed = self._prune_to_quota_locked() or changed
            if changed:
                self._persist_locked()
            self._delete_orphan_blobs_locked()
        except (KeyError, OSError, ValueError, TypeError) as exc:
            raise ObservationStoreCorruptError("Observation store metadata or blob integrity check failed") from exc

    def _persist_locked(self) -> None:
        self._path.mkdir(parents=True, exist_ok=True)
        temporary_path = self._metadata_path.with_suffix(".json.tmp")
        payload = {"observations": [item.model_dump_json() for item in self._observations.values()]}
        with temporary_path.open("w", encoding="utf-8") as temporary:
            json.dump(payload, temporary, separators=(",", ":"))
            temporary.flush()
            os.fsync(temporary.fileno())
        temporary_path.replace(self._metadata_path)

    def _write_blob(self, blob_id: str, data: bytes) -> None:
        self._blob_dir.mkdir(parents=True, exist_ok=True)
        path = self._blob_path(blob_id)
        temporary_path = path.with_suffix(".tmp")
        with temporary_path.open("wb") as temporary:
            temporary.write(data)
            temporary.flush()
            os.fsync(temporary.fileno())
        temporary_path.replace(path)

    def _read_blob(self, blob_id: str) -> bytes:
        try:
            return self._blob_path(blob_id).read_bytes()
        except FileNotFoundError as exc:
            raise BlobNotFoundError(blob_id) from exc

    def _assert_blob_ref(self, ref: BlobRef | None) -> None:
        if ref is None:
            return
        data = self._read_blob(ref.blob_id)
        if len(data) != ref.size_bytes or hashlib.sha256(data).hexdigest() != ref.sha256:
            raise ObservationStoreCorruptError("Observation blob integrity check failed")

    def _prune_expired_locked(self) -> bool:
        if self._retention_hours is None:
            return False
        cutoff = self._clock() - timedelta(hours=self._retention_hours)
        expired_ids = {item.observation_id for item in self._observations.values() if item.created_at <= cutoff}
        if not expired_ids:
            return False
        for observation_id in expired_ids:
            self._observations.pop(observation_id, None)
        for session_id, ids in list(self._session_observation_ids.items()):
            retained = deque(observation_id for observation_id in ids if observation_id not in expired_ids)
            if retained:
                self._session_observation_ids[session_id] = retained
            else:
                self._session_observation_ids.pop(session_id, None)
        return True

    def _delete_orphan_blobs_locked(self) -> None:
        if not self._blob_dir.exists():
            return
        referenced = {
            ref.blob_id
            for observation in self._observations.values()
            for ref in (observation.screenshot_ref, observation.ui_tree_ref)
            if ref is not None
        }
        for path in self._blob_dir.iterdir():
            if path.is_file() and (path.suffix == ".tmp" or path.name not in referenced):
                path.unlink()

    def _assert_observation_fits_quota(self, observation: Observation) -> None:
        if self._max_storage_bytes is None:
            return
        unique_refs = {ref.blob_id: ref for ref in (observation.screenshot_ref, observation.ui_tree_ref) if ref is not None}
        if sum(ref.size_bytes for ref in unique_refs.values()) > self._max_storage_bytes:
            raise ObservationStorageQuotaError("Observation exceeds persistent storage quota")

    def _prune_to_quota_locked(self, *, protected_observation_id: str | None = None) -> bool:
        if self._max_storage_bytes is None:
            return False
        changed = False
        while self._referenced_blob_bytes_locked() > self._max_storage_bytes:
            oldest = min(
                (item for item in self._observations.values() if item.observation_id != protected_observation_id),
                key=lambda item: item.created_at,
                default=None,
            )
            if oldest is None:
                raise ObservationStorageQuotaError("Unable to satisfy persistent storage quota")
            self._remove_observation_locked(oldest)
            changed = True
        return changed

    def _referenced_blob_bytes_locked(self) -> int:
        refs = {
            ref.blob_id: ref
            for observation in self._observations.values()
            for ref in (observation.screenshot_ref, observation.ui_tree_ref)
            if ref is not None
        }
        return sum(ref.size_bytes for ref in refs.values())

    def _remove_observation_locked(self, observation: Observation) -> None:
        self._observations.pop(observation.observation_id, None)
        ids = self._session_observation_ids.get(observation.session_id)
        if ids is None:
            return
        retained = deque(item for item in ids if item != observation.observation_id)
        if retained:
            self._session_observation_ids[observation.session_id] = retained
        else:
            self._session_observation_ids.pop(observation.session_id, None)

    def _blob_path(self, blob_id: str) -> Path:
        if _BLOB_ID_PATTERN.fullmatch(blob_id) is None:
            raise BlobNotFoundError(blob_id)
        return self._blob_dir / blob_id
