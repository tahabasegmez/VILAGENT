"""Tests for single-writer desktop lease behavior."""

from __future__ import annotations

import asyncio

import pytest

from vilagent.computer_use.lease import DesktopLease, DesktopLeaseOwnershipError, DesktopLeaseTimeoutError, DesktopLeaseToken


def test_lease_serializes_mutating_owners():
    async def run():
        lease = DesktopLease(stale_after_seconds=5)
        first = await lease.acquire("run-1")
        with pytest.raises(DesktopLeaseTimeoutError):
            await lease.acquire("run-2", timeout_seconds=0.01)
        await lease.release(first)

        second = await lease.acquire("run-2", timeout_seconds=0.1)
        assert second.owner_id == "run-2"

    asyncio.run(run())


def test_stale_lease_is_recovered():
    async def run():
        lease = DesktopLease(stale_after_seconds=0.01)
        await lease.acquire("abandoned")
        await asyncio.sleep(0.02)

        recovered = await lease.acquire("recovery", timeout_seconds=0.1)
        assert recovered.owner_id == "recovery"

    asyncio.run(run())


def test_non_owner_cannot_release_lease():
    async def run():
        lease = DesktopLease()
        await lease.acquire("run-1")
        fake = DesktopLeaseToken(token_id="fake", owner_id="run-1", acquired_at=0)
        with pytest.raises(DesktopLeaseOwnershipError):
            await lease.release(fake)

    asyncio.run(run())
