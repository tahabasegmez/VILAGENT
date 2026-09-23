"""Run the eval suite against a live gateway and record the metrics (see evals/README.md).

The suite drives the real machine, so the operator runs it. In two PowerShell terminals in
``agent/``, both with the same token (``$env:VILAGENT_INTERNAL_AUTH_TOKEN = "eval-token"``)::

    python -m vilagent --port 8001 --data-dir ..        # terminal 1: the gateway
    python -m evals.runner run --label baseline-1       # terminal 2: the suite
    python -m evals.runner compare baseline-1 baseline-2
    python -m evals.runner budgets latest-1 latest-2    # budget use (p95) and suggested limits

``run`` can pin settings for a labelled run and restores the operator's afterwards:
``--verifier fara|supervisor|none``, ``--memory on|off``, ``--threshold off|critical|high|medium``;
``--tag verifier`` runs only the tasks tagged for that measurement. ``--legacy`` talks to a
gateway from before Phase 5 (commit ``01b367a``, the baseline) through ``POST /tasks/run``.

Before each task the runner waits for Enter so the desktop can be reset (``--no-pause`` skips
that). Results are appended to ``logs/evals/<label>.jsonl`` (one line per task).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import yaml

from vilagent.agents.common import approach_name

SUITE_PATH = Path(__file__).with_name("suite.yaml")
RESULTS_DIR = Path(__file__).resolve().parents[2] / "logs" / "evals"
EVAL_DIR = Path(os.path.expanduser("~")) / "Documents" / "vilagent-evals"
AUTH_HEADER = "X-VILAGENT-Internal-Token"
ACTIVE_STATUSES = {"running", "awaiting_approval"}
POLL_SECONDS = 2.0
METRICS = ("duration_s", "actions", "replans", "planner_requests", "vision_requests", "vision_tokens")
# The operator settings a run can pin: (endpoint, JSON field, how --flag values map onto it).
SETTINGS = {
    "verifier": ("/vision/verifier", "verifier", lambda value: value),
    "memory": ("/memory/enabled", "enabled", lambda value: value == "on"),
    "threshold": ("/approvals/threshold", "threshold", lambda value: value),
}
BUDGET_HEADROOM = 1.5


def load_tasks(only: list[str] | None = None, tag: str | None = None) -> list[dict[str, Any]]:
    tasks = yaml.safe_load(SUITE_PATH.read_text(encoding="utf-8"))["tasks"]
    return [task for task in tasks if (not only or task["id"] in only) and (not tag or tag in task.get("tags", []))]


def check_passed(check: dict[str, Any], eval_dir: Path, ask=input) -> bool:
    if spec := check.get("file_contains"):
        path = eval_dir / spec["file"]
        return path.exists() and spec["text"] in path.read_text(encoding="utf-8", errors="ignore")
    return ask("  Did it succeed? [y/N] ").strip().lower() in {"y", "yes"}


def wait_for_run(api: httpx.Client, run_id: str) -> dict[str, Any]:
    """Poll the run until it is no longer active; returns its record."""
    while True:
        record = api.get(f"/runs/{run_id}").json()
        if record.get("status") not in ACTIVE_STATUSES:
            return record
        time.sleep(POLL_SECONDS)


def run_task(api: httpx.Client, task: dict[str, Any], label: str, eval_dir: Path, *, legacy: bool = False) -> dict[str, Any]:
    check = task.get("check") or {"manual": True}
    if spec := check.get("file_contains"):
        # Only files inside the eval folder are ever removed.
        (eval_dir / spec["file"]).unlink(missing_ok=True)
    api.post("/approach", json={"approach": approach_name(task.get("approach"))}).raise_for_status()
    api.post("/execution-mode", json={"execution_mode": task.get("execution_mode", "hybrid")}).raise_for_status()

    prompt = task["prompt"].replace("{eval_dir}", str(eval_dir))
    thread = f"eval-{label}-{task['id']}"
    started = time.monotonic()
    if legacy:  # the pre-Phase-5 gateway answers when the run is over
        response = api.post("/tasks/run", json={"thread_id": thread, "prompt": prompt})
        response.raise_for_status()
        body = response.json()
        record = {"status": (body.get("output") or {}).get("status"), "error": body.get("error"), "output": body.get("output")}
    else:
        response = api.post("/runs", json={"thread_id": thread, "prompt": prompt})
        response.raise_for_status()
        record = wait_for_run(api, response.json()["run_id"])
    duration = time.monotonic() - started
    output = record.get("output") or {}
    usage = output.get("usage") or {}
    steps = output.get("steps") or []
    approvals = record.get("approvals") or {}
    return {
        "label": label,
        "task_id": task["id"],
        "at": datetime.now(UTC).isoformat(),
        "status": record.get("status"),
        "error": record.get("error"),
        "success": check_passed(check, eval_dir),
        "duration_s": round(duration, 1),
        "steps": len(steps),
        "actions": sum(step.get("actions", 0) for step in steps),
        "replans": output.get("replan_count", 0),
        "planner_requests": usage.get("planner_requests", 0),
        "vision_requests": usage.get("vision_requests", 0),
        "vision_tokens": usage.get("vision_tokens", 0),
        "budget_used": (record.get("budget") or {}).get("used"),
        "approvals_asked": approvals.get("asked", 0),
        "approvals_declined": approvals.get("declined", 0),
    }


def read_settings(api: httpx.Client) -> dict[str, Any]:
    """The operator's current settings (None for any a gateway doesn't have, e.g. the baseline)."""
    settings = {}
    for name, (path, field, _) in SETTINGS.items():
        response = api.get(path)
        settings[name] = response.json().get(field) if response.status_code == 200 else None
    return settings


def apply_settings(api: httpx.Client, values: dict[str, Any]) -> None:
    for name, value in values.items():
        if value is not None:
            path, field, _ = SETTINGS[name]
            api.post(path, json={field: value}).raise_for_status()


def run_suite(label: str, gateway: str, only: list[str] | None, *, pause: bool = True, tag: str | None = None, legacy: bool = False, pinned: dict[str, str] | None = None) -> Path:
    token = os.environ.get("VILAGENT_INTERNAL_AUTH_TOKEN")
    if not token:
        raise SystemExit("Set VILAGENT_INTERNAL_AUTH_TOKEN to the token the gateway was started with.")
    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RESULTS_DIR / f"{label}.jsonl"
    with httpx.Client(base_url=f"{gateway.rstrip('/')}/api/computer-use", headers={AUTH_HEADER: token}, timeout=None) as api:
        # The suite switches the operator's selections; put them back afterwards.
        saved = {"approach": api.get("/approach").json()["approach"], "mode": api.get("/execution-mode").json()["execution_mode"], **read_settings(api)}
        try:
            apply_settings(api, {name: SETTINGS[name][2](value) for name, value in (pinned or {}).items() if value is not None})
            settings = read_settings(api)  # recorded with every task, so labelled runs can be compared
            for task in load_tasks(only, tag):
                if pause:
                    input(f"- {task['id']}: close the windows the last task left open, then press Enter… ")
                print(f"- {task['id']}: running…", flush=True)
                record = run_task(api, task, label, EVAL_DIR, legacy=legacy) | settings
                with out_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record) + "\n")
                print(f"  {'PASS' if record['success'] else 'FAIL'} · {record['status']} · {record['duration_s']}s · {record['vision_requests']} vision calls")
        finally:
            api.post("/approach", json={"approach": saved["approach"]})
            api.post("/execution-mode", json={"execution_mode": saved["mode"]})
            apply_settings(api, {name: saved[name] for name in SETTINGS})
    return out_path


def load_results(label: str) -> dict[str, dict[str, Any]]:
    """The latest record per task id for a label."""
    records: dict[str, dict[str, Any]] = {}
    for line in (RESULTS_DIR / f"{label}.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            record = json.loads(line)
            records[record["task_id"]] = record
    return records


def compare(label_a: str, label_b: str) -> str:
    a, b = load_results(label_a), load_results(label_b)
    rows = [f"{'task':<28} {'success':>11}  " + "  ".join(f"{m:>18}" for m in METRICS)]
    for task_id in sorted(a.keys() | b.keys()):
        ra, rb = a.get(task_id, {}), b.get(task_id, {})
        success = f"{_mark(ra.get('success'))} → {_mark(rb.get('success'))}"
        rows.append(f"{task_id:<28} {success:>11}  " + "  ".join(f"{_pair(ra.get(m), rb.get(m)):>18}" for m in METRICS))
    common = a.keys() & b.keys()
    totals = [f"{'TOTAL (common tasks)':<28} {_pair(_count(a, common), _count(b, common)):>11}  "]
    totals.append("  ".join(f"{_pair(_sum(a, common, m), _sum(b, common, m)):>18}" for m in METRICS))
    # A false pass: the agent reported "completed" but the task was not done (verifier quality).
    false_passes = _pair(_false_passes(a, common), _false_passes(b, common))
    approvals = _pair(_sum(a, common, "approvals_asked"), _sum(b, common, "approvals_asked"))
    return "\n".join([*rows, "".join(totals), f"false passes (completed but not done): {false_passes} · approvals asked: {approvals}"])


def budgets(labels: list[str]) -> str:
    """95th-percentile budget use per kind over these runs, and a limit with 1.5× headroom."""
    used: dict[str, list[int]] = {}
    for label in labels:
        for record in load_results(label).values():
            for kind, count in (record.get("budget_used") or {}).items():
                used.setdefault(kind, []).append(int(count))
    if not used:
        return "No budget data (runs from before Phase 3 or with --legacy don't record it)."
    lines = [f"{'budget':<12} {'runs':>5} {'p95':>6} {'max':>6} {'suggested limit':>16}"]
    for kind, values in sorted(used.items()):
        p95 = _percentile(values, 95)
        lines.append(f"{kind:<12} {len(values):>5} {p95:>6} {max(values):>6} {math.ceil(p95 * BUDGET_HEADROOM):>16}")
    return "\n".join(lines)


def _percentile(values: list[int], percent: int) -> int:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.ceil(percent / 100 * len(ordered)) - 1)]


def _mark(success: bool | None) -> str:
    return "-" if success is None else ("pass" if success else "fail")


def _pair(x: Any, y: Any) -> str:
    return f"{'-' if x is None else x} → {'-' if y is None else y}"


def _count(records: dict[str, dict[str, Any]], ids: set[str]) -> int:
    return sum(1 for task_id in ids if records[task_id].get("success"))


def _false_passes(records: dict[str, dict[str, Any]], ids: set[str]) -> int:
    return sum(1 for task_id in ids if records[task_id].get("status") == "completed" and not records[task_id].get("success"))


def _sum(records: dict[str, dict[str, Any]], ids: set[str], metric: str) -> float:
    return round(sum(records[task_id].get(metric) or 0 for task_id in ids), 1)


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m evals.runner", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="run the suite against a live gateway")
    run.add_argument("--label", required=True)
    run.add_argument("--gateway", default="http://127.0.0.1:8001")
    run.add_argument("--only", nargs="*", help="task ids to run (default: all)")
    run.add_argument("--tag", help="run only the tasks with this tag (e.g. verifier)")
    run.add_argument("--no-pause", dest="pause", action="store_false", help="don't wait for Enter before each task")
    run.add_argument("--legacy", action="store_true", help="a gateway from before Phase 5 (the baseline commit 01b367a)")
    run.add_argument("--verifier", choices=["fara", "supervisor", "none"])
    run.add_argument("--memory", choices=["on", "off"])
    run.add_argument("--threshold", choices=["off", "critical", "high", "medium"])
    diff = commands.add_parser("compare", help="compare two labelled results")
    diff.add_argument("label_a")
    diff.add_argument("label_b")
    spend = commands.add_parser("budgets", help="budget use (p95) over labelled results, with suggested limits")
    spend.add_argument("labels", nargs="+")
    args = parser.parse_args()
    if args.command == "run":
        pinned = {"verifier": args.verifier, "memory": args.memory, "threshold": args.threshold}
        print(f"Results: {run_suite(args.label, args.gateway, args.only, pause=args.pause, tag=args.tag, legacy=args.legacy, pinned=pinned)}")
    elif args.command == "compare":
        print(compare(args.label_a, args.label_b))
    else:
        print(budgets(args.labels))


if __name__ == "__main__":
    main()
