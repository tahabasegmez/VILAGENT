"use client";

import { useEffect, useMemo, useState } from "react";

import type { InterruptedRun } from "@/components/hud/interrupted-card";
import {
  answerApproval,
  applyTrace,
  ComputerUseApiError,
  createRunOwner,
  discardRun,
  EMPTY_TRACE,
  engageEmergencyStop,
  followRun,
  getApproach,
  getApprovalThreshold,
  getExecutionMode,
  getMemoryEnabled,
  getModels,
  getRun,
  getStatus,
  getVerifier,
  getVisionRecovery,
  listRuns,
  rateRun,
  resetEmergencyStop,
  resumeRun,
  runMemory,
  checkRole,
  deleteConnection,
  saveConnection,
  selectRole,
  setApproach,
  setApprovalThreshold,
  setExecutionMode,
  setMemoryEnabled,
  setVerifier as saveVerifier,
  setVisionRecovery as saveVisionRecovery,
  startRun,
  useOperatorRuntimeState,
  type Trace,
} from "@/core/computer-use";
import type {
  AgentActivity,
  AgentApproach,
  ApprovalScope,
  ApprovalThreshold,
  BudgetSnapshot,
  ComputerUseExecutionMode,
  MemoryRating,
  ModelRole,
  ModelRoles,
  RoleChoice,
  PendingApproval,
  RunFinished,
  RunStatus,
  TraceSpan,
  Verifier,
} from "@/core/computer-use";

const ACTIVE_RUN_STATUSES = new Set(["running", "awaiting_approval"]);
const LEAD_AGENT_ID = "computer_use_plan_execute";
const RUN_LABEL = "run-task";

export type RunOutcome = "success" | "failed" | null;
export type LiveThinking = { event: string | null; thought: string | null };
/** How the last run ended, in the agent's own words. */
export type RunResult = { status: RunStatus; text: string };
export type Usage = { requests: number; tokens: number };

