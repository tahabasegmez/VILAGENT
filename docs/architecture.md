# VILAGENT architecture

How VILAGENT is built and how a task flows through it. Every box in the diagrams maps to a file
or folder; paths are relative to `agent/vilagent/` unless they start with `desktop/`.

## 1. Overview

VILAGENT is a Windows desktop app that operates the computer from a plain-language task.

- An **Electron shell** starts a **local Python gateway** (FastAPI), loads the operator UI (a
  static Next.js export served by that gateway), and stops everything when the window closes.
- A run is a **LangGraph** graph: recall experience, plan (or brief, or go direct), execute step by
  step, check, replan when blocked, finish.
- Steps run **natively** where they can (Windows UI Automation, Playwright) and otherwise through
  a **vision loop**: screenshot → the computer-use model (FARA) picks one action → act → repeat.
- Every model is a **LangChain connection** the operator adds in the UI. Nothing is hard-wired.
- Every action passes **one choke point** (`control.py`) that enforces the stop, the approval rules
  and the budgets.
- **Memory** learns from finished runs and hands relevant runs and lessons to the next one.

## 2. System context

```mermaid
flowchart LR
    operator(["Operator"])

    subgraph app["VILAGENT app (one Windows machine)"]
        subgraph electron["Electron shell - desktop/electron"]
            ui["Operator UI<br/>desktop/src"]
            mini["Mini window<br/>always on top"]
        end
        gateway["Local gateway<br/>FastAPI on 127.0.0.1<br/>agent/vilagent"]
        data[("Data dir<br/>state, connections.db,<br/>memory, checkpoints,<br/>run records, logs")]
    end

    subgraph outside["What it drives and calls"]
        desktop["Windows desktop<br/>via pywinauto + pyautogui"]
        browser["Edge / Chrome, own profile<br/>via Playwright"]
        models["Model endpoints: APIs, ngrok,<br/>Ollama - via LangChain"]
    end

    operator --> ui
    ui -- "HTTP + SSE, internal token" --> gateway
    mini -. "follows the same run" .- gateway
    gateway --> data
    gateway --> desktop
    gateway --> browser
    gateway --> models
```

The gateway only listens on loopback and only accepts callers that carry the per-launch token the
launcher generated. Keys, memory and run records never leave the machine; the only outbound
traffic is to the model endpoints the operator configured (and whatever the browser task visits).

## 3. Processes and startup

```mermaid
sequenceDiagram
    participant E as Electron main<br/>desktop/electron/main.cjs
    participant G as Gateway<br/>python -m vilagent
    participant R as Runtime<br/>server/runtime.py
    participant W as UI window

    E->>E: pick a free port, generate the internal token
    E->>G: spawn with port, data dir, token
    G->>G: chdir into the data dir, load .env (startup only)
    G->>R: lifespan starts
    R->>R: desktop + browser environments, Control
    R->>R: register the Ctrl+Alt+Esc stop hotkey
    R->>R: open graph checkpoints and the memory store
    R-->>R: re-embed memory rows in the background
    loop until healthy
        E->>G: GET /health
    end
    E->>W: load the UI with the gateway address and token
    Note over E,W: closing the window stops the gateway and every child process
```

In development `corepack pnpm dev` does the same with `next dev` serving the UI; when packaged,
the gateway is a PyInstaller executable inside the Electron app.

## 4. Code layers

```mermaid
flowchart TB
    server["server/<br/>HTTP routes, runtime, persisted selections, run report"]
    graph_["graph/<br/>LangGraph run graph, thin nodes"]
    agents["agents/<br/>planner, brief writer, step executor,<br/>vision loop, outcomes, supervisor"]
    vision["vision/<br/>computer-use prompts, reply parsing,<br/>screenshot encoding"]
    control["control.py<br/>stop, ActionGate, action log"]
    env["env/<br/>Environment interface:<br/>desktop/ and browser.py"]

    runs["runs/<br/>RunManager, RunSession, budgets,<br/>events, trace, records"]
    memory["memory/<br/>store, recall, learner, redaction"]
    approvals["approvals/<br/>policy, broker, gate"]
    connections["connections.py<br/>models as LangChain connections"]
    config["config/<br/>run settings"]

    server --> graph_ --> agents
    agents --> vision
    agents --> control --> env
    server --> runs
    server --> memory
    server --> connections
    graph_ --> memory
    control --> approvals
    agents --> runs
    server --> config
```

Rules that keep the layers clean:

- Agents talk to the **`Environment`** interface only (`screenshot()` + `act()`); the desktop and
  the browser are interchangeable and fully faked in tests.
