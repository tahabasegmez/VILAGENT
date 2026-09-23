"""The eval runner (no live gateway: httpx MockTransport)."""

from __future__ import annotations

import json

import httpx

from evals import runner


def test_suite_loads_with_unique_ids_and_known_checks():
    tasks = runner.load_tasks()
    ids = [task["id"] for task in tasks]

    assert len(ids) == len(set(ids)) >= 10
    assert [task["id"] for task in runner.load_tasks(tag="verifier")] == ["form-long", "notepad-numbers", "paint-shapes"]
    for task in tasks:
        assert set(task) <= {"id", "prompt", "check", "approach", "execution_mode", "tags"}
        assert set(task.get("check") or {"manual": True}) <= {"file_contains", "manual"}
        assert task.get("approach", "plan") in {"plan", "brief"}
        assert task.get("execution_mode", "hybrid") in {"hybrid", "vision_only"}


def test_file_check_reads_the_eval_folder(tmp_path):
    check = {"file_contains": {"file": "note.txt", "text": "hello"}}
    assert not runner.check_passed(check, tmp_path)

    (tmp_path / "note.txt").write_text("say hello", encoding="utf-8")

    assert runner.check_passed(check, tmp_path)


def test_manual_check_asks_the_operator(tmp_path):
    assert runner.check_passed({"manual": True}, tmp_path, ask=lambda _: "y")
    assert not runner.check_passed({"manual": True}, tmp_path, ask=lambda _: "")


def test_run_task_sets_selections_and_records_metrics(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "POLL_SECONDS", 0)
    (tmp_path / "note.txt").write_text("stale", encoding="utf-8")
    seen: list[tuple[str, dict]] = []
    polls = iter([{"status": "running"}])

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        seen.append((request.url.path, body))
        if request.method == "POST" and request.url.path.endswith("/runs"):
            return httpx.Response(202, json={"run_id": "r1", "thread_id": body["thread_id"]})
        if request.url.path.endswith("/runs/r1"):
            if (running := next(polls, None)) is not None:
                return httpx.Response(200, json=running)
            (tmp_path / "note.txt").write_text("VILAGENT eval note", encoding="utf-8")
            output = {"status": "completed", "steps": [{"actions": 3}, {"actions": 1}], "replan_count": 1, "usage": {"planner_requests": 2, "vision_requests": 5, "vision_tokens": 900}}
            return httpx.Response(200, json={"status": "completed", "error": None, "output": output})
        return httpx.Response(200, json=body)

    task = {"id": "notepad", "execution_mode": "vision_only", "prompt": r"save as {eval_dir}\note.txt", "check": {"file_contains": {"file": "note.txt", "text": "eval note"}}}
    with httpx.Client(base_url="http://gateway/api/computer-use", transport=httpx.MockTransport(handler)) as api:
        record = runner.run_task(api, task, "lbl", tmp_path)

    assert [path.rsplit("/", 1)[-1] for path, _ in seen] == ["approach", "execution-mode", "runs", "r1", "r1"]
    assert seen[1][1] == {"execution_mode": "vision_only"}
    assert str(tmp_path) in seen[2][1]["prompt"]
    expected = {"task_id": "notepad", "success": True, "status": "completed", "steps": 2, "actions": 4, "replans": 1, "planner_requests": 2, "vision_requests": 5, "vision_tokens": 900}
    assert {key: record[key] for key in expected} == expected


def test_compare_reports_per_task_and_totals(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "RESULTS_DIR", tmp_path)
    rows = {
        "a": [{"task_id": "t1", "success": False, "vision_requests": 10}, {"task_id": "t2", "success": True, "vision_requests": 4}],
        "b": [{"task_id": "t1", "success": True, "vision_requests": 6}, {"task_id": "t2", "success": True, "vision_requests": 4}],
    }
    for label, records in rows.items():
        (tmp_path / f"{label}.jsonl").write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")

    report = runner.compare("a", "b")

    assert "fail → pass" in report
    assert "TOTAL (common tasks)" in report and "1 → 2" in report and "14 → 10" in report


def test_legacy_mode_talks_to_the_baseline_gateway(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tasks/run"):
            (tmp_path / "out.txt").write_text("42", encoding="utf-8")
            output = {"status": "completed", "steps": [{"actions": 2}], "replan_count": 0}
            return httpx.Response(200, json={"thread_id": "t", "output": output, "error": None})
        return httpx.Response(200, json={})

    task = {"id": "calc", "prompt": "compute", "check": {"file_contains": {"file": "out.txt", "text": "42"}}}
    with httpx.Client(base_url="http://gateway/api/computer-use", transport=httpx.MockTransport(handler)) as api:
        record = runner.run_task(api, task, "baseline-1", tmp_path, legacy=True)

    assert (record["status"], record["actions"], record["budget_used"], record["approvals_asked"]) == ("completed", 2, None, 0)


def test_settings_are_pinned_for_a_run_and_restored(monkeypatch):
    state = {"/vision/verifier": {"verifier": "fara"}, "/memory/enabled": {"enabled": True}}  # no threshold route: an old gateway
    posted = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/computer-use")
        if request.method == "POST":
            posted.append((path, json.loads(request.content)))
            state[path] = json.loads(request.content)
            return httpx.Response(200, json=state[path])
        return httpx.Response(200, json=state[path]) if path in state else httpx.Response(404, json={})

    with httpx.Client(base_url="http://gateway/api/computer-use", transport=httpx.MockTransport(handler)) as api:
        before = runner.read_settings(api)
        runner.apply_settings(api, {"verifier": "none", "memory": False})
        pinned = runner.read_settings(api)
        runner.apply_settings(api, before)

    assert before == {"verifier": "fara", "memory": True, "threshold": None}
    assert pinned == {"verifier": "none", "memory": False, "threshold": None}
    assert state["/vision/verifier"] == {"verifier": "fara"} and ("/approvals/threshold", {"threshold": None}) not in posted


def test_false_passes_and_budget_suggestions(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "RESULTS_DIR", tmp_path)
    rows = [
        {"task_id": f"t{i}", "status": "completed", "success": i != 0, "approvals_asked": 1, "budget_used": {"vision": 10 * (i + 1), "action": i}}
        for i in range(4)
    ]
    (tmp_path / "a.jsonl").write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")

    report = runner.compare("a", "a")
    suggestion = runner.budgets(["a"])

    assert "false passes (completed but not done): 1 → 1" in report and "approvals asked: 4 → 4" in report
    assert "vision           4     40     40               60" in suggestion
