export type ComputerUseExecutionMode = "hybrid" | "vision_only";
/** plan: the planner writes steps. brief: it writes one brief. direct: FARA gets the raw task. */
export type AgentApproach = "plan" | "brief" | "direct";
/** Who checks a step that ran out of actions without FARA saying it finished. */
export type Verifier = "fara" | "supervisor" | "none";
/** Ask before steps at or above this planned risk ("off" never asks). */
export type ApprovalThreshold = "off" | "critical" | "high" | "medium";
export type ApprovalScope = "once" | "step";
export type MemoryKeyType = "app" | "domain" | "general";
export type MemoryRating = "good" | "bad" | null;

export type ComputerUseStatus = {
  enabled: boolean;
  approach: AgentApproach;
  execution_mode: ComputerUseExecutionMode;
  platform: string;
  budgets: Record<string, number>;
};

/** A models: (or embeddings:) entry of config.yaml, as the UI shows it. */
export type ModelInfo = {
  name: string;
  label: string;
  model?: string | null;
  /** The host it runs on, or the provider package for cloud SDKs. */
  where: string;
  sees_images: boolean;
};

/** A role of the agent: who plans, who supervises, who acts on screen, who searches memory. */
export type ModelRole = "planner" | "supervisor" | "vision" | "embeddings";

/** What a role points at: a connection id, "planner" (the supervisor), or "" for nothing. */
export type RoleChoice = { connection: string };

/** One argument of a LangChain class, under the name LangChain itself uses. */
export type InterfaceParam = {
  name: string;
  /** text | secret | number | integer | boolean. */
  type: string;
  required: boolean;
  default: string;
  hint: string;
};

/** A LangChain class the operator can connect through. */
export type ModelInterface = { path: string; label: string; kinds: string[]; params: InterfaceParam[] };

/** One model endpoint the operator added. Secrets are never sent back, only their names. */
export type ApiConnection = {
  id: string;
  name: string;
  /** llm | vlm | computer_use | embedding. */
  kind: string;
  interface: string;
  /** The interface's arguments, by their LangChain names. */
  params: Record<string, string>;
  /** Which secret arguments have a value stored. */
  secrets_set: string[];
};

/** Everything the models and connections screens show. */
export type ModelRoles = {
  roles: Record<ModelRole, RoleChoice>;
  /** Which types of connection each role accepts. */
  role_kinds: Record<ModelRole, string[]>;
  kinds: { id: string; label: string }[];
  connections: ApiConnection[];
  interfaces: ModelInterface[];
  /** Where each role currently runs, resolved (nickname, model, host). */
  where: Record<ModelRole, ModelInfo>;
};

export type ConnectionCheck = {
  role: "planner" | "supervisor" | "fara" | "embeddings" | (string & {});
  name: string;
  model?: string | null;
  where: string;
  ok: boolean;
  latency_ms?: number | null;
  detail: string;
};

/** A Chromium browser installed on this machine, and the profiles inside it. */
export type InstalledBrowser = { channel: string; label: string; user_data_dir: string };
export type BrowserProfile = { directory: string; name: string };

/** Which browser and profile a browser task opens. */
export type BrowserSelection = {
  use_profile: boolean;
  channel: string;
  user_data_dir: string;
  profile_directory: string;
  browsers: InstalledBrowser[];
  profiles: BrowserProfile[];
};

export type ExecutionModeSelection = {
  execution_mode: ComputerUseExecutionMode;
  options: ComputerUseExecutionMode[];
};

export type AgentApproachSelection = {
  approach: AgentApproach;
  options: AgentApproach[];
};

export type VisionRecoverySelection = {
  enabled: boolean;
};

export type ApprovalThresholdSelection = {
  threshold: ApprovalThreshold;
  options: ApprovalThreshold[];
};

/** A decision the run is waiting for (the `approval.requested` event). */
export type PendingApproval = {
  id: string;
  kind: "step" | "action";
  title: string;
  reasons: string[];
  level?: string | null;
  step_id?: string | null;
  expires_at: string;
};

