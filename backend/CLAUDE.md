# CLAUDE.md

This file guides Claude Code (claude.ai/code) when working in the VILAGENT backend.

## Project Overview

**VILAGENT** is a Windows-first **computer-use agent operator**. The backend is a
local FastAPI **Gateway** that exposes a typed, internal-authenticated computer-use
control plane (desktop sessions, observations, UIA discovery, target resolution,
approvals, safe action execution, audit, emergency stop) plus a minimal model-list
API. An Electron/Next operator UI talks to this Gateway through trusted server-side
proxy routes.

This codebase was derived from the deer-flow deep-research agent and then purged of
everything not related to computer use (no chat/research lead agent, sandbox,
subagents, memory, skills, MCP, community tools, or IM channels). Keep it that way:
**do not reintroduce non-computer-use surfaces.**

**Architecture**:
- **Gateway API** (port 8001): FastAPI app; mounts only the `computer_use` and
  `models` routers plus `/health`. No LangGraph research runtime is wired in.
- **Operator UI** (Next/Electron, port 3000): consumes the Gateway via
  `/api/computer-use/*` server-side proxy routes.

**Runtime model**: the Gateway runs an in-process or dedicated-process Windows host
(`computer_use.runtime_mode`, default `dedicated_process`). All Gateway computer-use
routes talk to that host through `RemoteWindowsHostControl`.

## Project Structure

```
backend/
├── langgraph.json              # single graph: computer_use_agent -> vilagent.computer_use.agent:make_computer_use_agent
├── pyproject.toml              # app deps
├── packages/harness/           # editable-installed package `vilagent-harness` (import: vilagent.*)
│   └── vilagent/
│       ├── computer_use/       # THE product surface (see below)
│       ├── config/             # AppConfig + sub-config schemas (computer_use, models, ...)
│       ├── models/             # create_chat_model factory (planner / text LLM)
│       ├── agents/middlewares/ # the 5 middlewares the cu agent reuses
│       ├── runtime/            # checkpointer + run/event plumbing (checkpointer is used by langgraph.json)
│       ├── persistence/, reflection/, tracing/, utils/
│       └── tools/builtins/     # ask_clarification_tool only
└── app/
    └── gateway/
        ├── app.py                      # FastAPI app (computer_use + models routers + /health)
        ├── computer_use_runtime.py     # startup lifespan: builds the Windows host / supervisor
        ├── computer_use_deps.py        # lazy deps so importing the router is cheap
        ├── routers/computer_use.py     # the internal control-plane router
        ├── routers/models.py
        └── auth/                       # local auth + internal auth + CSRF
```

**Import conventions**: app imports vilagent; vilagent never imports app
(`tests/test_harness_boundary.py` enforces this).

## computer_use package (`packages/harness/vilagent/computer_use/`)

Provider-neutral typed contracts, policy/HITL decisions, `DesktopLease`, the
observe→act→verify `ComputerUseEngine`, bounded observation storage, and Windows
session lifecycle.

Key modules:
- `agent.py` — `make_computer_use_agent`: a compact LangGraph computer-use
  coordinator (small-model friendly). It binds only computer-use tools, never
  research/sandbox/subagent surfaces.
- `prompt.py` — small, static, ASCII system prompt (UIA → browser DOM → vision →
  coordinate priority; HITL before risky actions). Keep it tiny and token-cheap.
- `plan_execute.py` — the plan-and-execute orchestrator (see below).
- `engine.py` — `ComputerUseEngine.execute()`: observe → lease → freshness/expected-
  window validation → preconditions → native action → postcondition verify. **Approval
  / policy gating was intentionally moved OUT of the engine to the action-lifecycle
  service** (`submit_action`/`execute_action`). The engine is the low-level executor.
- `fara.py` — `FaraVisionActionProvider`: queries a FARA/Qwen2-VL-style vision model
  and maps its `<tool_call>` JSON into typed `ActionCommand`s.
- `remote_host.py` — `RemoteWindowsHostControl`: the typed facade every Gateway route
  and the orchestrator use to reach the host (sessions, observe, resolve, submit,
  execute, approvals, events, audit).
- `windows/` — read-only Pillow screen capture, pywinauto UIA discovery, semantic UIA
  action provider (`action.py`), physical input (`input.py`, coordinate click only),
  desktop-safety, hotkey listener, dedicated child process.
- `tools/` — the cu agent's own tools (`observe`, `find_element`, `verify_condition`,
  `perform_native_action`, `perform_browser_action`).

### Hotkey handling

`windows/action.py::_pywinauto_hotkey` normalizes the many shapes a model emits
(FARA `keys` arrays, UI-TARS `"ctrl c"` space combos, `"ctrl+c"`, win/super combos,
literal `+`) into pywinauto key strings. Unknown alphanumeric names fall back to
`{VK_NAME}` instead of raising, so a single odd key no longer aborts the whole step.
Covered by `tests/test_computer_use_hotkey_parsing.py`.

