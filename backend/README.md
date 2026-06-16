# VILAGENT Backend

The VILAGENT backend is a local FastAPI **Gateway** for a Windows-first
**computer-use agent operator**. It exposes a typed, internal-authenticated
computer-use control plane (desktop sessions, observations, UIA discovery, target
resolution, approvals, safe action execution, audit, emergency stop) and a minimal
model-list API. The Electron/Next operator UI talks to it through server-side
`/api/computer-use/*` proxy routes.

This backend was rebuilt from deer-flow and purged of all non-computer-use surfaces
(no research lead agent, sandbox, subagents, memory, skills, MCP, community tools, or
IM channels).

## Run

Use the conda env `D:\code\envs\win\vilagent` (Python 3.14); the `vilagent-harness`
package under `packages/harness/` is editable-installed there.

```bash
PYTHONPATH=. python -m uvicorn app.gateway.app:app --port 8001   # Gateway on :8001
```

Health check: `GET /health`. Control plane: `GET/POST /api/computer-use/*`.

## Layout

- `app/gateway/` — FastAPI app, the `computer_use` + `models` routers, auth, and the
  computer-use runtime lifespan (`computer_use_runtime.py`).
- `packages/harness/vilagent/computer_use/` — the product surface: typed contracts,
  `ComputerUseEngine`, plan-and-execute orchestrator, FARA vision provider, the
  Windows host (`windows/`), and the agent's tools.
- `packages/harness/vilagent/{config,models,runtime,agents/middlewares}` — supporting
  infrastructure (config, the LLM factory, checkpointer, reused middlewares).

## Configuration

`config.yaml` (project root) holds only `computer_use` and `models`. Secrets resolve
from `VILAGENT_*` env vars. Runtime data lives in the gitignored `.vilagent/` dir.

## Development

See [CLAUDE.md](CLAUDE.md) for architecture, the test-running gotchas (use small
curated `test_computer_use_*.py` batches; avoid live `test_windows_*`), and the
harness→app import firewall. Every fix ships with a unit test.
