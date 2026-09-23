# VILAGENT agent

The Python half of VILAGENT: one package, `vilagent`, that holds both the agent and the local
gateway the desktop app talks to. The Electron shell starts it and stops it; you only run it
by hand while developing.

```bash
python -m vilagent --port 8001 --data-dir ..            # the repository root is the dev data dir
python -m vilagent --port 8001 --data-dir .. --reload   # restart on source changes
```

Other flags: `--host`, `--static-dir` (serve the built UI at `/`) and `--dev-origin` (let `next
dev` through CORS). The gateway answers `/health`, and everything else lives under
`/api/computer-use`, open only to callers that carry the launcher's internal token.

## How a task runs

1. The UI posts a task. The gateway answers `202` with a run id at once, and the run's live
   events stream over SSE (`/runs/{id}/events`): activity, the trace behind the run graph,
   approvals, and the result.
2. The run is a LangGraph graph. It first **recalls** relevant experience, then takes one of three
   approaches:
   - **plan**: the planner writes steps; each step passes an approval gate and runs; a blocked
     step triggers a replan.
   - **brief**: the planner writes one directive and the vision model carries out the whole task.
   - **direct**: the operator's own words go straight to the vision model.
3. A step runs either natively (UI Automation, or Playwright in the browser) or through the
   **vision loop**, which screenshots, asks the computer-use model for one action, acts and
   repeats. A supervisor model can help when the loop is stuck, and a step check can confirm the
   result.
4. Every action goes through `Control`, which enforces the emergency stop, the approval rules and
   the action log. Budgets (model calls, actions, time) are charged as the run goes.
5. The run's record is written to `<data dir>/runs/`, and the learner turns it into memory for
   next time. An interrupted run keeps its checkpoints and can be resumed.

## Package layout

| Module | Responsibility |
|---|---|
| `__main__.py` | Command line: data dir bootstrap, uvicorn start. |
| `actions.py` | The action vocabulary shared by the models, the agents and the environments. |
| `control.py` | The one choke point every action passes: emergency stop, `ActionGate`, action log. |
| `env/` | The `Environment` interface (`screenshot()` + `act()`) and its two implementations: `desktop/` (screen capture, mouse, keyboard, UI Automation, password redaction, the global stop hotkey) and `browser.py` (Edge or Chrome through Playwright, with the operator's own profile from `browser_profiles.py`). |
| `vision/` | Talking to the computer-use model: its prompts, reading its replies into actions (including the off-schema ones), screenshot encoding. |
| `agents/` | The planner and the brief writer, the step executor, the shared vision loop, step outcomes (classify and verify), and the recovery supervisor. |
| `approvals/` | Asking the operator: the risk policy, the per-run broker that waits for an answer, and the gate on `Control`. |
| `graph/` | The LangGraph run graph and its nodes, which stay thin and call the modules above; SQLite checkpoints. |
| `runs/` | One active run at a time (`RunManager`), its live state (`RunSession`), budgets, the event buffer behind SSE, the live trace (`span()` / `note()`), and the run records. |
| `memory/` | Experience memory: the SQLite store (remembered runs, lessons and operator notes, full-text search, vectors), recall, the learner, and redaction so nothing typed is ever stored. |
| `connections.py` | Every model the app can use. A connection is one of four types (`llm`, `vlm`, `computer_use`, `embedding`), a LangChain class and that class's own arguments; secrets are DPAPI-encrypted in `connections.db`. |
| `config/` | The run settings (budgets, approval rules, browser, log level), read from the state file. |
| `server/` | The FastAPI app and runtime, and the routes: runs and selections (`api.py`), models and connections (`models_api.py`), memory (`memory_api.py`), the browser (`browser_api.py`). It also holds the persisted UI state and the run report. |

## The data dir

Everything the gateway writes lives in one directory. In development that's the repository
root, and when packaged it's `%APPDATA%\VILAGENT`. The gateway chdirs into it at start, and all
of it is git-ignored.

| Path | Holds |
|---|---|
| `.env` | Only how the process starts (interpreter, host, port); no keys. |
| `.vilagent_state.json` | The operator's selections, the four roles' connections, and the `settings` block. |
| `connections.db` | The model connections, with their secrets encrypted. |
| `.vilagent/` | Graph checkpoints, the memory store, the action log. |
| `runs/` | One JSON record per run. |
| `logs/` | Agent and gateway logs, eval results. |

## Things worth knowing

- **Desktop calls run on one thread.** pywinauto's UI Automation objects only work on the thread
  that created them. From any other thread, lookups silently return nothing. The desktop
  environment therefore owns a single worker thread. Screen capture uses its own fresh thread,
  because `SetThreadDesktop` fails on a thread that has initialized COM.
- **Screenshots are sent at full resolution** by default, so coordinates are 1:1 with the
  screen. Downscaling (`vision_max_image_dimension`) is opt-in, because some models don't answer
  in the pixel space of the image they were sent.
- **The browser must be closed** before a browser task starts, since Chromium locks the profile
  directory while it runs.
- **Stored data outlives code.** The state file, the connections database, the memory store and
  the run records all read shapes and names from earlier versions and carry them over.

## Tests

```bash
python -m pytest tests -q        # from agent/, with this directory on PYTHONPATH
ruff check vilagent tests evals
```

No test touches the real desktop or a real model. The environments, the models and the drivers
are all faked, so the suite runs in seconds.

## Evals

`evals/` holds a fixed set of real tasks and a runner that measures whether a change makes the
agent better. The suite drives the real machine, so the operator runs it; `evals/README.md` has
the commands and the runbook.

## Frozen build

`corepack pnpm build:server` (from `desktop/`) freezes the gateway with PyInstaller using
`vilagent-server.spec`. A new runtime dependency has to be listed there too, or the installed
app won't have it.