### Plan-and-execute (`plan_execute.py`)

`PlanExecuteComputerUseOrchestrator` runs: plan → (HITL plan approval) → execute each
step → replan on block. `execution_mode`:
- `vision_only` — every step is forced `requires_vision=True` and executed by the
  vision model loop (`_execute_fara_vision_loop`).
- `hybrid` (default) — the planner sets `requires_vision` per step; vision steps go to
  the vision loop, deterministic steps go through UIA/browser target resolution + the
  action lifecycle.

The vision loop is bounded by `max_actions` per step (hard cap 4) and a `finish_step`
terminate signal, so it cannot loop forever. `_detect_served_model_name` resolves the
served model id from the configured base_url (ngrok tunnel friendly).

Known gap to address: `vision_provider="ui_tars"` currently reuses
`FaraVisionActionProvider`, which only parses FARA `<tool_call>` JSON; UI-TARS emits a
different `Action: ...(...)` action space and needs its own parser.

## Gateway control plane (`app/gateway/routers/computer_use.py`)

Internal-authenticated routes under `/api/computer-use/*`: sessions, observation
metadata, UIA discovery (`/uia/*`), target resolution
(`/sessions/{id}/targets/resolve` and body-based operator endpoints), typed action
submission/execute/cancel, approval management, owner-filtered lifecycle events
(`/events`, `/events/wait` long-poll, `/events/stream` SSE), audit, browser session
lifecycle (`/browser/*`), and emergency stop. Keep these internal-only and fail-closed
(`404` hides wrong-owner records, `403` policy denials, `503` unavailable/persistence
failures). Never expose raw `get_blob`; external consumers use the redaction-applied
export boundary. Binary screenshots/UI trees stay outside LangGraph checkpoints.

`config.computer_use` changes are restart-required. See
`docs/VILAGENT_ARCHITECTURE.md` for the host/IPC/lifecycle boundaries and roadmap.

## Configuration

`config.yaml` (project root) is the single source of truth and must stay pure
VILAGENT: only `computer_use` and `models`. Example:

```yaml
config_version: 1
computer_use:
  enabled: true
  host_safety:
    physical_input_enabled: true
models:
  - name: vilagent-text-glm
    use: langchain_openai:ChatOpenAI
    model: $VILAGENT_GLM_MODEL_NAME
    api_key: $VILAGENT_GLM_API_KEY
    base_url: $VILAGENT_GLM_BASE_URL
    supports_vision: false
```

- `$VAR` values resolve from the environment. Env var prefix is `VILAGENT_*`
  (the old `DEER_FLOW_*` names are gone).
- The runtime data directory is `.vilagent/` (audit, lifecycle, observations,
  checkpointer); it is gitignored.
- `AppConfig` still imports a few sub-config schema modules
  (`sandbox_config`, `skills_config`, `memory_config`, `acp_config`,
  `subagents_config`). They are harmless Pydantic schemas kept only so `app_config`
  imports cleanly; `config.yaml` does not use those sections. Do not depend on them.

## Commands & environment

- **Python**: use the conda env `D:\code\envs\win\vilagent\python.exe`
  (Python 3.14). The harness is editable-installed there via a `.pth` that puts
  `packages/harness` on `sys.path`, so `import vilagent` resolves the source tree.
  If you change packaging, refresh with `pip install -e backend/packages/harness`.
- **Run the Gateway**: `python -m uvicorn app.gateway.app:app --port 8001`
  (set `PYTHONPATH=backend`).
- **Tests** (PowerShell, this sandbox):
  ```
  $env:PYTHONPATH="backend"; $env:TMP="backend\tmp_pytest"; $env:TEMP="backend\tmp_pytest"
  python -m pytest tests/test_computer_use_models.py -p no:cacheprovider -o addopts="" --basetemp=backend\tmp_pytest\bt
  ```
  Run small curated batches of `test_computer_use_*.py`. **Do not** run the whole
  suite or `test_windows_*` / cu `ipc`/`process_supervisor`/`remote_host`/`orchestration`
  tests broadly — they exercise live Windows hardware (UIA/SendInput/hotkey/IPC/spawn)
  and can hang.

## Development guidelines

- **TDD**: every fix/feature ships with a `tests/test_*.py`. Prefer pure-logic unit
  tests with no live hardware (see the hotkey parsing test).
- Keep the harness → app firewall intact (`test_harness_boundary.py`).
- Keep `computer_use/prompt.py` and the FARA prompt short, static, ASCII, and
  token-cheap; they target small vision/text models.
- Performance: screenshot capture + blob export over the Gateway to a remote vision
  model is the hot path. Minimize round trips, reuse observations within a step, and
  avoid re-encoding blobs unnecessarily.
- ruff for lint/format; line length 240; Python 3.12+ type hints; double quotes.
