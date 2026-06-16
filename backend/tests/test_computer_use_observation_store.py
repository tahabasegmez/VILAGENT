"""Tests for checkpoint-external observation and blob storage."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from vilagent.computer_use.models import MonitorRef, Observation, Rect, Size
from vilagent.computer_use.observation_store import (
    BlobNotFoundError,
    BlobExportDeniedError,
    InMemoryObservationStore,
    JsonFileObservationStore,
    ObservationNotFoundError,
    ObservationStoreCorruptError,
    ObservationStorageQuotaError,
)


def _observation(observation_id: str, *, session_id: str = "session-1", screenshot_ref=None, created_at=None) -> Observation:
    return Observation(
        observation_id=observation_id,
        session_id=session_id,
        screenshot_ref=screenshot_ref,
        created_at=created_at or datetime.now(UTC),
        monitor=MonitorRef(monitor_id="primary", primary=True, bounds=Rect(x=0, y=0, width=1920, height=1080)),
        screen_size=Size(width=1920, height=1080),
    )


def test_blob_is_stored_separately_and_returned_as_copy():
    async def run():
        store = InMemoryObservationStore()
        payload = b"not-a-real-png"
        ref = await store.put_blob(payload, media_type="image/png")
        await store.save(_observation("obs-1", screenshot_ref=ref))

        stored = await store.get("obs-1")
        assert stored.screenshot_ref is not None
        assert stored.screenshot_ref.sha256 == hashlib.sha256(payload).hexdigest()
        assert await store.get_blob(ref.blob_id) == payload
        assert "not-a-real-png" not in stored.model_dump_json()

    asyncio.run(run())


def test_blob_export_requires_redacted_referencing_observation():
    async def run():
        store = InMemoryObservationStore()
        ref = await store.put_blob(b"screenshot", media_type="image/png")
        await store.save(_observation("unredacted", screenshot_ref=ref))

        with pytest.raises(BlobExportDeniedError):
            await store.get_exportable_blob("unredacted", ref.blob_id)
        with pytest.raises(BlobNotFoundError):
            await store.get_exportable_blob("unredacted", "not-referenced")

        redacted = _observation("redacted", screenshot_ref=ref).model_copy(update={"redaction_applied": True})
        await store.save(redacted)
        assert await store.get_exportable_blob("redacted", ref.blob_id) == b"screenshot"

    asyncio.run(run())


def test_json_file_store_deletes_blobs_evicted_by_history_limit(tmp_path):
    async def run():
        store = JsonFileObservationStore(tmp_path, max_observations_per_session=1)
        first_ref = await store.put_blob(b"first", media_type="image/png")
        await store.save(_observation("obs-1", screenshot_ref=first_ref))
        second_ref = await store.put_blob(b"second", media_type="image/png")
        await store.save(_observation("obs-2", screenshot_ref=second_ref))

        with pytest.raises(BlobNotFoundError):
            await store.get_blob(first_ref.blob_id)
        assert await store.get_blob(second_ref.blob_id) == b"second"

    asyncio.run(run())


def test_json_file_store_evicts_oldest_observations_to_storage_quota(tmp_path):
    async def run():
        store = JsonFileObservationStore(tmp_path, max_storage_bytes=6)
        first_ref = await store.put_blob(b"first", media_type="image/png")
        await store.save(_observation("obs-1", screenshot_ref=first_ref, created_at=datetime(2026, 1, 1, tzinfo=UTC)))
        second_ref = await store.put_blob(b"second", media_type="image/png")
        await store.save(_observation("obs-2", screenshot_ref=second_ref, created_at=datetime(2026, 1, 2, tzinfo=UTC)))

        assert [item.observation_id for item in await store.list_session("session-1")] == ["obs-2"]
        with pytest.raises(BlobNotFoundError):
            await store.get_blob(first_ref.blob_id)

    asyncio.run(run())


def test_json_file_store_rejects_single_observation_over_storage_quota(tmp_path):
    async def run():
        store = JsonFileObservationStore(tmp_path, max_storage_bytes=3)
        ref = await store.put_blob(b"large", media_type="image/png")

        with pytest.raises(ObservationStorageQuotaError):
            await store.save(_observation("obs-1", screenshot_ref=ref))
        assert await store.list_session("session-1") == []
        with pytest.raises(BlobNotFoundError):
            await store.get_blob(ref.blob_id)

    asyncio.run(run())


def test_json_file_store_prunes_expired_observations_and_orphan_blobs_on_restart(tmp_path):
    now = datetime(2026, 6, 9, tzinfo=UTC)

    async def run():
        store = JsonFileObservationStore(tmp_path)
        expired_ref = await store.put_blob(b"expired", media_type="image/png")
        await store.save(_observation("expired", screenshot_ref=expired_ref, created_at=now - timedelta(hours=25)))
        orphan_ref = await store.put_blob(b"orphan", media_type="image/png")

        restarted = JsonFileObservationStore(tmp_path, retention_hours=24, clock=lambda: now)

        assert await restarted.list_session("session-1") == []
        with pytest.raises(BlobNotFoundError):
            await restarted.get_blob(expired_ref.blob_id)
        with pytest.raises(BlobNotFoundError):
            await restarted.get_blob(orphan_ref.blob_id)

    asyncio.run(run())


def test_json_file_store_rejects_blob_path_traversal_and_removes_temporary_files(tmp_path):
    async def run():
        store = JsonFileObservationStore(tmp_path)
        with pytest.raises(BlobNotFoundError):
            await store.get_blob("../outside")

        blob_dir = tmp_path / "blobs"
        blob_dir.mkdir(parents=True, exist_ok=True)
        temporary = blob_dir / "interrupted.tmp"
        temporary.write_bytes(b"partial")

        JsonFileObservationStore(tmp_path)
        assert not temporary.exists()

    asyncio.run(run())


def test_json_file_store_removes_interrupted_metadata_write(tmp_path):
    temporary = tmp_path / "observations.json.tmp"
    tmp_path.mkdir(parents=True, exist_ok=True)
    temporary.write_text('{"observations":', encoding="utf-8")

    store = JsonFileObservationStore(tmp_path)

    assert not temporary.exists()
    assert asyncio.run(store.list_session("session-1")) == []


def test_json_file_store_fails_closed_for_malformed_published_metadata(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "observations.json").write_text('{"observations":', encoding="utf-8")

    with pytest.raises(ObservationStoreCorruptError):
        JsonFileObservationStore(tmp_path)


def test_observation_rejects_unknown_blob_reference():
    async def run():
        store = InMemoryObservationStore()
        ref = await store.put_blob(b"known", media_type="image/png")
        unknown = ref.model_copy(update={"blob_id": "missing"})
        with pytest.raises(BlobNotFoundError):
            await store.save(_observation("obs-1", screenshot_ref=unknown))

    asyncio.run(run())


def test_session_history_is_bounded():
    async def run():
        store = InMemoryObservationStore(max_observations_per_session=2)
        await store.save(_observation("obs-1"))
        await store.save(_observation("obs-2"))
        await store.save(_observation("obs-3"))

        assert [item.observation_id for item in await store.list_session("session-1")] == ["obs-2", "obs-3"]
        with pytest.raises(ObservationNotFoundError):
            await store.get("obs-1")

    asyncio.run(run())


def test_json_file_store_survives_restart_and_validates_blob(tmp_path):
    async def run():
        store = JsonFileObservationStore(tmp_path)
        payload = b"persistent-png"
        ref = await store.put_blob(payload, media_type="image/png")
        await store.save(_observation("obs-1", screenshot_ref=ref))

        restarted = JsonFileObservationStore(tmp_path)
        assert (await restarted.get("obs-1")).screenshot_ref == ref
        assert await restarted.get_blob(ref.blob_id) == payload

        (tmp_path / "blobs" / ref.blob_id).write_bytes(b"tampered")
        with pytest.raises(ObservationStoreCorruptError):
            JsonFileObservationStore(tmp_path)

    asyncio.run(run())
