# VILAGENT

**VILAGENT** is a Windows desktop app that operates your computer for you. You describe a task
in plain words, and it carries the task out on your real desktop and in your real browser —
clicking, typing and navigating while you watch, and asking before anything risky.

It is a single app. The Electron window starts its own local Python gateway, serves the operator
UI from it, and shuts everything down when you close the window. Your keys, memory and run
records stay on your machine.

## How it works

A **planner** model decides what to do. A **computer-use** model (FARA, a vision model) looks at
the screen and decides where to click and what to type. Every action passes one choke point that
enforces the stop button, your approval rules and the run's budgets.

1. **Recall.** Memory looks up similar tasks that worked, the lessons learned on the same apps and
   sites, and the notes you wrote yourself. Only what is actually relevant is passed on.
2. **Plan.** The planner breaks the task into steps and rates each step's risk.
3. **Ask.** A step at or above your "ask before" level waits for your OK. So does any action that
   looks like sending, paying or deleting. No answer counts as no, and a "no" ends the task.
4. **Act.** A step runs directly where it can (launch an app, open a URL, press a named button
   through Windows UI Automation). Otherwise it goes to the vision loop: screenshot, next action,
   act, until the step is done or its budget runs out. Browser steps use Edge or Chrome with
   your own profile, so you are already signed in.
5. **Check and recover.** A step can be confirmed on screen afterwards. When the vision model
   goes in circles, an optional supervisor model says how to get unstuck. A blocked step leads
   to a revised plan.
6. **Learn.** After the run, what went wrong, or what worked, is kept as short lessons, and the
   run is remembered as an example. Anything you typed is masked, and screenshots are never
   stored.

**Three approaches**, chosen in the Options panel:

| Approach | What happens |
|---|---|
| **Plan** | The planner writes steps, and each one is checked and approved on its own. |
| **Brief** | The planner writes one directive, and the vision model runs the whole task in one go. |
| **Direct** | Your words go straight to the vision model, with no planner call. |

One task runs at a time. Every run has budgets (model calls, actions, time), and a run cut off
by closing the app can be **resumed** from the step it was on.

## The UI

The UI is a full-screen live view of the run: each step, model call and vision action appears
as a node as it happens, with its output, the model's reasoning and the memory it used. The
plan sits beside it, and model usage counts up at the top.

- **Options** (right edge): the approach, execution mode, memory, recovery supervisor, step
  check and approval level.
- **Settings**:
  - **Connections**: every model the app can use.
  - **Models**: which connection plays each role, with a Check button per role.
  - **Browser**: which browser and profile browser tasks use.
  - **Memory**: browse, rate, edit or re-embed what it remembers, and add your own notes.
  - **Logs**.

## Models

Models can be connected as a **connection** you add in Settings →
Connections:

- a nickname and a type: **LLM** (text), **VLM** (text and images), **computer use** or
  **embedding**
- a provider (a LangChain interface): OpenAI-compatible, Ollama, Google Gemini, Anthropic Claude,
  or Hugging Face for embeddings
- that provider's own settings under their usual names (`model`, `base_url`, `api_key`,
  `temperature`, `max_tokens`, `timeout`, `max_retries`, …), with sensible defaults


A run needs:
- an **LLM** (or VLM) for the planner
- a **computer-use** connection for FARA, on any OpenAI-compatible server (vLLM on Colab behind
  ngrok, LM Studio, a local or remote vLLM)

Optional:
- a **VLM** for the supervisor and the step check
- an **embedding** model so memory can search by meaning, not only by keyword

## Requirements

- Windows 11
- Python 3.12+ with the agent's dependencies, and Node 22+ with pnpm (through corepack)
- Edge or Chrome for browser tasks. A specific profile can be used by agent, it will be locked while it runs so ensure this profile window is closed.

## Run it from source

```bash
cp .env.example .env            # set VILAGENT_PYTHON if the agent's Python isn't on PATH
cd desktop && corepack pnpm install
corepack pnpm dev               # the gateway, the UI dev server and the desktop window
```

Then add your connections in Settings → Connections and assign them in Settings → Models.

## Build the app

```bash
cd desktop && corepack pnpm dist
```

This builds the UI, freezes the gateway with PyInstaller and writes an installer to
`desktop/dist-electron/`. The installed app keeps its data (settings, connections, memory, run
records, logs) in `%APPDATA%\VILAGENT`.

## Stopping a run

The stop button in the UI, or the global **Ctrl+Alt+Esc** hotkey, cancels the running task at
once, blocks any further action and closes the managed browser.

## Repository

| Folder | What's there |
|---|---|
| `docs/` | How it works: [docs/architecture.md](docs/architecture.md) |
| `agent/` | The agent and the local gateway, in Python. See [agent/README.md](agent/README.md). |
| `agent/evals/` | The live eval suite that measures whether a change helps. See [agent/evals/README.md](agent/evals/README.md). |
| `desktop/` | The operator UI (a static Next.js export) and the Electron shell. See [desktop/README.md](desktop/README.md). |
| `tools/` | A PowerShell memory inspector. |