- **Every action goes through `Control.perform`.** No code calls an environment's executor
  directly.
- The vision loop, the environments and `Control` **never import LangGraph**. Graph nodes stay thin
  and call plain functions.
- Graph state is **JSON only** (it is checkpointed); live objects (environments, models, callbacks)
  travel in `RunContext` (`graph/state.py`).

## 5. Life of a run

```mermaid
sequenceDiagram
    participant UI as UI
    participant API as server/api.py
    participant M as RunManager<br/>runs/manager.py
    participant G as Run graph<br/>graph/
    participant B as EventBus<br/>runs/events.py
    participant L as Learner<br/>memory/learn.py

    UI->>API: POST /runs {prompt}
    API->>M: start a RunSession (one run at a time)
    API-->>UI: 202 {run_id}
    UI->>API: GET /runs/{id}/events (SSE)
    M->>G: run_graph(approach, prompt)
    loop while the run is active
        G->>B: activity, trace, approval.requested / resolved
        B-->>UI: events, each with a sequence number
    end
    G-->>M: result
    M->>M: write the record to runs/{id}.json
    M->>B: run.finished
    B-->>UI: run.finished
    M-)L: after_run (background)
```

- A reconnecting client resumes from the last sequence number it saw; the bus keeps the last 2000
  events, which covers any reconnect within a run.
- Budgets (planner, vision and supervisor calls, actions, duration) are charged at the choke
  points through the session's context variable; running out ends the run with a clear reason.
- An **interrupted** run (the app closed mid-task) keeps its checkpoints and can be resumed from
  the step it was on; any other ending deletes them.

## 6. The run graph

The graph is built in `graph/build.py`; nodes are in `graph/nodes.py`.

```mermaid
stateDiagram-v2
    [*] --> recall
    recall --> plan: approach = plan
    recall --> brief: approach = brief
    recall --> direct: approach = direct

    plan --> select_step
    select_step --> gate: a step is left
    select_step --> finalize: no step left
    gate --> execute_step: plan approach
    gate --> execute_task: brief / direct
    gate --> finalize: operator said no
    execute_step --> select_step: completed
    execute_step --> replan: blocked and replans left
    execute_step --> finalize: failed or out of replans
    replan --> select_step

    brief --> gate
    direct --> gate
    execute_task --> finalize
    finalize --> [*]
```

| Node | Does |
|---|---|
| `recall` | Reads memory once: similar past runs and lessons (section 11). |
| `plan` / `replan` | The planner writes (or revises) steps, each with a risk level and a completion criterion. |
| `brief` | The planner writes one directive and picks desktop or browser. |
| `direct` | The operator's words become the single step, unchanged. |
| `select_step` | Picks the next pending step. |
| `gate` | Asks the operator when the step's risk reaches the threshold: `interrupt()` pauses the graph until an answer arrives. |
| `execute_step` | One plan step through the step executor (section 7). |
| `execute_task` | The whole task in one vision loop (brief and direct). |
| `finalize` | Builds the outcome and the report. |

| Approach (`agents/common.APPROACHES`) | Planner calls | Steps |
|---|---|---|
| `plan` | plan + up to 2 replans | several, checked one by one |
| `brief` | one | one, the vision model owns the whole task |
| `direct` | none | one, the raw task |

Every node is wrapped by `traced()` so the UI sees it start and finish (section 12), and the graph
checkpoints to SQLite after each node, which is what makes resume possible.

## 7. Executing a step

`StepExecutor.execute` in `agents/plan_execute.py` decides how one plan step runs.

```mermaid
flowchart TB
    start(["Plan step"]) --> where{"Environment?"}

    where -- browser --> bkind{"Plain browser action<br/>and no vision needed?"}
    bkind -- "yes: open URL, type, hotkey" --> bact["Playwright action"]
    bkind -- no --> vloop

    where -- desktop --> dvis{"Needs vision?<br/>always true in<br/>vision-only mode"}
    dvis -- yes --> vloop
    dvis -- no --> kind{"Keyboard action?"}
    kind -- "yes: type, hotkey" --> act["Act directly"]
    kind -- "no: launch, click a named control" --> uia["UI Automation lookup<br/>resolve_target"]
    uia -- "found one match" --> act
    uia -- "nothing or ambiguous" --> vloop

    vloop["Vision loop<br/>section 8"] --> verdict{"Finished by<br/>the model?"}
    verdict -- yes --> done(["completed"])
    verdict -- "no, budget spent" --> check{"Step check<br/>FARA / supervisor / off"}
    check -- passed or off --> done
    check -- failed --> blocked(["blocked, may replan"])

    act --> result(["completed / failed / denied"])
    bact --> result
```

