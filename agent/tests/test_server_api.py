"""Gateway API tests for the operator-facing computer-use routes."""

from __future__ import annotations

import asyncio
import json
import sys
import time

import pytest
from fakes import FakeEnv
from fastapi import FastAPI
from fastapi.testclient import TestClient

from vilagent.agents.common import EnvironmentContext, Plan, PlanStep, RiskLevel, StepResult, StepStatus
from vilagent.config.computer_use_config import ComputerUseConfig
from vilagent.control import Control
from vilagent.runs.manager import RunManager
from vilagent.runs.records import RunRecords
from vilagent.runs.session import RunSession
from vilagent.server import activity as activity_module
from vilagent.server import api as computer_use
from vilagent.server import state
from vilagent.server.deps import internal_auth_headers
from vilagent.server.runtime import Runtime


def _runtime(runs_dir) -> Runtime:
    control = Control()
    return Runtime(control=control, desktop=FakeEnv(control=control), browser=FakeEnv("browser", control=control), runs=RunManager(RunRecords(runs_dir)))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # Isolate the persisted UI selections so tests never touch the real state file.
    monkeypatch.setattr(state, "STATE_FILE_PATH", tmp_path / "state.json")
    app = FastAPI()
    app.include_router(computer_use.router)
    app.state.runtime = _runtime(tmp_path / "runs")
    # One event loop for the whole test: runs keep going in the background after POST /runs.
    with TestClient(app, headers=internal_auth_headers()) as test_client:
        yield test_client


def _events(client, run_id: str, last_event_id: int | None = None) -> list[tuple[str, dict]]:
    """Read a run's event stream to the end: [(type, data), ...]."""
    headers = {"Last-Event-ID": str(last_event_id)} if last_event_id is not None else {}
    body = client.get(f"/api/computer-use/runs/{run_id}/events", headers=headers).text.replace("\r\n", "\n")
    events = []
    for block in body.split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line and not line.startswith(":"))
        if "event" in fields:
            events.append((fields["event"], json.loads(fields["data"])))
    return events


def _start(client, monkeypatch, run_graph) -> str:
    monkeypatch.setattr(computer_use, "planner_model", lambda config: "model")
    # No connection is set up in these tests, so the planner's client is never really built.
    monkeypatch.setattr(computer_use, "planner_factory", lambda config: (lambda thinking=False: None))
    monkeypatch.setattr(computer_use, "JsonLLMPlanner", lambda *a, **k: None)
    monkeypatch.setattr(computer_use, "run_graph", run_graph)
    response = client.post("/api/computer-use/runs", json={"thread_id": "t", "prompt": "open notepad"})
    assert response.status_code == 202, response.text
    return response.json()["run_id"]


def test_router_rejects_non_internal_call():
    app = FastAPI()
    app.include_router(computer_use.router)

    assert TestClient(app).get("/api/computer-use/execution-mode").status_code == 403


def test_emergency_stop_requires_the_runtime():
    app = FastAPI()
    app.include_router(computer_use.router)
    response = TestClient(app, headers=internal_auth_headers()).get("/api/computer-use/emergency-stop")

    assert response.status_code == 503


def test_emergency_stop_engage_and_reset(client):
    engaged = client.post("/api/computer-use/emergency-stop/engage", json={"reason": "operator stop"})
    status = client.get("/api/computer-use/emergency-stop")
    reset = client.post("/api/computer-use/emergency-stop/reset", json={"reason": "operator reset"})

    assert engaged.json() == {"engaged": True, "reason": "operator stop"}
    assert status.json()["engaged"] is True
    assert reset.json() == {"engaged": False, "reason": None}


@pytest.mark.parametrize(
    ("path", "payload", "key", "default"),
    [
        ("/api/computer-use/execution-mode", {"execution_mode": "vision_only"}, "execution_mode", "hybrid"),
        ("/api/computer-use/approach", {"approach": "brief"}, "approach", "plan"),
        ("/api/computer-use/vision/recovery", {"enabled": True}, "enabled", False),
        ("/api/computer-use/vision/verifier", {"verifier": "supervisor"}, "verifier", "fara"),
        ("/api/computer-use/approvals/threshold", {"threshold": "medium"}, "threshold", "high"),
    ],
)
def test_ui_selections_persist(client, path, payload, key, default):
    assert client.get(path).json()[key] == default

    updated = client.post(path, json=payload)

    assert updated.status_code == 200, updated.text
    assert client.get(path).json()[key] == payload[key]


def test_the_approach_names_the_ui_uses_are_the_stored_ones(client):
    """Plan, brief, direct — and a state file from before the rename still reads."""
    assert client.get("/api/computer-use/approach").json() == {"approach": "plan", "options": ["plan", "brief", "direct"]}
    assert client.post("/api/computer-use/approach", json={"approach": "plan_execute"}).status_code == 422

    state.set_state_value("agent_approach", "autonomous")  # written by an earlier version
    assert client.get("/api/computer-use/approach").json()["approach"] == "brief"

    state.set_state_value("agent_approach", "plan_execute")
    assert client.get("/api/computer-use/status").json()["approach"] == "plan"


