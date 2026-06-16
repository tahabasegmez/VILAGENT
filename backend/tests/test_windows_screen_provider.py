"""Tests for the read-only Windows screen provider spike."""

from __future__ import annotations

import asyncio
import threading

from PIL import Image
import pytest

from vilagent.computer_use.models import WindowRef
from vilagent.computer_use.observation_store import BlobExportDeniedError, InMemoryObservationStore
from vilagent.computer_use.windows.screen import WindowsScreenProvider


def test_windows_screen_provider_stores_screenshot_outside_observation():
    async def run():
        store = InMemoryObservationStore()
        provider = WindowsScreenProvider(store, grabber=lambda: Image.new("RGB", (32, 24), color="white"))

        observation = await provider.observe("session-1")

        assert observation.screen_size.width == 32
        assert observation.screenshot_ref is not None
        assert await store.get_blob(observation.screenshot_ref.blob_id)
        assert "screenshot_ref" in observation.model_dump()
        assert "base64" not in observation.model_dump_json()

    asyncio.run(run())


def test_windows_screen_provider_calculates_normalized_diff():
    images = iter(
        [
            Image.new("RGB", (10, 10), color="black"),
            Image.new("RGB", (10, 10), color="white"),
        ]
    )

    async def run():
        store = InMemoryObservationStore()
        provider = WindowsScreenProvider(store, grabber=lambda: next(images))
        first = await provider.observe("session-1")
        second = await provider.observe("session-1", previous=first)

        assert second.previous_observation_id == first.observation_id
        assert second.diff_from_previous == 1.0

    asyncio.run(run())


def test_windows_screen_provider_records_active_window_metadata():
    async def run():
        store = InMemoryObservationStore()
        expected = WindowRef(window_id="hwnd:42", title="Editor", process_id=7)
        provider = WindowsScreenProvider(
            store,
            grabber=lambda: Image.new("RGB", (32, 24), color="white"),
            active_window_reader=lambda: expected,
        )

        observation = await provider.observe("session-1")

        assert observation.active_window == expected

    asyncio.run(run())


def test_windows_screen_provider_uses_disposable_thread_for_each_capture():
    thread_state = threading.local()

    def thread_affine_grabber():
        if getattr(thread_state, "used", False):
            raise RuntimeError("capture thread was reused")
        thread_state.used = True
        return Image.new("RGB", (8, 8), color="white")

    async def run():
        store = InMemoryObservationStore()
        provider = WindowsScreenProvider(store, grabber=thread_affine_grabber)

        await provider.observe("session-1")
        await provider.observe("session-1")

    asyncio.run(run())


def test_windows_screen_provider_uses_disposable_thread_for_each_redaction():
    thread_state = threading.local()

    def thread_affine_redactor(image):
        if getattr(thread_state, "used", False):
            raise RuntimeError("redaction thread was reused")
        thread_state.used = True
        return image

    async def run():
        store = InMemoryObservationStore()
        provider = WindowsScreenProvider(
            store,
            grabber=lambda: Image.new("RGB", (8, 8), color="white"),
            redact=thread_affine_redactor,
        )

        await provider.observe("session-1")
        await provider.observe("session-1")

    asyncio.run(run())


def test_windows_screen_provider_only_exports_after_configured_redaction():
    async def run():
        store = InMemoryObservationStore()
        unredacted = WindowsScreenProvider(store, grabber=lambda: Image.new("RGB", (8, 8), color="white"))
        raw_observation = await unredacted.observe("session-1")
        with pytest.raises(BlobExportDeniedError):
            await store.get_exportable_blob(raw_observation.observation_id, raw_observation.screenshot_ref.blob_id)

        redacted = WindowsScreenProvider(
            store,
            grabber=lambda: Image.new("RGB", (8, 8), color="white"),
            redact=lambda image: Image.new("RGB", image.size, color="black"),
        )
        safe_observation = await redacted.observe("session-1")
        assert safe_observation.redaction_applied is True
        assert await store.get_exportable_blob(safe_observation.observation_id, safe_observation.screenshot_ref.blob_id)

    asyncio.run(run())