/** A remembered run: a task that completed with every step verified (typed text masked). */
export type MemoryEpisode = {
  id: string;
  run_id: string;
  created_at: string;
  task_text: string;
  approach: string;
  apps: string[];
  domains: string[];
  plan_outline: { instruction: string; kind?: string | null; environment?: string | null }[];
  rating: MemoryRating;
  uses: number;
  /** 0 when a step of that run finished without a check; recall prefers the checked ones. */
  verified: number;
  /** Whether it has a vector, so memory can find it by meaning and not only by keyword. */
  embedded: boolean;
};

/** A lesson learned from trouble (source "auto") or an operator note (source "operator"). */
export type MemoryLesson = {
  id: string;
  key_type: MemoryKeyType;
  key: string;
  text: string;
  source: "auto" | "operator";
  hits: number;
  disabled: number;
  uses: number;
  updated_at: string;
  /** Whether it has a vector, so memory can find it by meaning and not only by keyword. */
  embedded: boolean;
};

export type MemoryEntries = { episodes: MemoryEpisode[]; lessons: MemoryLesson[] };

export type VerifierSelection = {
  verifier: Verifier;
  options: Verifier[];
  supervisor_sees_images: boolean;
};

export type AgentActivityItem = {
  agent_id: string;
  role: string;
  status: "idle" | "pending" | "running" | (string & {});
  task?: string | null;
  model_name?: string | null;
  request_count: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  tool_calls: string[];
  last_event?: string | null;
  current_thought?: string | null;
};

export type PlanStepActivityItem = {
  step_id: string;
  instruction: string;
  completion_criteria: string;
  max_actions?: number;
  status: "pending" | "running" | "completed" | "blocked" | "failed" | "skipped" | (string & {});
  requires_vision: boolean;
  error_code?: string | null;
  summary?: string | null;
};

export type AgentActivity = {
  thread_id: string;
  run_id?: string | null;
  agents: AgentActivityItem[];
  plan_steps: PlanStepActivityItem[];
  total_request_count: number;
  total_tokens: number;
};

export type TaskRunRequest = {
  thread_id: string;
  run_id?: string;
  prompt: string;
};

export type RunStarted = {
  run_id: string;
  thread_id: string;
};

export type RunStatus = "running" | "awaiting_approval" | "completed" | "failed" | "blocked" | "denied" | "stopped" | "cancelled" | "interrupted";

export type RunSummary = {
  run_id: string;
  thread_id: string;
  prompt: string;
  status: RunStatus;
  started_at: string;
  ended_at?: string | null;
  error?: string | null;
};

/** How a run ended: the `run.finished` event, or a run record. */
export type RunFinished = {
  status: RunStatus;
  error?: string | null;
  output?: Record<string, unknown> | null;
  activity: AgentActivity;
};

export type RunRecord = RunSummary & RunFinished;

/** One Server-Sent Event of a run: `activity` ({ activity, budget }) or `run.finished` (RunFinished). */
export type RunEvent = {
  id: number;
  type: string;
  data: unknown;
};

export type EmergencyStopStatus = {
  engaged: boolean;
  reason?: string | null;
};

export type LogSource = "agent" | "gateway" | "ui";

export type TraceStatus = "running" | "done" | "error" | "waiting";

/** One span of the live run graph (the `trace` event): a graph node, a model call or an action. */
export type TraceSpan = {
  id: string;
  parent: string | null;
  name: string;
  kind: "node" | "model" | "action";
  label?: string | null;
  status: TraceStatus;
  started_at: string;
  ended_at?: string | null;
  output?: string | null;
  thinking?: string | null;
  memory: string[];
  meta: Record<string, unknown>;
};

/** The run's budget as the `activity` event carries it: calls and tokens per kind. */
export type BudgetSnapshot = {
  used: Record<string, number>;
  tokens?: Record<string, number>;
  limits: Record<string, number>;
  active_seconds: number;
  duration_seconds: number;
};
