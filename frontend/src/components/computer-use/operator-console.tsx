"use client";

import {
  Activity, BrainCircuit, Bot, Check, CheckCircle2, Circle, Cpu,
  Image as ImageIcon, LayoutList, Loader2, Pointer, RefreshCcw,
  Send, Settings, ShieldAlert, Sparkles, Square, Terminal, XCircle,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";

import { Textarea } from "@/components/ui/textarea";
import {
  approveAction, denyAction,
  getComputerUseStatus, getTextModelHealth,
  getAgentActivity, getTextModelSelection,
  listPendingApprovals,
  updateTextModelSelection,
  updateExecutionModeSelection, getExecutionModeSelection,
  getAgentApproachSelection, updateAgentApproachSelection,
  getVisionRecoverySelection, updateVisionRecoverySelection,
  getSupervisorSource, updateSupervisorSource,
  engageEmergencyStop, resetEmergencyStop,
  createOperatorTaskOwner, runComputerUseTask, summarizeApproval, useOperatorRuntimeState,
} from "@/core/computer-use";
import type { ActionOwner, AgentActivity, AgentApproach, ComputerUseRiskLevel, ComputerUseStatus, TextModelSelection, TextModelProviderPreset, SupervisorSource } from "@/core/computer-use";
import { cn } from "@/lib/utils";

type ChatMessage = {
  id: string;
  role: "user" | "agent";
  agentRole?: "planner" | "vision" | "uia" | "system";
  text: string;
  thought?: string;
  createdAt: Date;
};

type LogSource = "backend" | "frontend" | "harness";

export function ComputerUseOperatorConsole() {
  const {
    agentActivity, approvals, busy, draft,
    status, taskRunResult, patchDraft,
    run, setApprovals, logs,
    setAgentActivity, setStatus, setTaskRunResult,
  } = useOperatorRuntimeState();

  const [showLogs, setShowLogs] = useState(false);
  const [logSource, setLogSource] = useState<LogSource>("harness");
  const [rawLog, setRawLog] = useState<string>("");
  const [logLoading, setLogLoading] = useState(false);

  const loadLog = useCallback((source: LogSource) => {
    setLogLoading(true);
    fetch(`/api/computer-use/logs/${source}`)
      .then((res) => res.text())
      .then((text) => setRawLog(text))
      .catch((err) => setRawLog(`Failed to load ${source} log: ${String(err)}`))
      .finally(() => setLogLoading(false));
  }, []);

  useEffect(() => {
    if (showLogs) loadLog(logSource);
  }, [showLogs, logSource, loadLog]);

  const [chatHistory, setChatHistory] = useState<ChatMessage[]>([]);
  const [baselineActivity, setBaselineActivity] = useState<AgentActivity | null>(null);
  const [textModelSelection, setTextModelSelection] = useState<TextModelSelection | null>(null);
  const [executionMode, setExecutionMode] = useState<ComputerUseStatus["execution_mode"] | null>(null);
  const [agentApproach, setAgentApproach] = useState<AgentApproach | null>(null);
  const [visionRecovery, setVisionRecovery] = useState<boolean | null>(null);
  const [supervisorSource, setSupervisorSource] = useState<SupervisorSource | null>(null);
  const [supervisorApiConfigured, setSupervisorApiConfigured] = useState(false);
  const [supervisorApiModelName, setSupervisorApiModelName] = useState<string | null>(null);
  const [autoApproveRiskThreshold, setAutoApproveRiskThreshold] = useState<ComputerUseRiskLevel | null>(null);
  const [activeTaskOwner, setActiveTaskOwner] = useState<ActionOwner | null>(null);
  const pollingOwner = activeTaskOwner ?? draft.owner;

  useEffect(() => {
    void run("status", async () => {
      try { setStatus(await getComputerUseStatus()); } catch (err) { console.error("status", err); }
      try {
        const activity = await getAgentActivity(draft.owner.thread_id);
        setAgentActivity(activity);
        setBaselineActivity(prev => prev === null && activity ? activity : prev);
      } catch (err) { console.error("activity", err); }
      try { setTextModelSelection(await getTextModelSelection()); } catch (err) { console.error("text model", err); }
      try { setAgentApproach((await getAgentApproachSelection()).approach); } catch (err) { console.error("approach", err); }
      try { setExecutionMode((await getExecutionModeSelection()).execution_mode); } catch (err) { console.error("exec mode", err); }
      try { setVisionRecovery((await getVisionRecoverySelection()).enabled); } catch (err) { console.error("recovery", err); }
      try {
        const sup = await getSupervisorSource();
        setSupervisorSource(sup.source);
        setSupervisorApiConfigured(sup.api_configured);
        setSupervisorApiModelName(sup.api_model_name ?? null);
      } catch (err) { console.error("supervisor", err); }
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const poll = () => {
      if (!pollingOwner.thread_id.trim()) return;
      void getAgentActivity(pollingOwner.thread_id, pollingOwner.run_id).then(setAgentActivity).catch(() => undefined);
      void listPendingApprovals(pollingOwner).then(setApprovals).catch(() => undefined);
    };
    poll();
    const timer = window.setInterval(poll, 3000);
    return () => window.clearInterval(timer);
  }, [pollingOwner.agent_id, pollingOwner.run_id, pollingOwner.thread_id, setAgentActivity, setApprovals]);

  const displayActivity = useMemo(() => {
    if (!agentActivity?.agents) return null;
    const baselineMap = new Map(baselineActivity?.agents?.map(a => [a.agent_id, a]) ?? []);
    const newAgents = agentActivity.agents.map(a => {
      const base = baselineMap.get(a.agent_id);
      return {
        ...a,
        request_count: Math.max(0, a.request_count - (base?.request_count ?? 0)),
        total_tokens: Math.max(0, a.total_tokens - (base?.total_tokens ?? 0)),
      };
    });
    return { ...agentActivity, agents: newAgents };
  }, [agentActivity, baselineActivity]);

  const llmAgents = useMemo(() => {
    if (!displayActivity?.agents) return [];
    return displayActivity.agents.filter(a => !a.agent_id.toLowerCase().includes("vision") && !a.agent_id.toLowerCase().includes("fara") && !a.agent_id.toLowerCase().includes("tars"));
  }, [displayActivity]);

  const visionAgents = useMemo(() => {
    if (!displayActivity?.agents) return [];
    return displayActivity.agents.filter(a => a.agent_id.toLowerCase().includes("vision") || a.agent_id.toLowerCase().includes("fara") || a.agent_id.toLowerCase().includes("tars"));
  }, [displayActivity]);

  const plannerSummary = useMemo(() => summarizeModel(llmAgents), [llmAgents]);
  const visionSummary = useMemo(() => summarizeModel(visionAgents), [visionAgents]);

  const isRunning = busy === "run-computer-use-task";
  const liveAgent = agentActivity?.agents?.find(a => a.status === "running");

  const handleRunTask = () => {
    if (!draft.owner.thread_id.trim() || !draft.task_prompt.trim() || autoApproveRiskThreshold === null || busy !== null) return;
    const userPrompt = draft.task_prompt;
    setChatHistory(prev => [...prev, { id: crypto.randomUUID(), role: "user", text: userPrompt, createdAt: new Date() }]);
    patchDraft({ task_prompt: "" });

    void run("run-computer-use-task", async () => {
      try {
        const taskOwner = createOperatorTaskOwner(draft.owner.thread_id);
        setActiveTaskOwner(taskOwner);
        patchDraft({ owner: taskOwner });
        const result = await runComputerUseTask({
          thread_id: taskOwner.thread_id,
          run_id: taskOwner.run_id,
          prompt: userPrompt,
          auto_approve_risk_threshold: autoApproveRiskThreshold,
        });
        setTaskRunResult(result);
        setAgentActivity(await getAgentActivity(result.thread_id, taskOwner.run_id));

        const output = result.output && typeof result.output === "object" ? (result.output as Record<string, unknown>) : null;
        const taskStatus = typeof output?.status === "string" ? (output.status as string) : null;
        const isFailure = taskStatus === "failed" || taskStatus === "blocked" || Boolean(result.error);
        const friendly = extractAgentResponseText(result.output);
        const text = friendly ?? (isFailure ? `I hit an error: ${result.error ?? "unknown error"}` : "Done.");
        setChatHistory(prev => [...prev, {
          id: crypto.randomUUID(), role: "agent", agentRole: isFailure ? "system" : "planner", text, createdAt: new Date(),
        }]);
      } catch (error) {
        setChatHistory(prev => [...prev, {
          id: crypto.randomUUID(), role: "agent", agentRole: "system",
          text: error instanceof Error ? `System error: ${error.message}` : "An unexpected system error occurred.",
          createdAt: new Date(),
        }]);
        throw error;
      }
    });
  };

  const handleEmergencyStop = () => {
    // Engage the kill switch (aborts the running task's next action), then clear it so
    // new runs work. The running task surfaces its own stopped/failed message.
    void (async () => {
      try {
        await engageEmergencyStop("Operator pressed stop");
        await new Promise(r => setTimeout(r, 1200));
        await resetEmergencyStop("Cleared after stop");
      } catch (err) {
        console.error("emergency stop", err);
      }
    })();
  };

  const handleSwitchTextModel = (provider: TextModelProviderPreset) => {
    void run(`switch-text-${provider}`, async () => {
      setTextModelSelection(await updateTextModelSelection({ provider }));
      await getTextModelHealth();
    });
  };
  const handleSwitchExecutionMode = (mode: ComputerUseStatus["execution_mode"]) => {
    if (!mode) return;
    void run(`switch-execution-mode-${mode}`, async () => { setExecutionMode((await updateExecutionModeSelection(mode)).execution_mode); });
  };
  const handleSwitchApproach = (approach: AgentApproach) => {
    void run(`switch-approach-${approach}`, async () => { setAgentApproach((await updateAgentApproachSelection(approach)).approach); });
  };
  const handleSwitchVisionRecovery = (enabled: boolean) => {
    void run(`switch-recovery-${enabled}`, async () => { setVisionRecovery((await updateVisionRecoverySelection(enabled)).enabled); });
  };
  const handleSwitchSupervisorSource = (source: SupervisorSource) => {
    void run(`switch-supervisor-${source}`, async () => {
      const sel = await updateSupervisorSource(source);
      setSupervisorSource(sel.source);
      setSupervisorApiConfigured(sel.api_configured);
      setSupervisorApiModelName(sel.api_model_name ?? null);
    });
  };

  const empty = chatHistory.length === 0 && approvals.length === 0;

  return (
    <main className="relative flex h-screen flex-col overflow-hidden bg-[#0a0610] text-zinc-200">
      {/* Neon ambient glow */}
      <div className="pointer-events-none absolute inset-0 z-0 opacity-70 [background:radial-gradient(60%_45%_at_50%_-10%,rgba(168,85,247,0.22),transparent_70%),radial-gradient(45%_40%_at_100%_100%,rgba(217,70,239,0.12),transparent_70%)]" />

      {/* Header */}
      <header className="relative z-30 flex-none border-b border-white/5 bg-white/[0.02] px-5 py-3 backdrop-blur-xl">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="grid size-9 place-items-center rounded-xl bg-gradient-to-br from-fuchsia-500 to-violet-600 shadow-[0_0_22px_-2px_rgba(192,132,252,0.7)] ring-1 ring-fuchsia-400/30">
              <Sparkles className="size-[18px] text-white" />
            </div>
            <div className="leading-none">
              <h1 className="bg-gradient-to-r from-fuchsia-200 to-violet-200 bg-clip-text text-[15px] font-bold tracking-tight text-transparent">VILAGENT</h1>
              <span className="font-mono text-[10px] uppercase tracking-[0.18em] text-fuchsia-300/50">computer-use operator</span>
            </div>
            {agentApproach ? (
              <span className="ml-1 rounded-full border border-fuchsia-400/20 bg-fuchsia-500/10 px-2.5 py-1 font-mono text-[10px] uppercase tracking-wide text-fuchsia-200/80">
                {agentApproach === "autonomous" ? "Autonomous" : "Plan + Execute"}
              </span>
            ) : null}
          </div>
          <button
            type="button"
            onClick={() => setShowLogs(true)}
            className="flex items-center gap-1.5 rounded-lg border border-white/10 bg-white/[0.03] px-3 py-1.5 text-[11px] font-medium text-zinc-300 transition-colors hover:border-fuchsia-400/30 hover:bg-fuchsia-500/10 hover:text-fuchsia-100"
          >
            <Terminal className="size-3.5" /> Logs {logs.length > 0 ? `(${logs.length})` : ""}
          </button>
        </div>
      </header>

      {/* Body */}
      <div className="relative z-10 flex flex-1 overflow-hidden">
        {/* Chat column */}
        <section className="flex flex-1 flex-col overflow-hidden">
          <div className="flex-1 space-y-5 overflow-y-auto px-4 py-6 md:px-8">
            {empty ? (
              <div className="flex h-full flex-col items-center justify-center gap-4 px-6 text-center">
                <div className="grid size-16 place-items-center rounded-2xl bg-gradient-to-br from-fuchsia-500/90 to-violet-600/90 shadow-[0_0_40px_-6px_rgba(192,132,252,0.7)] ring-1 ring-fuchsia-400/30">
                  <Bot className="size-8 text-white" />
                </div>
                <div className="space-y-1.5">
                  <p className="text-[15px] font-semibold text-zinc-100">Ready to operate</p>
                  <p className="max-w-sm text-[12px] leading-relaxed text-zinc-400">
                    Describe a task and VILAGENT will plan it, then drive the desktop and browser under your control.
                  </p>
                </div>
              </div>
            ) : (
              <div className="mx-auto w-full max-w-3xl space-y-5">
                {chatHistory.map((msg) => (
                  <div key={msg.id} className={cn("flex w-full", msg.role === "user" ? "justify-end" : "justify-start")}>
                    {msg.role === "agent" && (
                      <div className="mr-2.5 mt-0.5 flex-shrink-0">
                        <div className={cn(
                          "grid size-7 place-items-center rounded-lg ring-1",
                          msg.agentRole === "system" ? "bg-red-500/10 text-red-400 ring-red-500/30" : "bg-fuchsia-500/10 text-fuchsia-300 ring-fuchsia-500/25",
                        )}>
                          {msg.agentRole === "vision" ? <ImageIcon className="size-3.5" /> :
                           msg.agentRole === "uia" ? <Pointer className="size-3.5" /> :
                           msg.agentRole === "system" ? <ShieldAlert className="size-3.5" /> : <Bot className="size-3.5" />}
                        </div>
                      </div>
                    )}
                    <div className={cn(
                      "max-w-[85%] rounded-2xl px-4 py-2.5 text-[13px] sm:max-w-[78%]",
                      msg.role === "user"
                        ? "rounded-br-md bg-gradient-to-br from-fuchsia-600 to-violet-600 text-white shadow-[0_0_22px_-8px_rgba(192,132,252,0.8)]"
                        : "rounded-bl-md border border-white/8 bg-white/[0.03] text-zinc-200",
                    )}>
                      {msg.role === "user"
                        ? <div className="whitespace-pre-wrap leading-relaxed">{msg.text}</div>
                        : <RichText text={msg.text} />}
                    </div>
                  </div>
                ))}

                {approvals.map((approval) => (
                  <div key={approval.approval_id} className="flex w-full justify-start">
                    <div className="mr-2.5 mt-0.5 flex-shrink-0">
                      <div className="grid size-7 place-items-center rounded-lg bg-amber-500/10 text-amber-400 ring-1 ring-amber-500/30">
                        <ShieldAlert className="size-3.5" />
                      </div>
                    </div>
                    <div className="w-full max-w-[85%] space-y-3 rounded-2xl rounded-bl-md border border-amber-400/30 bg-amber-500/[0.06] px-4 py-3.5 sm:max-w-[78%]">
                      <span className="font-mono text-[10px] font-semibold uppercase tracking-[0.14em] text-amber-300">Approval required</span>
                      {approval.args?.type !== "plan_approval" && (
                        <div className="whitespace-pre-wrap text-[13px] leading-relaxed text-zinc-200">{summarizeApproval(approval)}</div>
                      )}
                      {approval.args?.type === "plan_approval" && approval.args?.plan_json && (
                        <div className="rounded-lg border border-white/8 bg-black/20 p-3 text-[12px]">
                          <div className="mb-2 flex items-center gap-1.5 font-semibold text-zinc-200"><Bot className="size-3.5 text-fuchsia-300" /> Proposed plan</div>
                          <ol className="list-inside list-decimal space-y-1 text-zinc-400">
                            {parseApprovalPlanSteps(approval.args.plan_json).map((step, idx) => (
                              <li key={idx} className="leading-snug">
                                <span className="text-zinc-300">{step.instruction || step.description || "Step"}</span>
                                {step.requires_vision && <span className="ml-1.5 rounded bg-fuchsia-500/15 px-1.5 py-0.5 text-[9px] font-medium uppercase tracking-wide text-fuchsia-300">vision</span>}
                              </li>
                            ))}
                          </ol>
                        </div>
                      )}
                      <div className="flex gap-2">
                        <NeonButton tone="primary" className="flex-1" disabled={busy === `approve-${approval.approval_id}`} onClick={() => void run(`approve-${approval.approval_id}`, async () => {
                          const decided = await approveAction(approval.approval_id, { owner: approval.owner, decided_by: "operator-ui", reason: "Approved in console." });
                          setApprovals(c => c.filter(i => i.approval_id !== decided.approval_id));
                        })}>{approval.args?.type === "plan_approval" ? "Approve plan" : "Approve"}</NeonButton>
                        <NeonButton tone="ghost" className="flex-1" disabled={busy === `deny-${approval.approval_id}`} onClick={() => void run(`deny-${approval.approval_id}`, async () => {
                          const decided = await denyAction(approval.approval_id, { owner: approval.owner, decided_by: "operator-ui", reason: "Denied in console." });
                          setApprovals(c => c.filter(i => i.approval_id !== decided.approval_id));
                        })}>{approval.args?.type === "plan_approval" ? "Reject" : "Deny"}</NeonButton>
                      </div>
                    </div>
                  </div>
                ))}

                {isRunning && (
                  <div className="flex w-full justify-start">
                    <div className="mr-2.5 mt-0.5 flex-shrink-0">
                      <div className="grid size-7 place-items-center rounded-lg bg-fuchsia-500/10 text-fuchsia-300 ring-1 ring-fuchsia-500/25">
                        <Loader2 className="size-3.5 animate-spin" />
                      </div>
                    </div>
                    <div className="min-w-[260px] space-y-2.5 rounded-2xl rounded-bl-md border border-fuchsia-400/20 bg-white/[0.03] px-4 py-3">
                      <div className="flex items-center gap-2 text-[13px] text-zinc-200">
                        <span className="size-1.5 animate-pulse rounded-full bg-fuchsia-400 shadow-[0_0_8px_rgba(192,132,252,0.9)]" />
                        {liveAgent?.last_event ?? "Working…"}
                      </div>
                      {liveAgent?.current_thought && (
                        <details className="rounded-lg border border-white/8 bg-black/20">
                          <summary className="flex cursor-pointer list-none items-center gap-1.5 px-2.5 py-1.5 font-mono text-[10px] font-semibold uppercase tracking-wide text-fuchsia-300/70 [&::-webkit-details-marker]:hidden">
                            <BrainCircuit className="size-3 animate-pulse" /> Thinking
                          </summary>
                          <div className="whitespace-pre-wrap border-t border-white/8 px-2.5 pb-2 pt-1.5 font-mono text-[10.5px] leading-relaxed text-zinc-400">
                            {liveAgent.current_thought}
                          </div>
                        </details>
                      )}
                    </div>
                  </div>
                )}
              </div>
            )}
          </div>

          {/* Composer */}
          <div className="flex-none border-t border-white/5 bg-gradient-to-b from-transparent to-fuchsia-950/10 px-4 py-3">
            <div className="mx-auto w-full max-w-3xl">
              <div className="group flex items-end gap-2 rounded-2xl border border-white/10 bg-white/[0.03] p-2 shadow-[0_0_0_1px_rgba(0,0,0,0.2)] transition-all focus-within:border-fuchsia-400/40 focus-within:shadow-[0_0_28px_-8px_rgba(192,132,252,0.6)]">
                <Textarea
                  value={draft.task_prompt}
                  onChange={(e) => patchDraft({ task_prompt: e.target.value })}
                  placeholder={isRunning ? "VILAGENT is working… press stop to halt." : "Ask VILAGENT to do something…"}
                  disabled={isRunning}
                  className="max-h-[180px] min-h-[44px] w-full resize-none border-0 bg-transparent px-3 py-2.5 text-[13px] leading-relaxed text-zinc-100 shadow-none placeholder:text-zinc-500 focus-visible:ring-0 disabled:opacity-60"
                  onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); handleRunTask(); } }}
                />
                {isRunning ? (
                  <button
                    type="button"
                    onClick={handleEmergencyStop}
                    title="Emergency stop"
                    className="grid size-10 shrink-0 place-items-center rounded-xl bg-gradient-to-br from-red-500 to-rose-600 text-white shadow-[0_0_22px_-6px_rgba(244,63,94,0.9)] transition-transform hover:from-red-400 hover:to-rose-500 active:scale-95"
                  >
                    <Square className="size-4 fill-current" />
                  </button>
                ) : (
                  <button
                    type="button"
                    onClick={handleRunTask}
                    disabled={!draft.owner.thread_id.trim() || !draft.task_prompt.trim() || autoApproveRiskThreshold === null}
                    className="grid size-10 shrink-0 place-items-center rounded-xl bg-gradient-to-br from-fuchsia-500 to-violet-600 text-white shadow-[0_0_22px_-6px_rgba(192,132,252,0.9)] transition-transform hover:from-fuchsia-400 hover:to-violet-500 active:scale-95 disabled:opacity-30 disabled:shadow-none"
                  >
                    <Send className="size-4" />
                  </button>
                )}
              </div>
              <p className="mt-1.5 text-center font-mono text-[10px] tracking-tight text-zinc-500">
                {isRunning ? "■ Stop halts the run immediately" : "Enter to send · Shift+Enter for a new line"}
              </p>
            </div>
          </div>
        </section>

        {/* Right rail */}
        <aside className="z-10 hidden w-[340px] flex-none flex-col gap-5 overflow-y-auto border-l border-white/5 bg-white/[0.015] p-4 lg:flex xl:w-[380px]">
          {/* Plan */}
          <Panel icon={<LayoutList className="size-3.5" />} title="Plan">
            {(agentActivity?.plan_steps ?? []).length === 0 ? <Empty>No plan yet.</Empty> : (
              <div className="space-y-2.5">
                {(agentActivity?.plan_steps ?? []).map((step, idx, arr) => {
                  const done = step.status === "completed";
                  const running = step.status === "running";
                  const error = step.status === "failed" || step.status === "blocked";
                  return (
                    <div key={step.step_id} className="relative flex gap-3">
                      <div className="flex flex-col items-center">
                        <div className={cn(
                          "grid size-5 place-items-center rounded-full ring-1",
                          done ? "bg-emerald-500/15 text-emerald-400 ring-emerald-500/30"
                            : running ? "bg-fuchsia-500/15 text-fuchsia-300 ring-fuchsia-400/40 shadow-[0_0_12px_-2px_rgba(192,132,252,0.8)]"
                            : error ? "bg-red-500/15 text-red-400 ring-red-500/30"
                            : "bg-white/5 text-zinc-600 ring-white/10",
                        )}>
                          {done ? <CheckCircle2 className="size-3" /> : running ? <Loader2 className="size-3 animate-spin" /> : error ? <XCircle className="size-3" /> : <Circle className="size-2.5" />}
                        </div>
                        {idx < arr.length - 1 && <div className="my-0.5 w-px flex-1 bg-white/8" />}
                      </div>
                      <div className="min-w-0 space-y-0.5 pb-1.5">
                        <div className="flex items-start gap-1.5">
                          {step.requires_vision ? <ImageIcon className="mt-0.5 size-3 shrink-0 text-fuchsia-400/70" /> : <Pointer className="mt-0.5 size-3 shrink-0 text-amber-400/70" />}
                          <p className={cn("break-words text-[12px] leading-snug", done ? "text-zinc-500 line-through" : error ? "text-red-300" : "text-zinc-200")}>{step.instruction}</p>
                        </div>
                        {error && (step.error_code || step.summary) && <p className="text-[10px] leading-relaxed text-red-400/80">{step.error_code ?? step.summary}</p>}
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
            {approvals.length > 0 && (
              <div className="mt-3 rounded-lg border border-amber-400/30 bg-amber-500/[0.06] p-2.5">
                <div className="flex items-center gap-1.5 text-amber-300"><ShieldAlert className="size-3.5" /><span className="text-[11px] font-semibold">Awaiting approval</span></div>
                <p className="mt-1 text-[11px] text-zinc-400">Review it in the chat window.</p>
              </div>
            )}
          </Panel>

          {/* Models */}
          <Panel icon={<Activity className="size-3.5" />} title="Models">
            <div className="space-y-2.5">
              <ModelPanel
                icon={<Cpu className="size-3.5 text-violet-300" />}
                name="Planner LLM"
                connection={(textModelSelection?.provider ?? status?.text_model?.provider ?? "—").toUpperCase()}
                summary={plannerSummary}
              />
              <ModelPanel
                icon={<ImageIcon className="size-3.5 text-fuchsia-300" />}
                name="FARA Vision"
                connection="COLAB · PYNGROK"
                summary={visionSummary}
              />
            </div>
          </Panel>

          {/* Configuration */}
          <Panel icon={<Settings className="size-3.5" />} title="Configuration">
            <div className="space-y-3.5">
              <Field label="Auto-approve risk" hint="Steps up to this level run automatically.">
                <div className="grid grid-cols-4 gap-1">
                  {([["low", "Low"], ["medium", "Med"], ["high", "High"], ["critical", "Max"]] as const).map(([level, lbl]) => (
                    <Chip key={level} active={autoApproveRiskThreshold === level} onClick={() => setAutoApproveRiskThreshold(level)}>{lbl}</Chip>
                  ))}
                </div>
              </Field>

              <Field label="Planner">
                <div className="grid grid-cols-2 gap-1.5">
                  {(textModelSelection?.options || ["gemini", "glm", "ollama", "fara"]).map((p) => (
                    <Chip key={p} active={textModelSelection?.provider === p} onClick={() => handleSwitchTextModel(p as TextModelProviderPreset)}>{p.toUpperCase()}</Chip>
                  ))}
                </div>
              </Field>

              <Field label="Approach" hint="Plan + Execute breaks the task into steps. Autonomous lets FARA run the whole task.">
                <div className="grid grid-cols-2 gap-1.5">
                  <Chip active={agentApproach === "plan_execute"} onClick={() => handleSwitchApproach("plan_execute")}>Plan + Execute</Chip>
                  <Chip active={agentApproach === "autonomous"} onClick={() => handleSwitchApproach("autonomous")}>Autonomous</Chip>
                </div>
              </Field>

              {agentApproach !== "autonomous" && (
                <Field label="Execution mode">
                  <div className="grid grid-cols-2 gap-1.5">
                    <Chip active={executionMode === "hybrid"} onClick={() => handleSwitchExecutionMode("hybrid")}>Normal</Chip>
                    <Chip active={executionMode === "vision_only"} onClick={() => handleSwitchExecutionMode("vision_only")}>Vision-only</Chip>
                  </div>
                </Field>
              )}

              <Field label="Recovery supervisor" hint="When vision is stuck, a stronger model steps in once.">
                <div className="grid grid-cols-2 gap-1.5">
                  <Chip active={visionRecovery === false} onClick={() => handleSwitchVisionRecovery(false)}>Off</Chip>
                  <Chip active={visionRecovery === true} onClick={() => handleSwitchVisionRecovery(true)}>Supervised</Chip>
                </div>
                {visionRecovery && (
                  <div className="mt-2 space-y-1.5">
                    <div className="grid grid-cols-2 gap-1.5">
                      <Chip active={supervisorSource === "planner"} onClick={() => handleSwitchSupervisorSource("planner")}>Planner</Chip>
                      <Chip active={supervisorSource === "api"} disabled={!supervisorApiConfigured} onClick={() => handleSwitchSupervisorSource("api")}>GLM-V API</Chip>
                    </div>
                    <p className="text-[10px] leading-snug text-zinc-500">
                      {supervisorApiConfigured ? `GLM-V API: ${supervisorApiModelName ?? "configured"}.` : "Set VILAGENT_SUPERVISOR_API_KEY to enable the API option."}
                    </p>
                  </div>
                )}
              </Field>
            </div>
          </Panel>
        </aside>
      </div>

      {/* Logs drawer */}
      {showLogs && (
        <div className="absolute inset-0 z-50 flex justify-end bg-black/50 backdrop-blur-sm">
          <div className="flex h-full w-[560px] max-w-[92vw] flex-col border-l border-fuchsia-500/20 bg-[#0a0610] shadow-[0_0_60px_-10px_rgba(192,132,252,0.4)]">
            <div className="flex items-center justify-between border-b border-white/8 px-4 py-3">
              <h2 className="flex items-center gap-2 font-mono text-[12px] font-semibold uppercase tracking-[0.12em] text-fuchsia-200"><Terminal className="size-4 text-fuchsia-400" /> Logs</h2>
              <div className="flex items-center gap-1">
                <button type="button" onClick={() => loadLog(logSource)} title="Refresh" className="grid size-7 place-items-center rounded-md text-zinc-400 hover:bg-white/5 hover:text-fuchsia-200"><RefreshCcw className={cn("size-3.5", logLoading && "animate-spin")} /></button>
                <button type="button" onClick={() => setShowLogs(false)} className="grid size-7 place-items-center rounded-md text-zinc-400 hover:bg-white/5 hover:text-zinc-100"><XCircle className="size-4" /></button>
              </div>
            </div>
            <div className="flex items-center gap-1 border-b border-white/8 px-3 py-2">
              {(["backend", "frontend", "harness"] as const).map((src) => (
                <button key={src} type="button" onClick={() => setLogSource(src)} className={cn(
                  "rounded-md px-3 py-1 font-mono text-[10px] font-semibold uppercase tracking-wide transition-colors",
                  logSource === src ? "bg-gradient-to-br from-fuchsia-500 to-violet-600 text-white shadow-[0_0_14px_-4px_rgba(192,132,252,0.9)]" : "text-zinc-400 hover:bg-white/5 hover:text-fuchsia-200",
                )}>{src}</button>
              ))}
            </div>
            <div className="flex-1 overflow-auto bg-black/40">
              <pre className="min-h-full whitespace-pre-wrap p-3 font-mono text-[10.5px] leading-relaxed text-zinc-400">
                {logLoading && !rawLog ? "Loading…" : (rawLog || "(empty)")}
              </pre>
            </div>
          </div>
        </div>
      )}
    </main>
  );
}

// ----------------------------------------------------------------------------
// Presentational helpers
// ----------------------------------------------------------------------------

function Panel({ icon, title, children }: { icon: ReactNode; title: string; children: ReactNode }) {
  return (
    <section className="rounded-2xl border border-white/8 bg-white/[0.02] p-3.5">
      <h2 className="mb-3 flex items-center gap-2 font-mono text-[11px] font-semibold uppercase tracking-[0.14em] text-fuchsia-200/80">
        <span className="text-fuchsia-400">{icon}</span> {title}
      </h2>
      {children}
    </section>
  );
}

function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <div className="space-y-1.5">
      <p className="font-mono text-[10px] font-semibold uppercase tracking-wide text-zinc-500">{label}</p>
      {children}
      {hint && <p className="text-[10px] leading-snug text-zinc-600">{hint}</p>}
    </div>
  );
}

function Chip({ active, children, onClick, disabled }: { active: boolean; children: ReactNode; onClick: () => void; disabled?: boolean }) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className={cn(
        "h-7 rounded-lg border px-2 text-[11px] font-medium transition-all",
        active
          ? "border-fuchsia-400/50 bg-gradient-to-br from-fuchsia-500/90 to-violet-600/90 text-white shadow-[0_0_16px_-5px_rgba(192,132,252,0.9)]"
          : "border-white/10 bg-white/[0.02] text-zinc-400 hover:border-fuchsia-400/30 hover:bg-fuchsia-500/10 hover:text-fuchsia-100",
        disabled && "cursor-not-allowed opacity-30 hover:border-white/10 hover:bg-white/[0.02]",
      )}
    >
      {children}
    </button>
  );
}

function NeonButton({ tone, children, onClick, disabled, className }: { tone: "primary" | "ghost"; children: ReactNode; onClick: () => void; disabled?: boolean; className?: string }) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className={cn(
        "h-8 rounded-lg px-3 text-[12px] font-medium transition-all disabled:opacity-50",
        tone === "primary"
          ? "bg-gradient-to-br from-fuchsia-500 to-violet-600 text-white shadow-[0_0_18px_-6px_rgba(192,132,252,0.9)] hover:from-fuchsia-400 hover:to-violet-500"
          : "border border-white/12 bg-white/[0.03] text-zinc-300 hover:border-red-400/30 hover:bg-red-500/10 hover:text-red-200",
        className,
      )}
    >
      {children}
    </button>
  );
}

