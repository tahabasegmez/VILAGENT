import { fetch } from "@/core/api/fetcher";

import type {
  ActionCancelRequest,
  ActionLifecycleRecord,
  ActionOwner,
  AgentActivity,
  AgentApproach,
  AgentApproachSelection,
  ComputerUseArchitecture,
  ComputerUseExecutionMode,
  ExecutionModeSelection,
  ApprovalDecisionRequest,
  ApprovalRecord,
  BrowserActionSubmissionRequest,
  BrowserContext,
  BrowserHealth,
  BrowserSessionCreateRequest,
  BrowserStateSummary,
  ComputerUseConfigValidation,
  ComputerUseLifecycleEvent,
  ComputerUseStatus,
  ComputerUseTaskRunRequest,
  ComputerUseTaskRunResult,
  ComputerUseTextModelHealth,
  DesktopSessionSnapshot,
  Observation,
  TargetQuery,
  TargetResolutionResult,
  TextModelSelection,
  TextModelSelectionUpdate,
  VisionProviderHealth,
  VisionRecoverySelection,
  SupervisorSource,
  SupervisorSourceSelection,
} from "./types";

const COMPUTER_USE_BASE = "/api/computer-use";

export class ComputerUseApiError extends Error {
  constructor(
    message: string,
    public readonly status: number,
    public readonly detail: string | null,
  ) {
    super(message);
    this.name = "ComputerUseApiError";
  }
}

export async function getBrowserHealth(): Promise<BrowserHealth> {
  return requestJson<BrowserHealth>("/browser/health");
}

export async function getComputerUseStatus(): Promise<ComputerUseStatus> {
  return requestJson<ComputerUseStatus>("/status");
}



export async function getExecutionModeSelection(): Promise<ExecutionModeSelection> {
  return requestJson<ExecutionModeSelection>("/execution-mode");
}

export async function updateExecutionModeSelection(
  execution_mode: ComputerUseExecutionMode,
): Promise<ExecutionModeSelection> {
  return requestJson<ExecutionModeSelection>("/execution-mode", {
    method: "POST",
    body: { execution_mode },
  });
}

export async function getAgentApproachSelection(): Promise<AgentApproachSelection> {
  return requestJson<AgentApproachSelection>("/approach");
}

export async function updateAgentApproachSelection(
  approach: AgentApproach,
): Promise<AgentApproachSelection> {
  return requestJson<AgentApproachSelection>("/approach", {
    method: "POST",
    body: { approach },
  });
}

export async function getVisionRecoverySelection(): Promise<VisionRecoverySelection> {
  return requestJson<VisionRecoverySelection>("/vision/recovery");
}

export async function updateVisionRecoverySelection(
  enabled: boolean,
): Promise<VisionRecoverySelection> {
  return requestJson<VisionRecoverySelection>("/vision/recovery", {
    method: "POST",
    body: { enabled },
  });
}

export async function getSupervisorSource(): Promise<SupervisorSourceSelection> {
  return requestJson<SupervisorSourceSelection>("/vision/supervisor");
}

export async function updateSupervisorSource(
  source: SupervisorSource,
): Promise<SupervisorSourceSelection> {
  return requestJson<SupervisorSourceSelection>("/vision/supervisor", {
    method: "POST",
    body: { source },
  });
}

export async function validateComputerUseConfig(): Promise<ComputerUseConfigValidation> {
  return requestJson<ComputerUseConfigValidation>("/config/validation");
}

export async function getTextModelHealth(): Promise<ComputerUseTextModelHealth> {
  return requestJson<ComputerUseTextModelHealth>("/text-model/health");
}

export async function getAgentActivity(
  threadId: string,
  runId?: string,
): Promise<AgentActivity> {
  const params = new URLSearchParams({ thread_id: threadId });
  if (runId?.trim()) {
    params.set("run_id", runId.trim());
  }
  return requestJson<AgentActivity>(`/agents/activity?${params.toString()}`);
}

export async function getTextModelSelection(): Promise<TextModelSelection> {
  return requestJson<TextModelSelection>("/text-model/selection");
}

export async function updateTextModelSelection(
  request: TextModelSelectionUpdate,
): Promise<TextModelSelection> {
  return requestJson<TextModelSelection>("/text-model/selection", {
    method: "POST",
    body: request,
  });
}

export async function getVisionHealth(): Promise<VisionProviderHealth> {
  return requestJson<VisionProviderHealth>("/vision/health");
}

