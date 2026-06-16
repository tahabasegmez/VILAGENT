# VILAGENT

**VILAGENT** is a Windows-first **computer-use agent operator**. A planner reasons
about a task, then a vision and/or UI-Automation executor drives the real desktop and
browser to carry it out — under human-in-the-loop approval, with a fail-closed safety
model and a full audit trail.

> VILAGENT was rebuilt from the deer-flow deep-research agent and stripped down to a
> pure computer-use surface. There is no general chat, deep research, sandbox,
> subagents, memory, skills, MCP, or messaging-channel functionality — only the
> computer-use operator.

## Components

| Component | What it is |
|-----------|------------|
| **Gateway** (`backend/`, port 8001) | Local FastAPI control plane: desktop sessions, observations, UIA discovery, target resolution, approvals, safe action execution, audit, emergency stop. |
| **Operator UI** (`frontend/`, port 3000) | Next.js operator console (Electron-ready) that drives the Gateway through `/api/computer-use/*` proxy routes. |
| **Windows host** | In-process or dedicated child process owning screen capture, UIA, semantic actions, physical-input gating, hotkey listener, and the emergency-stop latch. |

## How it works

1. **Plan** — a text planner turns the task into compact, isolated steps. In hybrid
   mode it also marks each step as deterministic (UIA / browser DOM) or vision.
2. **Approve** — the proposed plan and every risky action require operator approval
   (HITL). Approval/policy gating lives in the action-lifecycle service, not the
   low-level engine.
3. **Execute** — each step runs through observe → lease → freshness/expected-window
   checks → preconditions → native action → postcondition verification. Vision steps
   run a bounded FARA/UI-TARS loop; deterministic steps resolve a stable UIA/DOM target.
4. **Replan** — a short replan is requested only when a step is blocked.

Two execution modes:
- **vision_only** — every step is executed by the vision model.
- **hybrid** (default) — per-step routing between deterministic UIA/DOM and vision,
  minimizing model round-trips without losing capability.

## Quick start

Requires the conda env at `D:\code\envs\win\vilagent` (Python 3.14), Node 22+, pnpm.

```bash
# Backend gateway
cd backend
PYTHONPATH=. python -m uvicorn app.gateway.app:app --port 8001

# Frontend operator
cd frontend
pnpm install
pnpm dev        # http://localhost:3000  ->  /operator
```

Configure models and the computer-use host in `config.yaml` (project root). Only the
`computer_use` and `models` sections are used; secrets resolve from `VILAGENT_*`
environment variables. See `backend/CLAUDE.md` and `backend/docs/VILAGENT_ARCHITECTURE.md`.

## Safety

- Human-in-the-loop approval before risky and coordinate-level actions.
- Fail-closed desktop safety: actions are blocked if the input desktop, expected
  foreground window, or preconditions change before mutation.
- Global emergency-stop hotkey; sanitized JSONL audit of every action.
- Physical input is disabled by default and limited to coordinate clicks when enabled.
