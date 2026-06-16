"""Tests for fail-closed durable lifecycle ownership."""

from __future__ import annotations

import asyncio

import pytest

from vilagent.computer_use.lifecycle_ownership import LifecycleOwnershipClaim, LifecycleOwnershipError


def test_only_one_claim_can_own_a_lifecycle_path(tmp_path):
    async def run():
        first = LifecycleOwnershipClaim(tmp_path / "lifecycle.json", owner_name="first")
        second = LifecycleOwnershipClaim(tmp_path / "lifecycle.json", owner_name="second")

        await first.acquire()
        with pytest.raises(LifecycleOwnershipError, match="already owned"):
            await second.acquire()

        await first.release()
        await second.acquire()
        await second.release()

    asyncio.run(run())


def test_stale_or_corrupt_claim_blocks_automatic_recovery(tmp_path):
    async def run():
        path = tmp_path / "lifecycle.json.owner"
        path.write_text("{not-json", encoding="utf-8")
        claim = LifecycleOwnershipClaim(tmp_path / "lifecycle.json")

        with pytest.raises(LifecycleOwnershipError, match="already owned"):
            await claim.acquire()

        assert path.exists()

    asyncio.run(run())


def test_release_refuses_a_replaced_claim(tmp_path):
    async def run():
        claim = LifecycleOwnershipClaim(tmp_path / "lifecycle.json")
        await claim.acquire()
        claim.claim_path.write_text(
            claim.claim_path.read_text(encoding="utf-8").replace(claim._metadata.claim_id, "another-host"),
            encoding="utf-8",
        )

        with pytest.raises(LifecycleOwnershipError, match="another host"):
            await claim.release()

        assert claim.claim_path.exists()

    asyncio.run(run())