**Execution mode** (`hybrid` or `vision_only`) is chosen in the UI: hybrid tries native actions
first, vision-only sends every step to the vision loop (`prepare_plan`).

## 8. The vision loop

`run_vision_loop` in `agents/vision_loop.py` is shared by every approach and tuned with
`LoopLimits` (max actions, extra turns for waits, nudges, model errors, history length).

```mermaid
flowchart TB
    shot["Screenshot<br/>password fields blacked out"] --> ask["Ask the computer-use model<br/>vision/fara.py via its LangChain chat model"]
    ask --> parse{"Reply usable?"}
    parse -- "no: off-schema action or coordinates" --> fix["Send a correction<br/>with an example"] --> ask
    parse -- yes --> finish{"Finish?"}
    finish -- yes --> out(["Result: completed or failed<br/>with the model's notes"])
    finish -- no --> repeat{"Same action<br/>repeated?"}
    repeat -- yes --> help["Recovery supervisor advice<br/>agents/supervisor.py<br/>or a generic nudge"] --> shot
    repeat -- no --> scale["Map coordinates<br/>back to the screen"] --> perform["Control.perform<br/>section 9"]
    perform --> limit{"Budget or<br/>limits left?"}
    limit -- yes --> shot
    limit -- no --> out
```

- A step is **completed only when the model says it finished**; running out of actions returns to
  the executor for a step check instead of counting as success.
- The model receives the step instruction, a short context hint, and up to two memory lessons for
  the step's app or site.
- Replies arrive in many spellings (`type_text`, `"640,380"`, `{x, y}`…); `vision/fara.py` maps
  them to the shared action vocabulary in `actions.py`.

## 9. Safety: one choke point

```mermaid
flowchart LR
    action(["Action"]) --> stop{"Emergency stop<br/>engaged?"}
    stop -- yes --> refuse(["refused"])
    stop -- no --> gate{"ActionGate<br/>ApprovalGate in approvals/gate.py"}
    gate -- "risky: send, pay, delete,<br/>unknown site, destructive hotkey" --> ask["Ask the operator<br/>approvals/broker.py"]
    ask -- "no, or no answer in time" --> refuse
    ask -- yes --> charge
    gate -- ok --> charge["Charge the action budget<br/>runs/budget.py"]
    charge --> exec["Environment executes it"]
    exec --> log[("Action log")]
    refuse --> log
```

There are two levels of approval:

- **Steps**: the `gate` node pauses the graph when a planned step's risk reaches the operator's
  threshold (`off`, `critical`, `high`, `medium`).
- **Actions**: the `ActionGate` inside `Control.perform` catches risky individual actions, whatever
  the plan said, using words in the model's note, the target site and the hotkey.

A "no" ends the run and is never planned around. The stop button and the global `Ctrl+Alt+Esc`
hotkey engage the emergency stop: the running task is cancelled, further actions are refused and
the managed browser is closed.

## 10. Models as connections

```mermaid
classDiagram
    class Connection {
        id
        name
        kind : llm | vlm | computer_use | embedding
        interface : LangChain class path
        params : model, base_url, temperature ...
        secrets : api_key ... DPAPI-encrypted
    }
    class Interface {
        path
        label
        kinds
        params : name, type, required, default
    }
    class Role {
        planner : llm or vlm
        supervisor : vlm, or same as planner
        vision : computer_use
        embeddings : embedding, or keywords only
    }
    Interface "1" --> "*" Connection : built from
    Role "*" --> "1" Connection : points at
```

- `connections.INTERFACES` lists the LangChain classes the operator can pick (OpenAI-compatible,
  Ollama, Google Gemini, Anthropic Claude, and embeddings for OpenAI-compatible, Ollama, Gemini
  and Hugging Face) with each class's own constructor arguments under their real names.