def test_ui_selections_reject_unknown_values(client):
    assert client.post("/api/computer-use/execution-mode", json={"execution_mode": "nope"}).status_code == 422
    assert client.post("/api/computer-use/approach", json={"approach": "nope"}).status_code == 422
    assert client.post("/api/computer-use/vision/verifier", json={"verifier": "nope"}).status_code == 422


def test_task_run_rejects_legacy_approval_threshold(client):
    response = client.post(
        "/api/computer-use/runs",
        json={"thread_id": "t1", "prompt": "open notepad", "auto_approve_risk_threshold": "low"},
    )

    assert response.status_code == 422


def test_events_of_an_unknown_run_are_404(client):
    assert client.get("/api/computer-use/runs/missing/events").status_code == 404


def test_logs_reject_unknown_source(client):
    response = client.get("/api/computer-use/logs/nope")

    assert response.status_code == 200
    assert "Unknown log source" in response.text


def _plan() -> Plan:
    return Plan(
        goal="Sort Downloads",
        steps=[PlanStep(step_id=f"s{i}", instruction=f"Step {i}.", risk={"level": RiskLevel.low}) for i in (1, 2, 3)],
    )


def test_plan_activity_marks_exactly_one_running_step():
    items = activity_module.plan_step_activity(_plan(), [], "s2")

    assert [item.status for item in items] == ["pending", "running", "pending"]
    assert [item.max_actions for item in items] == [8, 8, 8]


def test_plan_activity_reports_results_and_errors():
    results = [
        StepResult(step_id="s1", environment=EnvironmentContext.native, requires_vision=False, status=StepStatus.completed, summary="done"),
        StepResult(step_id="s2", environment=EnvironmentContext.native, requires_vision=True, status=StepStatus.failed, error_code="step_failed", summary="failed"),
    ]

    items = activity_module.plan_step_activity(_plan(), results, None)

    assert [item.status for item in items] == ["completed", "failed", "pending"]
    assert items[1].error_code == "step_failed"


def test_activity_updates_follow_the_reporting_role():
    activity = activity_module.new_activity("t", "r", "task", "model", ComputerUseConfig())

    activity_module.update_activity(activity, "vision", "clicking", "I see the button")

    running = [agent for agent in activity.agents if agent.status == "running"]
    assert [(a.agent_id, a.last_event, a.current_thought) for a in running] == [("vision_executor", "clicking", "I see the button")]


def test_task_run_request_takes_only_thread_and_prompt():
    assert computer_use.ComputerUseTaskRunRequest(thread_id="thread", prompt="Open Calculator").prompt == "Open Calculator"


def test_a_run_streams_activity_then_its_result(client, monkeypatch):
    async def done(graph, context, **kwargs):
        context.on_activity("vision", "clicking", "I see the Save button.")
        raise RuntimeError("planner exploded")

    run_id = _start(client, monkeypatch, done)
    events = _events(client, run_id)

    types = [kind for kind, _ in events]
    assert types[0] == "activity" and types[-1] == "run.finished"
    assert any(data["activity"]["agents"][1]["current_thought"] == "I see the Save button." for kind, data in events if kind == "activity")
    assert events[-1][1]["status"] == "failed" and events[-1][1]["error"] == "planner exploded"
    # Replay after an event id skips what the client already has.
    assert [kind for kind, _ in _events(client, run_id, last_event_id=len(events) - 1)] == ["run.finished"]


def test_stopped_run_is_reported_recorded_and_listed(client, monkeypatch):
    runtime = client.app.state.runtime

    async def hanging(*args, **kwargs):
        await runtime.control.engage("operator pressed stop")
        await asyncio.sleep(3600)

    run_id = _start(client, monkeypatch, hanging)
    finished = _events(client, run_id)[-1][1]

    assert finished["status"] == "stopped" and finished["error"] == "operator pressed stop"
    assert "Stopped" in finished["output"]["messages"][0]["content"]
    assert runtime.runs.active is None
    assert [run["status"] for run in client.get("/api/computer-use/runs").json()] == ["stopped"]
    assert client.get(f"/api/computer-use/runs/{run_id}").json()["error"] == "operator pressed stop"


def test_cancel_ends_only_this_run(client, monkeypatch):
    runtime = client.app.state.runtime
    started = asyncio.Event()

    async def hanging(*args, **kwargs):
        started.set()
        await asyncio.sleep(3600)

    run_id = _start(client, monkeypatch, hanging)
    assert client.get(f"/api/computer-use/runs/{run_id}").json()["status"] == "running"

    assert client.post(f"/api/computer-use/runs/{run_id}/cancel").status_code == 200
    finished = _events(client, run_id)[-1][1]

    assert finished["status"] == "cancelled"
    assert runtime.control.stopped is False  # unlike the emergency stop, nothing is latched
    assert client.post(f"/api/computer-use/runs/{run_id}/cancel").status_code == 404


def _session(run_id: str = "busy-run", thread_id: str = "t") -> RunSession:
    activity = activity_module.new_activity(thread_id, run_id, "task", "model", ComputerUseConfig())
    return RunSession(run_id=run_id, thread_id=thread_id, prompt="task", approach="plan_execute", execution_mode="hybrid", activity=activity)


