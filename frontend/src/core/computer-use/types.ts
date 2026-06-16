export type ActionOwner = {
  thread_id: string;
  run_id: string;
  agent_id: string;
};

export type BrowserHealth = {
  enabled: boolean;
  healthy: boolean;
  provider_name: string;
  active_sessions: number;
  error_code?: string | null;
};

export type ComputerUseStatus = {
  enabled: boolean;
  agent_mode: string;
  architecture: ComputerUseArchitecture;
  execution_mode: ComputerUseExecutionMode;
  assistant_id: "computer_use_agent" | string;
  prompt_profile: string;
  platform: string;
  runtime_mode: string;
  text_model: {
    provider: "api" | "pyngrok" | string;
    model_config_name?: string | null;
    model_name?: string | null;
    configured: boolean;
    endpoint_configured: boolean;
  };
  vision_model: {
    provider: "fara" | string;
    enabled: boolean;
    model_name: string;
    endpoint_configured: boolean;
    endpoint_path: string;
  };
  browser_enabled: boolean;
  allowed_actions: string[];
  budgets: {
    token_usage_enabled: boolean;
    planner_calls: number;
    vision_calls: number;
    total_actions: number;
    duration_seconds: number;
  };
};

export type ComputerUseArchitecture = "react_graph" | "plan_execute";

export type ArchitectureSelection = {
  architecture: ComputerUseArchitecture;
  options: ComputerUseArchitecture[];
};

export type ComputerUseExecutionMode = "hybrid" | "vision_only";
export type ComputerUseRiskLevel = "low" | "medium" | "high" | "critical";

export type ExecutionModeSelection = {
  execution_mode: ComputerUseExecutionMode;
  options: ComputerUseExecutionMode[];
};

export type VisionRecoverySelection = {
  enabled: boolean;
};

export type SupervisorSource = "planner" | "api";

export type SupervisorSourceSelection = {
  source: SupervisorSource;
  options: SupervisorSource[];
  api_configured: boolean;
  api_model_name?: string | null;
};

export type VisionProviderHealth = {
  provider_name: string;
  enabled: boolean;
  healthy: boolean;
  endpoint_configured: boolean;
  model_name: string;
  error_code?: string | null;
  details: Record<string, unknown>;
};

export type ComputerUseConfigCheck = {
  key: string;
  status: "ok" | "warn" | "error" | string;
  message: string;
};

export type ComputerUseConfigValidation = {
  healthy: boolean;
  config_path?: string | null;
  env_path?: string | null;
  checks: ComputerUseConfigCheck[];
};

export type ComputerUseTextModelHealth = {
  provider_name: string;
  provider: "api" | "pyngrok" | string;
  healthy: boolean;
  configured: boolean;
  endpoint_configured: boolean;
  probe_supported: boolean;
  model_config_name?: string | null;
  model_name?: string | null;
  endpoint_kind: string;
  error_code?: string | null;
  details: Record<string, unknown>;
};

export type TextModelProviderPreset = "gemini" | "glm" | "ollama" | "fara";

export type TextModelPresetInfo = {
  provider: TextModelProviderPreset;
  model_config_name: string;
  model_name: string;
  api_key_configured: boolean;
  base_url?: string | null;
};

export type TextModelSelection = {
  provider: TextModelProviderPreset;
  selected_config_name?: string | null;
  selected_model_name?: string | null;
  options?: string[];
  gemini: TextModelPresetInfo;
  glm: TextModelPresetInfo;
  ollama: TextModelPresetInfo;
  fara: TextModelPresetInfo;
};

export type TextModelSelectionUpdate = {
  provider: TextModelProviderPreset;
  model_name?: string | null;
  api_key?: string | null;
  base_url?: string | null;
};

export type AgentActivityItem = {
  agent_id: string;
  role: "lead" | "subagent" | string;
  status: "idle" | "pending" | "running" | string;
  task?: string | null;
  model_name?: string | null;
  request_count: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  tool_calls: string[];
  last_event?: string | null;
  current_thought?: string | null;
  last_updated_at?: string | null;
};

export type AgentActivity = {
  thread_id: string;
  run_id?: string | null;
  agents: AgentActivityItem[];
  plan_steps: PlanStepActivityItem[];
  total_request_count: number;
  total_input_tokens: number;
  total_output_tokens: number;
  total_tokens: number;
};

export type PlanStepActivityItem = {
  step_id: string;
  instruction: string;
  completion_criteria: string;
  max_actions?: number;
  status: "pending" | "running" | "completed" | "blocked" | "failed" | "skipped" | string;
  requires_vision: boolean;
  error_code?: string | null;
  summary?: string | null;
};

export type ComputerUseTaskRunRequest = {
  thread_id: string;
  run_id?: string;
  prompt: string;
  auto_approve_risk_threshold: ComputerUseRiskLevel;
};

export type ComputerUseTaskRunResult = {
  thread_id: string;
  assistant_id: "computer_use_agent";
  output: unknown;
  error?: string | null;
};

