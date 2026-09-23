import { getDesktop } from "@/core/desktop";

import type {
  AgentApproach,
  ApprovalScope,
  ApprovalThreshold,
  ApprovalThresholdSelection,
  BrowserSelection,
  MemoryEntries,
  MemoryEpisode,
  MemoryKeyType,
  MemoryLesson,
  MemoryRating,
  AgentApproachSelection,
  ComputerUseExecutionMode,
  ComputerUseStatus,
  ConnectionCheck,
  ModelRole,
  ModelRoles,
  RoleChoice,
  EmergencyStopStatus,
  ExecutionModeSelection,
  LogSource,
  RunRecord,
  RunStarted,
  RunSummary,
  TaskRunRequest,
  Verifier,
  VerifierSelection,
  VisionRecoverySelection,
} from "./types";

const AUTH_HEADER = "X-VILAGENT-Internal-Token";

export class ComputerUseApiError extends Error {
  constructor(
    message: string,
    public readonly status: number,
  ) {
    super(message);
    this.name = "ComputerUseApiError";
  }
}

export function apiUrl(path: string): string {
  return `${getDesktop()?.apiBase ?? ""}/api/computer-use${path}`;
}

export function authHeaders(): Record<string, string> {
  const token = getDesktop()?.authToken;
  return token ? { [AUTH_HEADER]: token } : {};
}

async function request(path: string, init: { method?: string; body?: unknown } = {}): Promise<Response> {
  const headers = authHeaders();
  if (init.body !== undefined) headers["Content-Type"] = "application/json";
  const res = await fetch(apiUrl(path), {
    method: init.method ?? "GET",
    headers,
    body: init.body === undefined ? undefined : JSON.stringify(init.body),
  });
  if (!res.ok) {
    const payload = (await res.json().catch(() => ({}))) as { detail?: unknown };
    const detail = typeof payload.detail === "string" ? payload.detail : `Request failed: ${res.status} ${res.statusText}`;
    throw new ComputerUseApiError(detail, res.status);
  }
  return res;
}

async function getJson<T>(path: string): Promise<T> {
  return (await (await request(path)).json()) as T;
}

async function postJson<T>(path: string, body: unknown): Promise<T> {
  return (await (await request(path, { method: "POST", body })).json()) as T;
}

async function sendJson<T>(method: "PATCH" | "DELETE", path: string, body?: unknown): Promise<T> {
  return (await (await request(path, { method, body })).json()) as T;
}

export const getStatus = () => getJson<ComputerUseStatus>("/status");

export const getExecutionMode = () => getJson<ExecutionModeSelection>("/execution-mode");
export const setExecutionMode = (execution_mode: ComputerUseExecutionMode) =>
  postJson<ExecutionModeSelection>("/execution-mode", { execution_mode });

export const getApproach = () => getJson<AgentApproachSelection>("/approach");
export const setApproach = (approach: AgentApproach) => postJson<AgentApproachSelection>("/approach", { approach });

export const getVisionRecovery = () => getJson<VisionRecoverySelection>("/vision/recovery");
export const setVisionRecovery = (enabled: boolean) => postJson<VisionRecoverySelection>("/vision/recovery", { enabled });

export const getMemoryEnabled = () => getJson<{ enabled: boolean }>("/memory/enabled");
export const setMemoryEnabled = (enabled: boolean) => postJson<{ enabled: boolean }>("/memory/enabled", { enabled });

export const searchMemory = (q: string) => getJson<MemoryEntries>(`/memory/search?${new URLSearchParams({ q }).toString()}`);
export const listEpisodes = () => getJson<MemoryEpisode[]>("/memory/episodes");
/** Embed one entry, or every run or lesson, with the embedding connection in use. */
export const embedMemory = (entry_type: "episodes" | "lessons", entry_id?: string) =>
  postJson<{ embedded: number; model: string }>("/memory/embed", { entry_type, entry_id: entry_id ?? null });
export const rateEpisode = (id: string, rating: MemoryRating) => sendJson<{ id: string }>("PATCH", `/memory/episodes/${id}`, { rating });
export const deleteEpisode = (id: string) => sendJson<{ id: string }>("DELETE", `/memory/episodes/${id}`);
export const listLessons = () => getJson<MemoryLesson[]>("/memory/lessons");
export const addNote = (key_type: MemoryKeyType, key: string, text: string) => postJson<{ id: string; key: string }>("/memory/lessons", { key_type, key, text });
export const editLesson = (id: string, fields: { text?: string; disabled?: boolean; key_type?: MemoryKeyType; key?: string }) =>
  sendJson<{ id: string }>("PATCH", `/memory/lessons/${id}`, fields);