export async function runComputerUseTask(
  request: ComputerUseTaskRunRequest,
): Promise<ComputerUseTaskRunResult> {
  const threadId = request.thread_id.trim();
  if (threadId.length === 0) {
    throw new Error("Thread ID is required.");
  }
  const prompt = request.prompt.trim();
  if (prompt.length === 0) {
    throw new Error("Task prompt is required.");
  }
  const result = await requestJson<ComputerUseTaskRunResult>("/tasks/run", {
    method: "POST",
    body: {
      thread_id: threadId,
      run_id: request.run_id,
      prompt,
      auto_approve_risk_threshold: request.auto_approve_risk_threshold,
    },
  });
  return result;
}

export async function createDesktopSession(
  sessionId?: string,
): Promise<DesktopSessionSnapshot> {
  const normalizedSessionId = sessionId?.trim();
  return requestJson<DesktopSessionSnapshot>("/sessions", {
    method: "POST",
    body: {
      session_id:
        normalizedSessionId !== undefined && normalizedSessionId.length > 0
          ? normalizedSessionId
          : undefined,
    },
  });
}

export async function listDesktopSessions(): Promise<DesktopSessionSnapshot[]> {
  return requestJson<DesktopSessionSnapshot[]>("/sessions");
}

export async function createBrowserSession(
  request: BrowserSessionCreateRequest,
): Promise<BrowserStateSummary> {
  return requestJson<BrowserStateSummary>("/browser/sessions", {
    method: "POST",
    body: request,
  });
}

export async function listBrowserSessions(
  owner: ActionOwner,
): Promise<string[]> {
  return requestJson<string[]>(
    `/browser/sessions?${ownerSearchParams(owner).toString()}`,
  );
}

export async function closeBrowserSession(
  context: BrowserContext,
): Promise<void> {
  await requestVoid(
    `/browser/sessions/${encodeURIComponent(context.browser_session_id)}`,
    {
      method: "DELETE",
      body: context.owner,
    },
  );
}

export async function observeSession(sessionId: string): Promise<Observation> {
  return requestJson<Observation>(
    `/sessions/${encodeURIComponent(sessionId)}/observe`,
    { method: "POST" },
  );
}

export async function observeSessionWithBrowser(
  sessionId: string,
  context: BrowserContext,
): Promise<Observation> {
  return requestJson<Observation>(
    `/sessions/${encodeURIComponent(sessionId)}/browser/observe`,
    {
      method: "POST",
      body: context,
    },
  );
}

export async function resolveTarget(
  sessionId: string,
  query: TargetQuery,
): Promise<TargetResolutionResult> {
  return requestJson<TargetResolutionResult>(
    `/sessions/${encodeURIComponent(sessionId)}/targets/resolve`,
    {
      method: "POST",
      body: query,
    },
  );
}

export async function resolveBrowserTarget(
  sessionId: string,
  context: BrowserContext,
  query: TargetQuery,
): Promise<TargetResolutionResult> {
  return requestJson<TargetResolutionResult>(
    `/sessions/${encodeURIComponent(sessionId)}/browser/targets/resolve`,
    {
      method: "POST",
      body: { ...context, query },
    },
  );
}

export async function submitBrowserAction(
  request: BrowserActionSubmissionRequest,
): Promise<ActionLifecycleRecord> {
  return requestJson<ActionLifecycleRecord>("/browser/actions", {
    method: "POST",
    body: request,
  });
}

export async function getAction(
  actionId: string,
  owner: ActionOwner,
): Promise<ActionLifecycleRecord> {
  return requestJson<ActionLifecycleRecord>(
    `/actions/${encodeURIComponent(actionId)}?${ownerSearchParams(owner).toString()}`,
  );
}

export async function executeAction(
  actionId: string,
  owner: ActionOwner,
): Promise<ActionLifecycleRecord> {
  return requestJson<ActionLifecycleRecord>(
    `/actions/${encodeURIComponent(actionId)}/execute`,
    {
      method: "POST",
      body: { owner },
    },
  );
}

export async function cancelAction(
  actionId: string,
  request: ActionCancelRequest,
): Promise<ActionLifecycleRecord> {
  return requestJson<ActionLifecycleRecord>(
    `/actions/${encodeURIComponent(actionId)}/cancel`,
    {
      method: "POST",
      body: request,
    },
  );
}

export async function listPendingApprovals(
  owner: ActionOwner,
): Promise<ApprovalRecord[]> {
  return requestJson<ApprovalRecord[]>(
    `/approvals?${ownerSearchParams(owner).toString()}`,
  );
}

export type EmergencyStopStatus = { engaged: boolean; reason?: string | null };