/** Everything the HUD needs: settings, the live run (trace, plan, usage) and its handlers. */
export function useOperator() {
  const { agentActivity, busy, draft, status, patchDraft, run, logs, setAgentActivity, setStatus } = useOperatorRuntimeState();

  const [taskPrompt, setTaskPrompt] = useState<string | null>(null);
  const [trace, setTrace] = useState<Trace>(EMPTY_TRACE);
  const [budget, setBudget] = useState<BudgetSnapshot | null>(null);
  const [result, setResult] = useState<RunResult | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [modelRoles, setModelRoles] = useState<ModelRoles | null>(null);
  const [executionMode, setExecutionModeState] = useState<ComputerUseExecutionMode | null>(null);
  const [approach, setApproachState] = useState<AgentApproach | null>(null);
  const [recovery, setRecovery] = useState<boolean | null>(null);
  const [approvalThreshold, setApprovalThresholdState] = useState<ApprovalThreshold | null>(null);
  const [memory, setMemory] = useState<boolean | null>(null);
  const [approvals, setApprovals] = useState<PendingApproval[]>([]);
  const [currentRunId, setCurrentRunId] = useState<string | null>(null);
  const [interrupted, setInterrupted] = useState<InterruptedRun | null>(null);
  // The last finished run: how many memories it was given, and the operator's rating of it.
  const [lastRun, setLastRun] = useState<{ run_id: string; used: number; rating: MemoryRating } | null>(null);
  const [verifier, setVerifierState] = useState<Verifier | null>(null);
  const [supervisorSeesImages, setSupervisorSeesImages] = useState(true);
  const [running, setRunning] = useState(false);
  const [thinking, setThinking] = useState<LiveThinking>({ event: null, thought: null });
  const [outcome, setOutcome] = useState<RunOutcome>(null);

  // Load the persisted selections once.
  useEffect(() => {
    void run("load", async () => {
      const settle = async (label: string, load: () => Promise<void>) => {
        try {
          await load();
        } catch (error) {
          console.error(label, error);
        }
      };
      await settle("status", async () => setStatus(await getStatus()));
      await settle("models", async () => setModelRoles(await getModels()));
      await settle("approach", async () => setApproachState((await getApproach()).approach));
      await settle("execution mode", async () => setExecutionModeState((await getExecutionMode()).execution_mode));
      await settle("recovery", async () => setRecovery((await getVisionRecovery()).enabled));
      await settle("approval threshold", async () => setApprovalThresholdState((await getApprovalThreshold()).threshold));
      await settle("memory", async () => setMemory((await getMemoryEnabled()).enabled));
      await settle("verifier", async () => {
        const selection = await getVerifier();
        setVerifierState(selection.verifier);
        setSupervisorSeesImages(selection.supervisor_sees_images);
      });
    });
    // A run still going (e.g. the window was reloaded mid-run): follow it again.
    // A run cut off by an app close or crash: offer to resume it.
    void listRuns(1)
      .then(async ([latest]) => {
        if (!latest) return;
        if (ACTIVE_RUN_STATUSES.has(latest.status)) watch(latest.run_id, latest.prompt);
        if (latest.status === "interrupted") {
          const record = await getRun(latest.run_id);
          const step = record.activity?.plan_steps?.find((item) => item.status === "running")?.instruction ?? null;
          setInterrupted({ run_id: latest.run_id, prompt: latest.prompt, step });
        }
      })
      .catch(() => undefined);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const isRunning = running || busy === RUN_LABEL;

  // What the running agent last said, for the mini-HUD's thinking line.
  useEffect(() => {
    if (!isRunning) {
      setThinking({ event: null, thought: null });
      return;
    }
    const active = agentActivity?.agents?.find((agent) => agent.status === "running");
    const thought = active?.current_thought?.trim() ?? null;
    const event = active?.last_event?.trim() ?? null;
    if (thought ?? event) {
      setThinking((previous) => (previous.thought === thought && previous.event === event ? previous : { event, thought }));
    }
  }, [agentActivity, isRunning]);

  const planSteps = useMemo(() => agentActivity?.plan_steps ?? [], [agentActivity]);
  // "Acting" = driving the screen, as opposed to planning or writing the brief.
  const acting = isRunning && (planSteps.some((step) => step.status === "running") || (agentActivity?.agents ?? []).some((agent) => agent.agent_id !== LEAD_AGENT_ID && agent.status === "running"));

  // VLM = FARA; LLM = the planner (and brief) plus the supervisor.
  const usage = useMemo(() => {
    const used = budget?.used ?? {};
    const tokens = budget?.tokens ?? {};
    const sum = (record: Record<string, number>, ...kinds: string[]) => kinds.reduce((total, kind) => total + (record[kind] ?? 0), 0);
    return {
      vlm: { requests: sum(used, "vision"), tokens: sum(tokens, "vision") } satisfies Usage,
      llm: { requests: sum(used, "planner", "supervisor"), tokens: sum(tokens, "planner", "supervisor") } satisfies Usage,
    };
  }, [budget]);

  function begin(prompt: string) {
    setTaskPrompt(prompt);
    setTrace(EMPTY_TRACE);
    setBudget(null);
    setResult(null);
    setNotice(null);
    setOutcome(null);
    setLastRun(null);
    setAgentActivity(null);
  }

  function send(onStart: () => void) {
    const prompt = draft.task_prompt.trim();
    if (!draft.thread_id.trim() || !prompt || busy !== null) return;
    begin(prompt);
    patchDraft({ task_prompt: "" });
    setRunning(true);
    setThinking({ event: "Starting…", thought: null });
    onStart();

    void run(RUN_LABEL, async () => {
      try {
        const { run_id } = await startRun({ ...createRunOwner(draft.thread_id), prompt });
        report(await follow(run_id));
        rememberRun(run_id);
      } catch (error) {
        setOutcome("failed");
        // The gateway runs one task at a time (another window or client may own the run).
        const busyNote = error instanceof ComputerUseApiError && error.status === 409 ? " Wait for it to finish or press stop, then try again." : "";
        setNotice(error instanceof Error ? `${error.message}${busyNote}` : "An unexpected system error occurred.");
        throw error;
      } finally {
        setRunning(false);
      }
    });
  }

  /** Stream a run's events into the HUD; resolves with how it ended. */
  async function follow(runId: string): Promise<RunFinished> {
    let finished: RunFinished | null = null;
    setCurrentRunId(runId);
    try {
      await followRun(runId, (event) => {
        if (event.type === "trace") setTrace((current) => applyTrace(current, event.data as TraceSpan));
        if (event.type === "activity") {
          const data = event.data as { activity: AgentActivity; budget: BudgetSnapshot | null };
          setAgentActivity(data.activity);
          if (data.budget) setBudget(data.budget);
        }
        if (event.type === "approval.requested") setApprovals((previous) => [...previous, event.data as PendingApproval]);
        if (event.type === "approval.resolved") setApprovals((previous) => previous.filter((item) => item.id !== (event.data as { id: string }).id));
        if (event.type === "run.finished") {
          finished = event.data as RunFinished;
          setAgentActivity(finished.activity);
        }
      });
    } finally {
      setApprovals([]);
      setCurrentRunId(null);
    }
    // The stream can end without the event when the gateway restarted; its record has the ending.
    return finished ?? (await getRun(runId));
  }

  function report(finished: RunFinished) {
    const failed = finished.status !== "completed";
    setOutcome(failed ? "failed" : "success");
    const text = agentReplyText(finished.output ?? undefined) ?? (failed ? `I hit an error: ${finished.error ?? "unknown error"}` : "Done.");
    setResult({ status: finished.status, text });
  }

  function rememberRun(runId: string) {
    runMemory(runId)
      .then((used) => setLastRun({ run_id: runId, used: used.episodes.length + used.lessons.length, rating: null }))
      .catch(() => setLastRun({ run_id: runId, used: 0, rating: null }));
  }

  function rate(rating: MemoryRating) {
    if (!lastRun) return;
    rateRun(lastRun.run_id, rating)
      .then(() => setLastRun({ ...lastRun, rating }))
      .catch((error: unknown) => setNotice(error instanceof Error ? error.message : "The rating was not saved."));
  }

  /** Re-attach to a run that is already going (its events replay from the start). */
  function watch(runId: string, prompt: string) {
    begin(prompt);
    setRunning(true);
    setThinking({ event: "Following the running task…", thought: null });
    void run(RUN_LABEL, async () => {
      try {
        report(await follow(runId));
        rememberRun(runId);
      } finally {
        setRunning(false);
      }
    });
  }

  function resume() {
    if (!interrupted) return;
    const { run_id, prompt } = interrupted;
    setInterrupted(null);
    void resumeRun(run_id)
      .then(() => watch(run_id, prompt))
      .catch((error: unknown) => setNotice(error instanceof Error ? `Could not resume: ${error.message}` : "Could not resume."));
  }

  function discard() {
    if (!interrupted) return;
    const runId = interrupted.run_id;
    setInterrupted(null);
    discardRun(runId).catch((error: unknown) => console.error("discard", error));
  }

  function answer(approvalId: string, approve: boolean, scope: ApprovalScope = "once") {
    if (!currentRunId) return;
    // Not through run(): that would clear the busy flag while the task is still going.
    setApprovals((previous) => previous.filter((item) => item.id !== approvalId));
    answerApproval(currentRunId, approvalId, approve, scope).catch((error: unknown) => console.error("approval", error));
  }

  /** Point a role at a source and model; the step-check warning follows the supervisor's model. */
  async function selectModelRole(role: ModelRole, picked: RoleChoice) {
    setModelRoles(await selectRole(role, picked));
    setSupervisorSeesImages((await getVerifier()).supervisor_sees_images);
  }

  function stop() {
    // Engage the stop (it cancels the running task), then clear it so the next run works.
    void (async () => {
      try {
        await engageEmergencyStop("Operator pressed stop");
        await new Promise((resolve) => setTimeout(resolve, 1200));
        await resetEmergencyStop("Cleared after stop");
      } catch (error) {
        console.error("emergency stop", error);
      }
    })();
  }

  const settings = {
    roles: modelRoles,
    approach,
    executionMode,
    recovery,
    approvalThreshold,
    memory,
    verifier,
    supervisorSeesImages,
    onSelectRole: (role: ModelRole, picked: RoleChoice) => selectModelRole(role, picked),
    onCheckRole: (role: ModelRole) => checkRole(role),
    onSaveConnection: async (body: { name: string; kind: string; interface: string; params: Record<string, string> }, id = "") => setModelRoles(await saveConnection(body, id)),
    onDeleteConnection: async (id: string) => setModelRoles(await deleteConnection(id)),
    onApproach: (next: AgentApproach) => void run(`approach-${next}`, async () => setApproachState((await setApproach(next)).approach)),
    onExecutionMode: (mode: ComputerUseExecutionMode) =>
      void run(`execution-mode-${mode}`, async () => setExecutionModeState((await setExecutionMode(mode)).execution_mode)),
    onMemory: (enabled: boolean) => void run(`memory-${enabled}`, async () => setMemory((await setMemoryEnabled(enabled)).enabled)),
    onApprovalThreshold: (threshold: ApprovalThreshold) =>
      void run(`approval-threshold-${threshold}`, async () => setApprovalThresholdState((await setApprovalThreshold(threshold)).threshold)),
    onRecovery: (enabled: boolean) => void run(`recovery-${enabled}`, async () => setRecovery((await saveVisionRecovery(enabled)).enabled)),
    onVerifier: (next: Verifier) =>
      void run(`verifier-${next}`, async () => {
        const selection = await saveVerifier(next);
        setVerifierState(selection.verifier);
        setSupervisorSeesImages(selection.supervisor_sees_images);
      }),
  };

  return {
    draft,
    patchDraft,
    logs,
    status,
    taskPrompt,
    trace,
    planSteps,
    usage,
    settings,
    isRunning,
    acting,
    thinking,
    outcome,
    result,
    notice,
    dismissNotice: () => setNotice(null),
    approvals,
    answer,
    interrupted,
    resume,
    discard,
    lastRun,
    rate,
    send,
    stop,
  };
}

export type Operator = ReturnType<typeof useOperator>;
export type Settings = Operator["settings"];

/** The text VILAGENT should show for a finished run. */
function agentReplyText(output: Record<string, unknown> | undefined): string | null {
  if (!output) return null;
  const messages = Array.isArray(output.messages) ? output.messages : [];
  for (const message of [...messages].reverse()) {
    const text = messageText(message);
    if (text?.trim()) return text.trim();
  }
  if (typeof output.error === "string" && output.error.trim()) return output.error;
  if (typeof output.summary === "string" && output.summary.trim()) return output.summary.trim();
  if (typeof output.status === "string" && output.status.trim()) return `Task ${output.status}.`;
  return null;
}

function messageText(message: unknown): string | null {
  if (typeof message === "string") return message;
  if (message === null || typeof message !== "object") return null;
  const content = (message as { content?: unknown }).content;
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    const parts = content
      .map((part) => (typeof part === "string" ? part : typeof (part as { text?: unknown })?.text === "string" ? (part as { text: string }).text : ""))
      .filter(Boolean);
    return parts.length > 0 ? parts.join("\n") : null;
  }
  return null;
}