export const deleteLesson = (id: string) => sendJson<{ id: string }>("DELETE", `/memory/lessons/${id}`);
export const runMemory = (runId: string) => getJson<MemoryEntries>(`/runs/${encodeURIComponent(runId)}/memory`);
export const rateRun = (runId: string, rating: MemoryRating) => postJson<{ run_id: string }>(`/runs/${encodeURIComponent(runId)}/rating`, { rating });
export const clearMemory = () => postJson<{ cleared: boolean }>("/memory/clear", { confirm: true });

export const getApprovalThreshold = () => getJson<ApprovalThresholdSelection>("/approvals/threshold");
export const setApprovalThreshold = (threshold: ApprovalThreshold) => postJson<ApprovalThresholdSelection>("/approvals/threshold", { threshold });
export const answerApproval = (runId: string, approvalId: string, approve: boolean, scope: ApprovalScope = "once") =>
  postJson<{ id: string; approve: boolean }>(`/runs/${encodeURIComponent(runId)}/approvals/${encodeURIComponent(approvalId)}`, { approve, scope });

export const getVerifier = () => getJson<VerifierSelection>("/vision/verifier");
export const setVerifier = (verifier: Verifier) => postJson<VerifierSelection>("/vision/verifier", { verifier });

/** The models config.yaml defines and which plays each role; choosing validates the name. */
export const getModels = () => getJson<ModelRoles>("/models");
/** Point one role at a source and model; the answer is the whole screen's new state. */
export const selectRole = (role: ModelRole, choice: RoleChoice) => postJson<ModelRoles>(`/models/${role}`, choice);
/** Ask one role's model a real question (can take a few seconds). */
export const checkRole = (role: ModelRole) => postJson<ConnectionCheck>(`/models/${role}/check`, {});

/** Add an API connection, or update one; the key is sent once and stored encrypted. */
export const saveConnection = (body: { name: string; kind: string; interface: string; params: Record<string, string> }, id = "") =>
  postJson<ModelRoles>(`/connections${id ? `?connection_id=${encodeURIComponent(id)}` : ""}`, body);
/** Forget a connection and its key. */
export const deleteConnection = (id: string) => sendJson<ModelRoles>("DELETE", `/connections/${encodeURIComponent(id)}`);

/** Which browser and profile FARA drives; choosing one closes the open browser. */
export const getBrowser = () => getJson<BrowserSelection>("/browser");
export const selectBrowser = (choice: { use_profile: boolean; channel?: string; user_data_dir?: string; profile_directory?: string }) =>
  postJson<BrowserSelection>("/browser", { channel: "", user_data_dir: "", profile_directory: "", ...choice });

/** Start a task; follow it with `followRun`. The gateway answers 409 while another run is active. */
export function startRun(req: TaskRunRequest): Promise<RunStarted> {
  const prompt = req.prompt.trim();
  if (!req.thread_id.trim()) throw new Error("Thread ID is required.");
  if (!prompt) throw new Error("Task prompt is required.");
  return postJson<RunStarted>("/runs", { thread_id: req.thread_id.trim(), run_id: req.run_id, prompt });
}

export const getRun = (runId: string) => getJson<RunRecord>(`/runs/${encodeURIComponent(runId)}`);
export const listRuns = (limit = 20) => getJson<RunSummary[]>(`/runs?limit=${limit}`);
export const resumeRun = (runId: string) => postJson<RunStarted>(`/runs/${encodeURIComponent(runId)}/resume`, {});
export const discardRun = (runId: string) => postJson<{ run_id: string; discarded: boolean }>(`/runs/${encodeURIComponent(runId)}/discard`, {});
export const cancelRun = (runId: string) => postJson<{ run_id: string; cancelled: boolean }>(`/runs/${encodeURIComponent(runId)}/cancel`, {});

export const engageEmergencyStop = (reason = "Operator pressed stop") =>
  postJson<EmergencyStopStatus>("/emergency-stop/engage", { reason });
export const resetEmergencyStop = (reason = "Operator cleared stop") =>
  postJson<EmergencyStopStatus>("/emergency-stop/reset", { reason });

export async function getLog(source: LogSource): Promise<string> {
  return (await request(`/logs/${source}`)).text();
}

export async function clearLog(source: LogSource): Promise<void> {
  await request(`/logs/${source}`, { method: "DELETE" });
}