export async function engageEmergencyStop(reason = "Operator pressed stop"): Promise<EmergencyStopStatus> {
  return requestJson<EmergencyStopStatus>("/emergency-stop/engage", {
    method: "POST",
    body: { reason },
  });
}

export async function resetEmergencyStop(reason = "Operator cleared stop"): Promise<EmergencyStopStatus> {
  return requestJson<EmergencyStopStatus>("/emergency-stop/reset", {
    method: "POST",
    body: { reason },
  });
}

export async function getApproval(
  approvalId: string,
  owner: ActionOwner,
): Promise<ApprovalRecord> {
  return requestJson<ApprovalRecord>(
    `/approvals/${encodeURIComponent(approvalId)}?${ownerSearchParams(owner).toString()}`,
  );
}

export async function approveAction(
  approvalId: string,
  request: ApprovalDecisionRequest,
): Promise<ApprovalRecord> {
  return decideApproval(approvalId, request, "approve");
}

export async function denyAction(
  approvalId: string,
  request: ApprovalDecisionRequest,
): Promise<ApprovalRecord> {
  return decideApproval(approvalId, request, "deny");
}

export async function listLifecycleEvents(
  owner: ActionOwner,
  options: { session_id?: string; after_sequence?: number; limit?: number } = {},
): Promise<ComputerUseLifecycleEvent[]> {
  return requestJson<ComputerUseLifecycleEvent[]>(
    `/events?${eventSearchParams(owner, options).toString()}`,
  );
}

export async function waitLifecycleEvents(
  owner: ActionOwner,
  options: {
    session_id?: string;
    after_sequence?: number;
    limit?: number;
    timeout_seconds?: number;
  } = {},
): Promise<ComputerUseLifecycleEvent[]> {
  return requestJson<ComputerUseLifecycleEvent[]>(
    `/events/wait?${eventSearchParams(owner, options).toString()}`,
  );
}

async function decideApproval(
  approvalId: string,
  request: ApprovalDecisionRequest,
  decision: "approve" | "deny",
): Promise<ApprovalRecord> {
  return requestJson<ApprovalRecord>(
    `/approvals/${encodeURIComponent(approvalId)}/${decision}`,
    {
      method: "POST",
      body: request,
    },
  );
}

function ownerSearchParams(owner: ActionOwner): URLSearchParams {
  return new URLSearchParams({
    thread_id: owner.thread_id,
    run_id: owner.run_id,
    agent_id: owner.agent_id,
  });
}

function eventSearchParams(
  owner: ActionOwner,
  options: {
    session_id?: string;
    after_sequence?: number;
    limit?: number;
    timeout_seconds?: number;
  },
): URLSearchParams {
  const params = ownerSearchParams(owner);
  if (options.session_id !== undefined) {
    params.set("session_id", options.session_id);
  }
  if (options.after_sequence !== undefined) {
    params.set("after_sequence", String(options.after_sequence));
  }
  if (options.limit !== undefined) {
    params.set("limit", String(options.limit));
  }
  if (options.timeout_seconds !== undefined) {
    params.set("timeout_seconds", String(options.timeout_seconds));
  }
  return params;
}

async function requestJson<T>(
  path: string,
  init: { method?: string; body?: unknown } = {},
): Promise<T> {
  const res = await fetch(`${COMPUTER_USE_BASE}${path}`, {
    method: init.method ?? "GET",
    headers:
      init.body === undefined
        ? undefined
        : { "Content-Type": "application/json" },
    body: init.body === undefined ? undefined : JSON.stringify(init.body),
  });
  if (!res.ok) {
    throw await toComputerUseApiError(res);
  }
  return (await res.json()) as T;
}

async function requestVoid(
  path: string,
  init: { method: string; body?: unknown },
): Promise<void> {
  const res = await fetch(`${COMPUTER_USE_BASE}${path}`, {
    method: init.method,
    headers:
      init.body === undefined
        ? undefined
        : { "Content-Type": "application/json" },
    body: init.body === undefined ? undefined : JSON.stringify(init.body),
  });
  if (!res.ok) {
    throw await toComputerUseApiError(res);
  }
}

async function toComputerUseApiError(
  res: Response,
): Promise<ComputerUseApiError> {
  const payload = (await res.json().catch(() => ({}))) as { detail?: unknown };
  const detail = typeof payload.detail === "string" ? payload.detail : null;
  return new ComputerUseApiError(
    detail ?? `Computer-use request failed: ${res.statusText}`,
    res.status,
    detail,
  );
}
