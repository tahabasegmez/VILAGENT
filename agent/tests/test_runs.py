"""Run sessions, the single-run manager and the on-disk run records."""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from vilagent.config.computer_use_config import ComputerUseConfig
from vilagent.runs.events import EventBus
from vilagent.runs.manager import RunActive, RunManager
from vilagent.runs.records import TYPED_TEXT, RunRecords
from vilagent.runs.session import RunSession
from vilagent.server.activity import new_activity


def session(run_id: str = "r1", thread_id: str = "t") -> RunSession:
    activity = new_activity(thread_id, run_id, "task", "model", ComputerUseConfig())
    return RunSession(run_id=run_id, thread_id=thread_id, prompt="task", approach="plan_execute", execution_mode="hybrid", activity=activity)


def test_only_one_run_is_active(tmp_path):
    manager = RunManager(RunRecords(tmp_path))

    with manager.open(session("first")):
        with pytest.raises(RunActive) as rejected:
            with manager.open(session("second")):
                pass
        assert rejected.value.run_id == "first"

    assert manager.active is None
    with manager.open(session("third")):
        assert manager.active.run_id == "third"


def test_an_unfinished_run_is_recorded_as_failed_and_released(tmp_path):
    manager = RunManager(RunRecords(tmp_path))

    with pytest.raises(RuntimeError):
        with manager.open(session()):
            raise RuntimeError("boom")

    assert manager.active is None
    assert manager.records.read("r1")["status"] == "failed"
    assert manager.get("r1").run_id == "r1"
    assert manager.get("other") is None


def test_finish_writes_the_final_record(tmp_path):
    manager = RunManager(RunRecords(tmp_path))
    run = session()

    with manager.open(run):
        assert manager.records.read("r1")["status"] == "running"
        manager.finish(run, "completed", {"status": "completed"})

    record = manager.records.read("r1")
    assert (record["status"], record["output"], record["ended_at"] is not None) == ("completed", {"status": "completed"}, True)


def test_typed_text_is_scrubbed_on_disk_only(tmp_path):
    records = RunRecords(tmp_path)
    run = session()
    run.output = {"plan": {"steps": [{"step_id": "s1", "args": {"text": "my secret", "keys": "ENTER"}}]}}

    records.write(run)

    assert records.read("r1")["output"]["plan"]["steps"][0]["args"] == {"text": TYPED_TEXT, "keys": "ENTER"}
    assert run.output["plan"]["steps"][0]["args"]["text"] == "my secret"


def test_recover_marks_cut_off_runs_interrupted_and_prunes(tmp_path):
    records = RunRecords(tmp_path, retention=2)
    for index, status in enumerate(["completed", "running", "awaiting_approval"]):
        run = session(f"r{index}")
        run.status = status
        records.write(run)
        os.utime(tmp_path / f"r{index}.json", (index, index))  # r0 is the oldest

    assert records.recover() == 2

    assert sorted(path.stem for path in tmp_path.glob("*.json")) == ["r1", "r2"]
    assert {json.loads(path.read_text(encoding="utf-8"))["status"] for path in tmp_path.glob("*.json")} == {"interrupted"}


def test_list_is_newest_first_with_summary_fields(tmp_path):
    records = RunRecords(tmp_path)
    for run_id, started in (("old", "2026-01-01"), ("new", "2026-02-01")):
        run = session(run_id)
        run.started_at = started
        records.write(run)

    listed = records.list()

    assert [run["run_id"] for run in listed] == ["new", "old"]
    assert "activity" not in listed[0] and "output" not in listed[0]


def test_started_run_claims_the_slot_immediately_and_can_be_cancelled(tmp_path):
    manager = RunManager(RunRecords(tmp_path))

    async def scenario():
        started = asyncio.Event()

        async def work():
            started.set()
            await asyncio.sleep(3600)

        task = manager.start(session("r1"), work)
        with pytest.raises(RunActive):
            manager.start(session("r2"), work)  # claimed before the first run even began
        await started.wait()
        assert manager.cancel("r1") and not manager.cancel("other")
        await task

    asyncio.run(scenario())

    record = manager.records.read("r1")
    assert (record["status"], record["error"], manager.active) == ("cancelled", "Cancelled by the operator.", None)


def test_event_bus_replays_and_follows_until_closed():
    bus = EventBus(size=3)

    async def scenario():
        for index in range(4):
            bus.publish("activity", {"n": index})  # the first falls out of the buffer
        seen = []

        async def follow():
            async for event in bus.follow(after=2):
                seen.append((event.seq, event.data.get("n")))

        follower = asyncio.create_task(follow())
        await asyncio.sleep(0)
        bus.publish("run.finished", {})
        bus.close()
        await follower
        return seen

    assert asyncio.run(scenario()) == [(3, 2), (4, 3), (5, None)]


def test_checkpoints_are_kept_only_for_interrupted_runs(tmp_path):
    forgotten = []

    async def forget(run_id):
        forgotten.append(run_id)

    manager = RunManager(RunRecords(tmp_path), forget=forget)

    async def scenario():
        async def done():
            manager.finish(manager.active, "completed")

        await manager.start(session("finished"), done)

        async def forever():
            await asyncio.sleep(3600)

        manager.start(session("interrupted"), forever)
        await asyncio.sleep(0)
        await manager.shutdown()

    asyncio.run(scenario())

    assert forgotten == ["finished"]
    record = manager.records.read("interrupted")
    assert record["status"] == "interrupted" and "Resume" in record["error"]


def test_discard_and_expiry_forget_interrupted_runs(tmp_path):
    forgotten = []

    async def forget(run_id):
        forgotten.append(run_id)

    records = RunRecords(tmp_path)
    for run_id, started in (("fresh", "2999-01-01T00:00:00+00:00"), ("stale", "2000-01-01T00:00:00+00:00"), ("done", "2000-01-01T00:00:00+00:00")):
        run = session(run_id)
        run.status, run.started_at = ("completed" if run_id == "done" else "interrupted"), started
        records.write(run)
    manager = RunManager(records, forget=forget)

    asyncio.run(manager.expire_interrupted(days=7))
    assert asyncio.run(manager.discard("fresh")) is True
    assert asyncio.run(manager.discard("done")) is False

    assert forgotten == ["stale", "fresh"]
    assert {run_id: records.read(run_id)["status"] for run_id in ("fresh", "stale", "done")} == {"fresh": "failed", "stale": "failed", "done": "completed"}


def test_budget_continues_from_what_was_spent():
    from vilagent.config.computer_use_config import ComputerUseBudgetConfig
    from vilagent.runs.budget import BudgetMeter

    meter = BudgetMeter.from_config(ComputerUseBudgetConfig(), spent={"used": {"vision": 7, "action": 3, "bogus": 1}, "active_seconds": 42})

    assert (meter.used["vision"], meter.used["action"], "bogus" in meter.used) == (7, 3, False)
    assert meter.active_seconds() >= 42