function Empty({ children }: { children: ReactNode }) {
  return <p className="rounded-lg border border-dashed border-white/10 bg-white/[0.02] p-2.5 text-center text-[11px] italic text-zinc-500">{children}</p>;
}

type ModelSummary = {
  requests: number;
  tokens: number;
  status: "running" | "pending" | "idle";
  thinking: string | null;
  lastEvent: string | null;
};

function summarizeModel(agents: AgentActivity["agents"]): ModelSummary {
  let requests = 0;
  let tokens = 0;
  let status: ModelSummary["status"] = "idle";
  let thinking: string | null = null;
  let lastEvent: string | null = null;
  for (const a of agents) {
    requests += a.request_count;
    tokens += a.total_tokens;
    if (a.status === "running") {
      status = "running";
      thinking = a.current_thought ?? thinking;
      lastEvent = a.last_event ?? lastEvent;
    } else if (a.status === "pending" && status !== "running") {
      status = "pending";
    }
  }
  return { requests, tokens, status, thinking, lastEvent };
}

function ModelPanel({ icon, name, connection, summary }: { icon: ReactNode; name: string; connection: string; summary: ModelSummary }) {
  const running = summary.status === "running";
  return (
    <div className={cn(
      "rounded-xl border bg-white/[0.02] p-3 transition-colors",
      running ? "border-fuchsia-400/40 shadow-[0_0_18px_-6px_rgba(192,132,252,0.7)]" : "border-white/8",
    )}>
      <div className="flex items-center justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2">
          <span className="grid size-6 shrink-0 place-items-center rounded-md bg-white/5">{icon}</span>
          <div className="min-w-0">
            <p className="truncate text-[12px] font-semibold text-zinc-100">{name}</p>
            <p className="truncate font-mono text-[9px] uppercase tracking-[0.1em] text-zinc-500">{connection}</p>
          </div>
        </div>
        <span className={cn(
          "flex shrink-0 items-center gap-1 rounded-full px-2 py-0.5 font-mono text-[9px] font-semibold uppercase tracking-wide",
          running ? "bg-fuchsia-500/15 text-fuchsia-300" : summary.status === "pending" ? "bg-amber-500/15 text-amber-300" : "bg-white/5 text-zinc-500",
        )}>
          {running ? <Loader2 className="size-2.5 animate-spin" /> : <Circle className="size-2 fill-current" />}
          {summary.status}
        </span>
      </div>
      <div className="mt-2.5 grid grid-cols-2 gap-1.5">
        <Metric label="Requests" value={summary.requests.toString()} />
        <Metric label="Tokens" value={summary.tokens.toLocaleString()} />
      </div>
      {running && (summary.thinking || summary.lastEvent) && (
        <details className="mt-2 rounded-lg border border-white/8 bg-black/20" open={Boolean(summary.thinking)}>
          <summary className="flex cursor-pointer list-none items-center gap-1.5 px-2.5 py-1.5 font-mono text-[9px] font-semibold uppercase tracking-wide text-fuchsia-300/70 [&::-webkit-details-marker]:hidden">
            <BrainCircuit className="size-3 animate-pulse" /> {summary.thinking ? "Thinking" : "Status"}
          </summary>
          <div className="whitespace-pre-wrap border-t border-white/8 px-2.5 pb-2 pt-1.5 font-mono text-[10px] leading-relaxed text-zinc-400">
            {summary.thinking ?? summary.lastEvent}
          </div>
        </details>
      )}
    </div>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg border border-white/8 bg-black/20 px-2.5 py-1.5">
      <p className="mb-0.5 font-mono text-[9px] font-medium uppercase tracking-[0.1em] text-zinc-500">{label}</p>
      <p className="font-mono text-[13px] font-semibold tabular-nums text-zinc-100">{value}</p>
    </div>
  );
}