def test_second_run_is_rejected_while_one_is_active(client):
    client.app.state.runtime.runs.active = _session()

    response = client.post("/api/computer-use/runs", json={"thread_id": "t", "prompt": "open notepad"})

    assert response.status_code == 409
    assert "busy-run" in response.json()["detail"]


def test_run_id_must_be_file_safe(client):
    response = client.post("/api/computer-use/runs", json={"thread_id": "t", "run_id": "../evil", "prompt": "open notepad"})

    assert response.status_code == 422


def test_unknown_run_is_404(client):
    assert client.get("/api/computer-use/runs/nope").status_code == 404


def test_spent_budget_ends_the_run_with_its_reason(client, monkeypatch):
    from vilagent.runs.budget import BudgetExhausted

    async def spending(*args, **kwargs):
        raise BudgetExhausted("planner", 6)

    run_id = _start(client, monkeypatch, spending)
    finished = _events(client, run_id)[-1][1]
    record = client.get(f"/api/computer-use/runs/{run_id}").json()

    assert finished["output"]["error_code"] == "budget_exhausted:planner"
    assert finished["error"] == "The run used all 6 planner-model calls."
    assert (record["status"], record["budget"]["limits"]["planner"]) == ("failed", 6)


def _asking(client, monkeypatch) -> str:
    async def asking(graph, context, **kwargs):
        answer = await context.ask({"title": "Send the email", "reasons": ["Messages others"], "level": "high"})
        if not answer["approve"]:
            raise RuntimeError(f"declined: {answer['reason']}")
        raise RuntimeError("approved")  # ends the fake run either way

    run_id = _start(client, monkeypatch, asking)
    runs = client.app.state.runtime.runs
    for _ in range(200):
        if runs.active is not None and runs.active.approvals.pending:
            return run_id
        time.sleep(0.01)
    raise AssertionError("the run never asked")


def test_the_operator_answers_a_pending_approval(client, monkeypatch):
    run_id = _asking(client, monkeypatch)
    runs = client.app.state.runtime.runs
    (approval_id,) = runs.active.approvals.pending
    assert client.get(f"/api/computer-use/runs/{run_id}").json()["status"] == "awaiting_approval"

    assert client.post(f"/api/computer-use/runs/{run_id}/approvals/{approval_id}", json={"approve": False}).status_code == 200
    events = _events(client, run_id)

    kinds = [kind for kind, _ in events]
    assert "approval.requested" in kinds and "approval.resolved" in kinds
    assert events[-1][1]["error"] == "declined: The operator declined."
    assert client.post(f"/api/computer-use/runs/{run_id}/approvals/{approval_id}", json={"approve": True}).status_code == 404


def test_emergency_stop_resolves_a_pending_approval(client, monkeypatch):
    run_id = _asking(client, monkeypatch)

    client.post("/api/computer-use/emergency-stop/engage", json={"reason": "operator stop"})
    events = _events(client, run_id)

    resolved = [data for kind, data in events if kind == "approval.resolved"]
    assert resolved and resolved[0]["approve"] is False
    assert events[-1][1]["status"] == "stopped"


def test_resume_and_discard_only_accept_interrupted_runs(client, monkeypatch):
    runtime = client.app.state.runtime
    interrupted = _session("cut-off")
    interrupted.status = "interrupted"
    runtime.runs.records.write(interrupted)
    resumed = []

    async def resuming(graph, context, **kwargs):
        resumed.append(kwargs["resume"])
        raise RuntimeError("stop here")

    monkeypatch.setattr(computer_use, "planner_model", lambda config: "model")
    monkeypatch.setattr(computer_use, "planner_factory", lambda config: (lambda thinking=False: None))
    monkeypatch.setattr(computer_use, "JsonLLMPlanner", lambda *a, **k: None)
    monkeypatch.setattr(computer_use, "run_graph", resuming)

    assert client.post("/api/computer-use/runs/nope/resume").status_code == 404
    response = client.post("/api/computer-use/runs/cut-off/resume")
    assert response.status_code == 202 and response.json()["thread_id"] == "t"
    _events(client, "cut-off")
    assert resumed == [True]
    assert client.post("/api/computer-use/runs/cut-off/resume").status_code == 409  # now failed, not interrupted

    other = _session("other-cut")
    other.status = "interrupted"
    runtime.runs.records.write(other)
    assert client.post("/api/computer-use/runs/other-cut/discard").status_code == 200
    assert client.get("/api/computer-use/runs/other-cut").json()["error"] == "Discarded by the operator."
    assert client.post("/api/computer-use/runs/other-cut/discard").status_code == 409


def test_windows_gateway_keeps_a_loop_that_can_start_playwright(monkeypatch):
    """uvicorn's --reload loop on Windows cannot spawn the Playwright driver (NotImplementedError)."""
    from vilagent.__main__ import loop_kwargs

    monkeypatch.setattr(sys, "platform", "win32")
    assert loop_kwargs() == {"loop": "none"}
    monkeypatch.setattr(sys, "platform", "linux")
    assert loop_kwargs() == {}