export type DesktopSessionSnapshot = {
  session: {
    session_id: string;
    platform: string;
    monitor_id: string;
    created_at: string;
  };
  status: "ready" | "stopped";
  provider_name: string;
  provider_health: "healthy" | "degraded" | "stopped";
  latest_observation_id?: string | null;
  last_error?: StructuredError | null;
};

export type BrowserStateSummary = {
  url?: string | null;
  title?: string | null;
  tab_id?: string | null;
  allowed_domain?: boolean | null;
};

export type Condition = {
  kind: string;
  operator?: string;
  selector?: Record<string, unknown>;
  value?: unknown;
};

export type StructuredError = {
  code: string;
  message: string;
  retryable?: boolean;
};

export type BrowserContext = {
  owner: ActionOwner;
  browser_session_id: string;
};

export type BrowserSessionCreateRequest = {
  owner: ActionOwner;
  url: string;
};

export type Rect = {
  x: number;
  y: number;
  width: number;
  height: number;
};

export type MonitorRef = {
  monitor_id: string;
  bounds: Rect;
  primary?: boolean;
  dpi_scale?: number;
};

export type BlobRef = {
  blob_id: string;
  media_type: string;
  size_bytes: number;
  sha256: string;
};

export type WindowRef = {
  window_id: string;
  title: string;
  process_name?: string | null;
  process_id?: number | null;
  bounds?: Rect | null;
};

export type Observation = {
  observation_id: string;
  session_id: string;
  previous_observation_id?: string | null;
  created_at: string;
  active_window?: WindowRef | null;
  screenshot_ref?: BlobRef | null;
  ui_tree_ref?: BlobRef | null;
  browser_state?: BrowserStateSummary | null;
  monitor: MonitorRef;
  screen_size: { width: number; height: number };
  diff_from_previous?: number | null;
  redaction_applied: boolean;
  summary?: string | null;
};

export type TargetStrategy =
  | "app"
  | "browser"
  | "uia"
  | "vision"
  | "coordinate";

export type TargetQuery = {
  description: string;
  selector_hints?: Record<string, unknown>;
  allowed_strategies?: TargetStrategy[];
  minimum_confidence?: number;
};

export type TargetRef = {
  strategy: TargetStrategy;
  selector: Record<string, unknown>;
  bounds?: Rect | null;
  confidence: number;
  observation_id: string;
  expected_window?: WindowRef | null;
};

export type TargetResolutionAttempt = {
  provider_name: string;
  strategy: TargetStrategy;
  outcome: "resolved" | "not_found" | "rejected" | "error";
  confidence?: number | null;
  error_code?: string | null;
};

export type TargetResolutionResult = {
  target?: TargetRef | null;
  attempts: TargetResolutionAttempt[];
};

export type BrowserActionSubmissionRequest = {
  owner: ActionOwner;
  session_id: string;
  target: TargetRef;
  browser_state: BrowserStateSummary;
  browser_action?: string;
  args?: Record<string, unknown>;
  postconditions?: Condition[];
  action_id?: string | null;
  idempotency_key?: string | null;
  timeout_seconds?: number;
};

export type ActionLifecycleRecord = {
  action: {
    action_id: string;
    session_id: string;
    kind: string;
    target?: TargetRef | null;
    args: Record<string, unknown>;
    postconditions: Condition[];
  };
  owner: ActionOwner;
  status:
    | "pending"
    | "awaiting_approval"
    | "approved"
    | "denied"
    | "executing"
    | "succeeded"
    | "failed"
    | "uncertain"
    | "cancelled";
  action_fingerprint: string;
  approval_id?: string | null;
  result?: unknown;
  error?: StructuredError | null;
  created_at?: string;
  updated_at?: string;
  completed_at?: string | null;
};

export type ApprovalRecord = {
  approval_id: string;
  action_id: string;
  session_id: string;
  owner: ActionOwner;
  action_fingerprint: string;
  status: "pending" | "approved" | "denied" | "expired" | "cancelled";
  reasons: string[];
  consequences: string[];
  args?: Record<string, any> | null;
  created_at: string;
  expires_at: string;
  decided_at?: string | null;
  decided_by?: string | null;
  decision_reason?: string | null;
};

export type ComputerUseLifecycleEvent = {
  sequence: number;
  event_type:
    | "action_submitted"
    | "action_status_changed"
    | "approval_requested"
    | "approval_decided";
  owner: ActionOwner;
  session_id: string;
  action_id: string;
  action_kind: string;
  action_status?: ActionLifecycleRecord["status"] | null;
  approval_id?: string | null;
  approval_status?: ApprovalRecord["status"] | null;
  error_code?: string | null;
  created_at: string;
};

export type ApprovalDecisionRequest = {
  owner: ActionOwner;
  decided_by: string;
  reason?: string | null;
};

export type ActionCancelRequest = {
  owner: ActionOwner;
  reason?: string | null;
};