function renderInline(text: string, keyBase: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  const regex = /(\*\*[^*]+\*\*|\*[^*]+\*)/g;
  let last = 0;
  let i = 0;
  let m: RegExpExecArray | null;
  while ((m = regex.exec(text)) !== null) {
    if (m.index > last) nodes.push(text.slice(last, m.index));
    const tok = m[0];
    if (tok.startsWith("**")) nodes.push(<strong key={`${keyBase}-${i}`} className="font-semibold text-zinc-100">{tok.slice(2, -2)}</strong>);
    else nodes.push(<em key={`${keyBase}-${i}`} className="text-zinc-400">{tok.slice(1, -1)}</em>);
    last = m.index + tok.length;
    i++;
  }
  if (last < text.length) nodes.push(text.slice(last));
  return nodes;
}

function RichText({ text }: { text: string }) {
  const lines = text.split("\n");
  return (
    <div className="space-y-1 text-[13px] leading-relaxed">
      {lines.map((line, idx) => {
        const trimmed = line.trim();
        if (!trimmed) return <div key={idx} className="h-1" />;
        const headline = /^([^*\s][^*]*\s)?\*\*(.+)\*\*$/.exec(trimmed);
        if (headline) {
          return <p key={idx} className="text-[13.5px] font-semibold text-zinc-100">{headline[1] ?? ""}{renderInline(headline[2] ?? "", `h${idx}`)}</p>;
        }
        if (trimmed.startsWith("✓ ") || trimmed.startsWith("✗ ")) {
          const ok = trimmed.startsWith("✓ ");
          return (
            <div key={idx} className="flex items-start gap-2">
              {ok ? <Check className="mt-0.5 size-3.5 shrink-0 text-emerald-400" /> : <XCircle className="mt-0.5 size-3.5 shrink-0 text-red-400" />}
              <span>{renderInline(trimmed.slice(2), `c${idx}`)}</span>
            </div>
          );
        }
        const numbered = /^(\d+)\.\s+(.*)$/.exec(trimmed);
        if (numbered) {
          return (
            <div key={idx} className="flex items-start gap-2">
              <span className="mt-px font-mono text-[11px] font-semibold text-fuchsia-300/80">{numbered[1]}.</span>
              <span>{renderInline(numbered[2] ?? "", `n${idx}`)}</span>
            </div>
          );
        }
        const note = /^_(.+)_$/.exec(trimmed);
        if (note) return <p key={idx} className="text-[12px] italic text-zinc-500">{renderInline(note[1] ?? "", `i${idx}`)}</p>;
        return <p key={idx}>{renderInline(trimmed, `p${idx}`)}</p>;
      })}
    </div>
  );
}