- `connections.build()` imports the class and passes the stored arguments (secrets decrypted,
  empty ones falling back to the interface's defaults). Nothing in the app speaks HTTP to a model
  itself; an API, an ngrok tunnel and a local Ollama differ only in `base_url`.
- Connections live in `<data dir>/connections.db`. Secrets are encrypted with Windows DPAPI, so
  they only decrypt for the same user on the same machine, and the UI only ever learns which
  secrets are set.
- `server/models.py` maps each role to its connection (`ROLE_KINDS` says which types a role
  accepts) and offers a per-role connection check.

Adding a provider is one new `Interface` entry.

## 11. Experience memory

Memory lives in `<data dir>/.vilagent/memory.sqlite`: **episodes** (remembered runs), **lessons**
(learned advice and the operator's own notes, filed under an app, a site or "general"), full-text
indexes, and optional vectors from the embeddings connection.

### Writing, after a run

```mermaid
sequenceDiagram
    participant M as RunManager
    participant L as Learner<br/>memory/learn.py
    participant P as Planner model
    participant S as Memory store<br/>memory/store.py

    M-)L: after_run(session)
    L->>L: redact typed text and personal data
    L->>S: add the episode (verified if a step check confirmed it)
    L->>L: worth notes? trouble, struggles, drift, spent budget, unknown app
    alt worth a look
        L->>P: one call: task + each step's instruction and the model's own notes (trimmed)
        P-->>L: short lessons filed by app / site / general
        L->>S: add lessons (near-duplicates merged)
    end
```

### Reading, before a run

```mermaid
flowchart LR
    prompt(["Task prompt"]) --> fts["Keyword search<br/>FTS5, bm25"]
    prompt --> vec["Vector search<br/>above MIN_SIMILARITY"]
    vec -. "vetoes keyword hits<br/>it judged unrelated" .-> fts
    fts --> fuse["Rank fusion<br/>fuse"]
    vec --> fuse
    keys["Lessons filed under the<br/>task's apps and sites"] --> pick
    fuse --> pick["Keep the best<br/>3 runs, 5 lessons"]
    pick --> planner["Planner / brief:<br/>runs + lessons"]
    pick --> step["Each step:<br/>up to 2 lessons for its app or site<br/>to the vision model and supervisor"]
```

- `memory/retrieval.recall` runs once, in the `recall` node, and records which entries each run
  was given (the Memory panel's "Used in last run").
- The relevance floor means a task with nothing similar in memory gets nothing, not the least
  unrelated examples.
- Operator notes rank first; a run rated "bad" and a switched-off lesson are never recalled.
- Nothing typed and no screenshot is ever stored (`memory/redact.py`).

## 12. Live visibility

```mermaid
flowchart LR
    nodes["Graph nodes<br/>traced() in graph/build.py"] --> span
    code["Any code<br/>runs/trace.span() and note()"] --> span
    span["Trace spans<br/>id, parent, status, output,<br/>thinking, memory"] --> bus["EventBus<br/>trace events"]
    bus --> reducer["UI reducer<br/>desktop/src/core/computer-use/trace.ts"]
    reducer --> graphview["Live run graph<br/>React Flow"]
    reducer --> minihud["Mini window<br/>while the vision model acts"]
```

Traces are live only: they are never written to the run record, and typed text is masked before a
span is published. Budget snapshots (calls and tokens per model) ride on the `activity` event and
drive the usage counters in the UI.

## 13. Data and privacy

Everything the gateway writes lives in one data dir: the repository root in development,
`%APPDATA%\VILAGENT` when packaged.

| Path | Holds | Notes |
|---|---|---|
| `.env` | How the process starts (interpreter, host, port, data dir) | No keys |
| `.vilagent_state.json` | The operator's selections, each role's connection, and the `settings` block (budgets, approval rules, browser, log level) | Authoritative for a run |
| `connections.db` | Model connections | Secrets DPAPI-encrypted |
| `.vilagent/checkpoints.sqlite` | Graph checkpoints | Kept only for interrupted runs |
| `.vilagent/memory.sqlite` | Episodes, lessons, vectors | Redacted text only |
| `.vilagent/actions.jsonl` | Action log | Typed text masked |
| `runs/` | One JSON record per run | |
| `logs/` | Gateway, agent and UI logs, eval results | |

```mermaid
flowchart LR
    typed["Typed text"] -- masked --> stored[("Stored:<br/>records, memory,<br/>action log")]
    screen["Screenshots"] -- "sent to the model only" --> models["Model endpoint"]
    screen -. "never stored" .-> stored
    keys["API keys"] -- "encrypted" --> db[("connections.db")]
    keys -. "never sent" .-> ui["UI"]
```

Stored data outlives the code: the state file, the connections database, memory and run records
all read shapes and names written by earlier versions and carry them over.

## 14. Extension points

| To add | Change |
|---|---|
| A model provider | One `Interface` entry in `connections.INTERFACES` (and its package in `agent/pyproject.toml` and `agent/vilagent-server.spec`) |
| An environment | A new `Environment` implementation in `env/` |
| A safety rule for actions | A check in `ApprovalGate` (`approvals/gate.py`), the `ActionGate` on `Control` |
| Different loop behaviour | `LoopLimits` values, not a second loop |
| New run behaviour | A thin node in `graph/nodes.py` that calls plain functions, wired in `graph/build.py` |
| Something visible in the UI's run graph | A `span()` around it and `note()` for its output |