function extractAgentResponseText(output: unknown): string | null {
  if (typeof output === "string") return output;
  if (output === null || typeof output !== "object") return null;
  const record = output as Record<string, unknown>;
  const directError = record.error;
  if (typeof directError === "string" && directError.trim()) return directError;
  const messages = Array.isArray(record.messages) ? record.messages : [];
  for (const message of [...messages].reverse()) {
    const text = messageToText(message);
    if (text !== null && text.trim()) return text.trim();
  }
  const summary = record.summary;
  if (typeof summary === "string" && summary.trim()) return summary.trim();
  const status = record.status;
  if (typeof status === "string" && status.trim()) return `Task ${status}.`;
  return null;
}

function messageToText(message: unknown): string | null {
  if (typeof message === "string") return message;
  if (message === null || typeof message !== "object") return null;
  const record = message as Record<string, unknown>;
  const content = record.content;
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    const parts = content
      .map((part) => {
        if (typeof part === "string") return part;
        if (part !== null && typeof part === "object") {
          const text = (part as Record<string, unknown>).text;
          return typeof text === "string" ? text : "";
        }
        return "";
      })
      .filter(Boolean);
    return parts.length > 0 ? parts.join("\n") : null;
  }
  return null;
}

function parseApprovalPlanSteps(planJson: unknown): Array<Record<string, any>> {
  if (typeof planJson !== "string" || planJson.trim().length === 0) return [];
  try {
    const plan = JSON.parse(planJson) as unknown;
    if (plan === null || typeof plan !== "object") return [];
    const steps = (plan as Record<string, unknown>).steps;
    return Array.isArray(steps)
      ? steps.filter((step): step is Record<string, any> => step !== null && typeof step === "object")
      : [];
  } catch {
    return [];
  }
}
