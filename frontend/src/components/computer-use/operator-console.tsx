"use client";

import {
  Activity, CheckCircle2, Play, RefreshCcw, ShieldAlert,
  XCircle, Circle, Loader2, MessageSquare, Image as ImageIcon,
  Code, Globe, Terminal, Pointer, Check, LayoutList, User, Bot, BrainCircuit, Settings
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState, type ComponentProps, type ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import {
  approveAction, canCancelAction, canExecuteAction, cancelAction,
  denyAction, executeAction,
  getComputerUseStatus, getTextModelHealth,
  getAgentActivity, getTextModelSelection,
  listPendingApprovals,
  updateTextModelSelection,
  updateExecutionModeSelection, getExecutionModeSelection,
  getAgentApproachSelection, updateAgentApproachSelection,
  getVisionRecoverySelection, updateVisionRecoverySelection,
  getSupervisorSource, updateSupervisorSource,
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

export function ComputerUseOperatorConsole() {
  const {
    agentActivity, approvals, busy, draft, 
    status, taskRunResult, patchDraft,
    run, setApprovals, logs,
    setAgentActivity, setStatus, setTaskRunResult,
  } = useOperatorRuntimeState();

  const [showLogs, setShowLogs] = useState(false);
  const [logSource, setLogSource] = useState<"backend" | "frontend" | "harness">("harness");
  const [rawLog, setRawLog] = useState<string>("");
  const [logLoading, setLogLoading] = useState(false);

  const loadLog = useCallback((source: "backend" | "frontend" | "harness") => {
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
      try {
        const next = await getComputerUseStatus();
        setStatus(next);
      } catch (err) {
        console.error("Failed to fetch computer use status:", err);
      }

      try {
        const activity = await getAgentActivity(draft.owner.thread_id);
        setAgentActivity(activity);
        setBaselineActivity(prev => prev === null && activity ? activity : prev);
      } catch (err) {
        console.error("Failed to fetch agent activity:", err);
      }

      try {
        const selection = await getTextModelSelection();
        setTextModelSelection(selection);
      } catch (err) {
        console.error("Failed to fetch text model selection:", err);
      }

      try {
        const approachSelection = await getAgentApproachSelection();
        setAgentApproach(approachSelection.approach);
      } catch (err) {
        console.error("Failed to fetch agent approach selection:", err);
      }

      try {
        const executionSelection = await getExecutionModeSelection();
        setExecutionMode(executionSelection.execution_mode);
      } catch (err) {
        console.error("Failed to fetch execution mode selection:", err);
      }

      try {
        const recoverySelection = await getVisionRecoverySelection();
        setVisionRecovery(recoverySelection.enabled);
      } catch (err) {
        console.error("Failed to fetch recovery supervisor selection:", err);
      }

      try {
        const supervisorSelection = await getSupervisorSource();
        setSupervisorSource(supervisorSelection.source);
        setSupervisorApiConfigured(supervisorSelection.api_configured);
        setSupervisorApiModelName(supervisorSelection.api_model_name ?? null);
      } catch (err) {
        console.error("Failed to fetch supervisor source selection:", err);
      }
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const poll = () => {
      if (!pollingOwner.thread_id.trim()) return;
      void getAgentActivity(pollingOwner.thread_id, pollingOwner.run_id)
        .then(setAgentActivity)
        .catch(() => undefined);
      
      void listPendingApprovals(pollingOwner)
        .then(setApprovals)
        .catch(() => undefined);
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

    return {
      ...agentActivity,
      agents: newAgents,
      total_request_count: newAgents.reduce((acc, a) => acc + a.request_count, 0),
      total_tokens: newAgents.reduce((acc, a) => acc + a.total_tokens, 0)
    };
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

  const handleRunTask = () => {
    if (!draft.owner.thread_id.trim() || !draft.task_prompt.trim() || autoApproveRiskThreshold === null || busy !== null) return;
    
    const userPrompt = draft.task_prompt;
    const userMessage: ChatMessage = {
      id: crypto.randomUUID(),
      role: "user",
      text: userPrompt,
      createdAt: new Date(),
    };
    
    setChatHistory(prev => [...prev, userMessage]);
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
        
        const activity = await getAgentActivity(result.thread_id, taskOwner.run_id);
        setAgentActivity(activity);
        
        const output = result.output && typeof result.output === "object" ? (result.output as Record<string, unknown>) : null;
        const status = typeof output?.status === "string" ? (output.status as string) : null;

        if (status === "failed" || result.error) {
          // Always surface one clear failure message — never swallow a failure
          // just because the run produced no agent text.
          let errorText = result.error ?? "Task execution failed.";
          const steps = output?.steps;
          if (Array.isArray(steps)) {
            const failedStep = steps.find((step) => {
              if (step === null || typeof step !== "object") return false;
              const stepStatus = (step as Record<string, unknown>).status;
              return stepStatus === "failed" || stepStatus === "blocked";
            }) as Record<string, unknown> | undefined;
            if (typeof failedStep?.summary === "string" && failedStep.summary.trim()) {
              errorText = failedStep.summary;
            } else if (typeof output?.summary === "string" && output.summary.trim()) {
              errorText = output.summary as string;
            }
          } else if (typeof output?.summary === "string" && output.summary.trim()) {
            errorText = output.summary as string;
          }
          setChatHistory(prev => [...prev, {
            id: crypto.randomUUID(),
            role: "agent",
            agentRole: "system",
            text: `Execution failed: ${errorText}`,
            createdAt: new Date(),
          }]);
        } else {
          // Success: show a human-readable run summary, not a raw JSON dump.
          const rawText = extractAgentResponseText(result.output);
          if (rawText) {
            const { text, thought, agentRole } = parseAgentResponse(rawText);
            setChatHistory(prev => [...prev, {
              id: crypto.randomUUID(),
              role: "agent",
              agentRole,
              text,
              thought,
              createdAt: new Date(),
            }]);
          }
        }
      } catch (error) {
        setChatHistory(prev => [...prev, {
          id: crypto.randomUUID(),
          role: "agent",
          agentRole: "system",
          text: error instanceof Error ? `System Error: ${error.message}` : "An unexpected system error occurred.",
          createdAt: new Date()
        }]);
        throw error;
      }
    });
  };

  const handleSwitchTextModel = (provider: TextModelProviderPreset) => {
    void run(`switch-text-${provider}`, async () => {
      const selection = await updateTextModelSelection({ provider });
      setTextModelSelection(selection);
      const nextText = await getTextModelHealth();
    });
  };


  const handleSwitchExecutionMode = (mode: ComputerUseStatus["execution_mode"]) => {
    if (!mode) return;
    void run(`switch-execution-mode-${mode}`, async () => {
      const selection = await updateExecutionModeSelection(mode);
      setExecutionMode(selection.execution_mode);
    });
  };

  const handleSwitchApproach = (approach: AgentApproach) => {
    void run(`switch-approach-${approach}`, async () => {
      const selection = await updateAgentApproachSelection(approach);
      setAgentApproach(selection.approach);
    });
  };

  const handleSwitchVisionRecovery = (enabled: boolean) => {
    void run(`switch-recovery-${enabled}`, async () => {
      const selection = await updateVisionRecoverySelection(enabled);
      setVisionRecovery(selection.enabled);
    });
  };

  const handleSwitchSupervisorSource = (source: SupervisorSource) => {
    void run(`switch-supervisor-${source}`, async () => {
      const selection = await updateSupervisorSource(source);
      setSupervisorSource(selection.source);
      setSupervisorApiConfigured(selection.api_configured);
      setSupervisorApiModelName(selection.api_model_name ?? null);
    });
  };

  return (
    <main className="bg-[#f0f2f5] dark:bg-[#0a0a0a] flex h-screen flex-col overflow-hidden relative">
      {/* Floating Approvals Popup Removed */}

      {/* Header */}
      <header className="border-b px-6 py-3.5 flex-none bg-card/70 backdrop-blur-xl z-40 shadow-sm">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="size-9 rounded-xl bg-gradient-to-br from-primary to-primary/70 flex items-center justify-center shadow-sm ring-1 ring-primary/20">
              <Bot className="size-5 text-primary-foreground" />
            </div>
            <div className="flex flex-col">
              <h1 className="text-[15px] font-semibold tracking-tight leading-none">VILAGENT</h1>
              <span className="text-[11px] text-muted-foreground leading-none mt-1">Computer-use operator</span>
            </div>
            {executionMode ? (
              <span className="ml-2 rounded-full border border-border/70 bg-muted/50 px-2.5 py-1 text-[11px] font-medium text-muted-foreground capitalize">
                {executionMode.replace("_", " ")}
              </span>
            ) : null}
          </div>
          <div className="flex items-center gap-2">
            <Button variant="outline" size="sm" onClick={() => setShowLogs(true)} className="text-xs h-8">
              <Terminal className="size-3.5 mr-1.5" /> Logs {logs.length > 0 && `(${logs.length})`}
            </Button>
          </div>
        </div>
      </header>

      {/* Main 2-Column Layout */}
      <div className="flex flex-1 overflow-hidden">
        
        {/* Left Column: Chat Box */}
        <section className="flex flex-1 flex-col relative overflow-hidden">
          
          <div className="flex-1 overflow-y-auto px-4 md:px-8 py-6 space-y-5">
             {chatHistory.length === 0 && approvals.length === 0 ? (
               <div className="flex flex-col items-center justify-center h-full text-center space-y-4 px-6">
                 <div className="size-16 rounded-2xl bg-gradient-to-br from-primary to-primary/60 flex items-center justify-center shadow-lg shadow-primary/20 ring-1 ring-primary/20">
                   <Bot className="size-8 text-primary-foreground" />
                 </div>
                 <div className="space-y-1.5">
                   <p className="text-[14px] font-semibold text-foreground/90">Ready to operate</p>
                   <p className="max-w-sm text-[12px] leading-relaxed text-muted-foreground/70">
                     Describe a task and VILAGENT will plan it, then drive the desktop and browser under your approval.
                   </p>
                 </div>
               </div>
             ) : (
               <div className="max-w-4xl mx-auto w-full space-y-6">
                 {chatHistory.map((msg) => (
                   <div key={msg.id} className={cn("flex w-full", msg.role === "user" ? "justify-end" : "justify-start")}>
                     {msg.role === "agent" && (
                       <div className="mr-3 flex-shrink-0 mt-1">
                         <div className="size-8 rounded-full bg-primary/10 flex items-center justify-center border border-primary/20">
                           {msg.agentRole === "vision" ? <ImageIcon className="size-4 text-primary" /> : 
                            msg.agentRole === "uia" ? <Pointer className="size-4 text-primary" /> : 
                            msg.agentRole === "system" ? <ShieldAlert className="size-4 text-destructive" /> : 
                            <Bot className="size-4 text-primary" />}
                         </div>
                       </div>
                     )}
                     
                     <div className={cn(
                       "group relative max-w-[85%] rounded-2xl px-4 py-2.5 shadow-sm sm:max-w-[78%]",
                       msg.role === "user"
                         ? "rounded-br-md bg-primary text-primary-foreground"
                         : "rounded-bl-md border border-border/60 bg-card"
                     )}>
                        {msg.role === "agent" && msg.agentRole && (
                          <div className="mb-1.5 flex items-center gap-1.5 font-mono text-[9px] font-semibold uppercase tracking-[0.12em] text-muted-foreground">
                            <span className="rounded bg-muted px-1.5 py-0.5 text-foreground/70">{msg.agentRole}</span>
                            <span className="font-normal tabular-nums opacity-50">{msg.createdAt.toLocaleTimeString()}</span>
                          </div>
                        )}

                        {msg.thought && (
                          <details className="mb-2 rounded-lg border border-border/60 bg-muted/30 text-muted-foreground">
                            <summary className="flex cursor-pointer list-none select-none items-center gap-1.5 rounded-lg px-2.5 py-1.5 font-mono text-[10px] font-semibold uppercase tracking-wide text-muted-foreground/80 transition-colors hover:bg-muted/50 [&::-webkit-details-marker]:hidden">
                              <BrainCircuit className="size-3 text-primary/70" /> Thinking
                            </summary>
                            <div className="whitespace-pre-wrap border-t border-border/50 px-2.5 pb-2.5 pt-2 font-mono text-[10.5px] leading-relaxed opacity-80">
                              {msg.thought}
                            </div>
                          </details>
                        )}

                        <div className="whitespace-pre-wrap text-[13px] leading-relaxed">
                          {msg.text}
                        </div>
                     </div>
                   </div>
                 ))}
                 
                 {approvals.map((approval) => (
                   <div key={approval.approval_id} className="flex w-full justify-start">
                     <div className="mr-3 flex-shrink-0 mt-1">
                       <div className="size-8 rounded-full bg-amber-500/10 flex items-center justify-center border border-amber-500/20">
                         <ShieldAlert className="size-4 text-amber-500" />
                       </div>
                     </div>
                     <div className="bg-card border border-amber-500/30 rounded-2xl rounded-bl-sm px-5 py-4 shadow-sm flex flex-col gap-3 min-w-[280px] w-full max-w-[85%] sm:max-w-[75%]">
                        <div className="flex items-center gap-2">
                          <span className="text-sm font-semibold text-amber-600 uppercase tracking-wider">
                            Approval Required
                          </span>
                        </div>
                        <div className="text-[14px] whitespace-pre-wrap leading-relaxed font-sans">
                          {approval.args?.type !== "plan_approval" && summarizeApproval(approval)}
                        </div>
                        
                        {/* Display Plan Details if it's a plan_approval */}
                        {approval.args?.type === "plan_approval" && approval.args?.plan_json && (
                          <div className="mt-2 p-3 bg-muted/40 rounded-lg border border-muted/50 text-sm">
                            <div className="font-semibold mb-2 text-foreground/80 flex items-center gap-2">
                              <Bot className="size-4" /> Proposed Plan
                            </div>
                            <ol className="list-decimal list-inside space-y-1.5 text-muted-foreground">
                              {parseApprovalPlanSteps(approval.args.plan_json).map((step, idx) => (
                                <li key={idx} className="leading-snug">
                                  <span className="font-medium text-foreground/70">{step.instruction || step.description || "Step"}</span>
                                  {step.requires_vision && <span className="ml-2 inline-flex items-center px-1.5 py-0.5 rounded text-[9px] font-medium bg-blue-500/10 text-blue-500 uppercase tracking-wider">Vision</span>}
                                  {step.risk?.level && (
                                    <span className="ml-2 inline-flex items-center px-1.5 py-0.5 rounded text-[9px] font-medium bg-amber-500/10 text-amber-600 uppercase tracking-wider">
                                      {step.risk.level === "critical" ? "Very High" : step.risk.level} risk
                                    </span>
                                  )}
                                </li>
                              ))}
                            </ol>
                          </div>
                        )}

                        <div className="flex gap-2 mt-2">
                          <ActionButton className="flex-1" busy={busy} variant="default" label={`approve-${approval.approval_id}`} onClick={() => void run(`approve-${approval.approval_id}`, async () => {
                              const decided = await approveAction(approval.approval_id, { owner: approval.owner, decided_by: "operator-ui", reason: "Approved in console." });
                              setApprovals(c => c.filter(i => i.approval_id !== decided.approval_id));
                          })}>{approval.args?.type === "plan_approval" ? "Approve Plan" : "Approve Action"}</ActionButton>
                          <ActionButton className="flex-1" busy={busy} variant="outline" label={`deny-${approval.approval_id}`} onClick={() => void run(`deny-${approval.approval_id}`, async () => {
                              const decided = await denyAction(approval.approval_id, { owner: approval.owner, decided_by: "operator-ui", reason: "Denied in console." });
                              setApprovals(c => c.filter(i => i.approval_id !== decided.approval_id));
                          })}>{approval.args?.type === "plan_approval" ? "Reject Plan" : "Deny Action"}</ActionButton>
                        </div>
                     </div>
                   </div>
                 ))}
                 
                 {busy === "run-computer-use-task" && (
                   <div className="flex w-full justify-start">
                     <div className="mr-3 flex-shrink-0 mt-1">
                       <div className="size-8 rounded-full bg-primary/10 flex items-center justify-center border border-primary/20">
                         <Bot className="size-4 text-primary" />
                       </div>
                     </div>
                     <div className="bg-card border border-border/50 rounded-2xl rounded-bl-sm px-5 py-4 shadow-sm flex flex-col gap-3 min-w-[280px]">
                        <div className="flex items-center gap-2">
                          <Loader2 className="size-4 animate-spin text-muted-foreground" />
                          <span className="text-sm text-foreground font-medium">
                            {agentActivity?.agents?.find(a => a.status === "running")?.last_event || "Agent is working..."}
                          </span>
                        </div>
                        {agentActivity?.agents?.find(a => a.status === "running")?.current_thought && (
                          <details className="rounded-lg bg-muted/40 border border-muted text-sm text-muted-foreground group">
                            <summary className="flex items-center gap-2 p-3 text-xs font-semibold text-muted-foreground/80 cursor-pointer list-none select-none hover:bg-muted/60 transition-colors rounded-lg">
                              <BrainCircuit className="size-3.5 group-open:text-primary transition-colors" /> Thinking Process
                            </summary>
                            <div className="px-3 pb-3 whitespace-pre-wrap font-mono text-[11px] leading-relaxed opacity-80 border-t border-muted/50 pt-3">
                              {agentActivity?.agents?.find(a => a.status === "running")?.current_thought}
                            </div>
                          </details>
                        )}
                     </div>
                   </div>
                 )}
               </div>
             )}
          </div>

          {/* Composer — pinned footer (does not overlap the message list) */}
          <div className="flex-none border-t border-border/60 bg-gradient-to-b from-transparent to-muted/20 px-4 py-3">
            <div className="mx-auto w-full max-w-4xl">
              <div className="group flex items-end gap-2 rounded-2xl border border-border/70 bg-card/90 p-2 shadow-lg ring-1 ring-transparent transition-all focus-within:border-primary/40 focus-within:ring-primary/25">
                <Textarea
                  value={draft.task_prompt}
                  onChange={(event) => patchDraft({ task_prompt: event.target.value })}
                  placeholder="Ask VILAGENT to do something…"
                  className="max-h-[180px] min-h-[44px] w-full resize-none border-0 bg-transparent px-3 py-2.5 text-[13px] leading-relaxed shadow-none focus-visible:ring-0"
                  onKeyDown={(e) => {
                     if (e.key === 'Enter' && !e.shiftKey) {
                         e.preventDefault();
                         handleRunTask();
                     }
                  }}
                />
                <ActionButton
                   busy={busy}
                   disabled={!draft.owner.thread_id.trim() || !draft.task_prompt.trim() || autoApproveRiskThreshold === null}
                   label="run-computer-use-task"
                   className="size-10 shrink-0 rounded-xl bg-primary p-0 text-primary-foreground shadow-md transition-transform hover:bg-primary/90 active:scale-95 disabled:opacity-40"
                   onClick={handleRunTask}
                >
                   <Play className="size-4 translate-x-px fill-current" />
                </ActionButton>
              </div>
              <p className="mt-1.5 text-center font-mono text-[10px] tracking-tight text-muted-foreground/70">
                Enter to send · Shift+Enter for a new line
              </p>
            </div>
          </div>
        </section>

        {/* Right Column: Plans & Activity */}
        <aside className="w-[320px] xl:w-[380px] flex-none border-l bg-card overflow-y-auto z-10 relative shadow-sm">
          <div className="p-5 space-y-8">
            
            {/* Plans */}
            <section className="space-y-4">
              <h2 className="flex items-center gap-2 font-mono text-[11px] font-semibold uppercase tracking-[0.14em] text-foreground/80">
                <LayoutList className="size-3.5 text-primary" /> Plan
              </h2>
              {(agentActivity?.plan_steps ?? []).length === 0 ? <EmptyText>No plans active.</EmptyText> : (
                <div className="space-y-3 relative before:absolute before:inset-y-0 before:left-2.5 before:w-px before:bg-border/60 pl-1">
                  {(agentActivity?.plan_steps ?? []).map((step) => {
                    const isCompleted = step.status === "completed";
                    const isRunning = step.status === "running";
                    const isError = step.status === "failed" || step.status === "blocked";
                    return (
                      <div key={step.step_id} className="flex gap-3.5 items-start relative">
                        <div className="bg-card py-0.5 z-10 mt-0.5 shrink-0">
                          {isCompleted ? (
                            <CheckCircle2 className="size-5 text-emerald-500 bg-card rounded-full" />
                          ) : isRunning ? (
                            <Loader2 className="size-5 text-blue-500 bg-card rounded-full animate-spin" />
                          ) : isError ? (
                            <XCircle className="size-5 text-destructive bg-card rounded-full" />
                          ) : (
                            <Circle className="size-5 text-muted-foreground/30 bg-card rounded-full" />
                          )}
                        </div>
                        <div className="pt-1 min-w-0 space-y-1">
                           <div className="flex items-start gap-1.5">
                             {step.requires_vision
                               ? <ImageIcon className="size-3.5 text-purple-500 shrink-0 mt-0.5" />
                               : <Pointer className="size-3.5 text-amber-500 shrink-0 mt-0.5" />}
                             <p className={cn("text-[13px] font-medium leading-relaxed break-words", isCompleted ? "text-muted-foreground line-through opacity-70" : isError ? "text-destructive" : "text-foreground/90")}>
                               {step.instruction}
                             </p>
                           </div>
                           <p className="text-[10px] leading-relaxed text-muted-foreground">Finish when: {step.completion_criteria}</p>
                           <p className="text-[10px] leading-relaxed text-muted-foreground">Action allowance: {step.max_actions ?? 2}</p>
                           {isError && <p className="text-[10px] leading-relaxed text-destructive">{step.error_code ?? step.summary ?? "Step failed"}</p>}
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}
              {/* Approvals */}
              {approvals.length > 0 && (
                <div className="mt-4 border-2 border-amber-500/50 rounded-xl p-3 bg-amber-500/5 animate-in fade-in slide-in-from-top-4">
                  <div className="flex items-center gap-2 text-amber-600 dark:text-amber-500 mb-2">
                    <ShieldAlert className="size-4" />
                    <h4 className="text-sm font-semibold">Approval Required</h4>
                  </div>
                  {approvals.map(approval => (
                     <div key={approval.approval_id} className="space-y-3">
                       <p className="text-xs text-muted-foreground line-clamp-2">{summarizeApproval(approval)}</p>
                       <p className="text-xs text-amber-600 font-medium mt-1">Please review in the chat window.</p>
                     </div>
                  ))}
                </div>
              )}
            </section>

            <Separator />

            {/* Model Configurations */}
            <section className="space-y-4">
              <h2 className="flex items-center gap-2 font-mono text-[11px] font-semibold uppercase tracking-[0.14em] text-foreground/80">
                <Settings className="size-3.5 text-primary" /> Configuration
              </h2>
              <div className="space-y-4 rounded-xl border bg-muted/20 p-3.5">
                <div className="space-y-2.5">
                  <div className="flex items-center justify-between gap-2">
                    <p className="text-[10px] font-semibold uppercase text-muted-foreground">Auto-Approve Risk</p>
                    <span className="text-[10px] font-medium text-muted-foreground">
                      {autoApproveRiskThreshold === null
                        ? "Selection required"
                        : `Through ${autoApproveRiskThreshold === "critical" ? "Very High" : autoApproveRiskThreshold}`}
                    </span>
                  </div>
                  <div className="relative grid grid-cols-4 gap-1 rounded-full bg-muted p-1">
                    {([
                      ["low", "Low"],
                      ["medium", "Medium"],
                      ["high", "High"],
                      ["critical", "Very High"],
                    ] as const).map(([level, label]) => (
                      <button
                        key={level}
                        type="button"
                        className={cn(
                          "relative z-10 rounded-full px-1 py-1.5 text-[9px] font-semibold transition-colors",
                          autoApproveRiskThreshold === level
                            ? "bg-primary text-primary-foreground shadow-sm"
                            : "text-muted-foreground hover:text-foreground",
                        )}
                        onClick={() => setAutoApproveRiskThreshold(level)}
                      >
                        {label}
                      </button>
                    ))}
                  </div>
                  <p className="text-[10px] leading-relaxed text-muted-foreground">
                    Steps through the selected level run automatically. Higher-risk steps require approval.
                  </p>
                </div>

                <div className="h-px bg-border" />

                <div className="space-y-2">
                  <p className="text-[10px] font-semibold uppercase text-muted-foreground">Text Planner</p>
                  <div className="grid grid-cols-2 gap-2">
                    {(textModelSelection?.options || ["gemini", "glm", "ollama", "fara"]).map((provider) => (
                      <ModeButton
                        key={provider}
                        active={textModelSelection?.provider === provider}
                        onClick={() => handleSwitchTextModel(provider as TextModelProviderPreset)}
                      >
                        {provider.toUpperCase()}
                      </ModeButton>
                    ))}
                  </div>
                </div>

                <div className="space-y-2">
                  <p className="text-[10px] font-semibold uppercase text-muted-foreground">Approach</p>
                  <div className="grid grid-cols-2 gap-2">
                    <ModeButton active={agentApproach === "plan_execute"} onClick={() => handleSwitchApproach("plan_execute")}>
                      Plan &amp; Execute
                    </ModeButton>
                    <ModeButton active={agentApproach === "autonomous"} onClick={() => handleSwitchApproach("autonomous")}>
                      Autonomous FARA
                    </ModeButton>
                  </div>
                  <p className="text-[10px] leading-snug text-muted-foreground/70">
                    Plan &amp; Execute breaks the task into steps for FARA. Autonomous lets the planner write one brief and FARA runs the whole task itself.
                  </p>
                </div>

                {agentApproach !== "autonomous" ? (
                  <div className="space-y-2">
                    <p className="text-[10px] font-semibold uppercase text-muted-foreground">Execution Mode</p>
                    <div className="grid grid-cols-2 gap-2">
                      <ModeButton active={executionMode === "hybrid"} onClick={() => handleSwitchExecutionMode("hybrid")}>
                        Normal
                      </ModeButton>
                      <ModeButton active={executionMode === "vision_only"} onClick={() => handleSwitchExecutionMode("vision_only")}>
                        Vision-Only
                      </ModeButton>
                    </div>
                  </div>
                ) : null}

                <div className="space-y-2">
                  <p className="text-[10px] font-semibold uppercase text-muted-foreground">Recovery Supervisor</p>
                  <div className="grid grid-cols-2 gap-2">
                    <ModeButton active={visionRecovery === false} onClick={() => handleSwitchVisionRecovery(false)}>
                      Off
                    </ModeButton>
                    <ModeButton active={visionRecovery === true} onClick={() => handleSwitchVisionRecovery(true)}>
                      Supervised
                    </ModeButton>
                  </div>
                  <p className="text-[10px] leading-snug text-muted-foreground/70">
                    When the vision model gets stuck, a stronger reasoning model steps in once to unblock it.
                  </p>

                  {visionRecovery ? (
                    <div className="space-y-2 pt-1">
                      <p className="text-[10px] font-semibold uppercase text-muted-foreground">Supervisor Model</p>
                      <div className="grid grid-cols-2 gap-2">
                        <ModeButton active={supervisorSource === "planner"} onClick={() => handleSwitchSupervisorSource("planner")}>
                          Planner
                        </ModeButton>
                        <ModeButton
                          active={supervisorSource === "api"}
                          disabled={!supervisorApiConfigured}
                          onClick={() => handleSwitchSupervisorSource("api")}
                        >
                          GLM-V API
                        </ModeButton>
                      </div>
                      <p className="text-[10px] leading-snug text-muted-foreground/70">
                        {supervisorApiConfigured
                          ? `GLM-V API model: ${supervisorApiModelName ?? "configured"}.`
                          : "Set VILAGENT_SUPERVISOR_API_KEY (Zhipu GLM-V) in env to enable the API option."}
                      </p>
                    </div>
                  ) : null}
                </div>
              </div>
            </section>

            <Separator />

            {/* Model activity — one panel per real model in the project */}
            <section className="space-y-3">
              <h2 className="flex items-center gap-2 font-mono text-[11px] font-semibold uppercase tracking-[0.14em] text-foreground/80">
                <Activity className="size-3.5 text-primary" /> Models
              </h2>
              <div className="space-y-2.5">
                <ModelPanel
                  icon={<BrainCircuit className="size-3.5 text-primary" />}
                  name="Planner LLM"
                  connection={(textModelSelection?.provider ?? status?.text_model?.provider ?? "—").toUpperCase()}
                  summary={plannerSummary}
                />
                <ModelPanel
                  icon={<ImageIcon className="size-3.5 text-purple-500" />}
                  name="FARA Vision"
                  connection="COLAB · PYNGROK"
                  summary={visionSummary}
                />
              </div>
            </section>

          </div>
        </aside>
      </div>

      {/* Raw logs panel — one tab per process, streamed from its file */}
      {showLogs && (
        <div className="absolute inset-0 z-50 flex justify-end bg-background/50 backdrop-blur-sm">
          <div className="flex h-full w-[560px] max-w-[92vw] animate-in slide-in-from-right-10 flex-col border-l bg-card shadow-2xl">
            <div className="flex items-center justify-between border-b bg-muted/20 px-4 py-3">
              <h2 className="flex items-center gap-2 font-mono text-[12px] font-semibold uppercase tracking-[0.12em]">
                <Terminal className="size-4 text-primary" /> Logs
              </h2>
              <div className="flex items-center gap-1">
                <Button variant="ghost" size="icon" className="size-7" onClick={() => loadLog(logSource)} title="Refresh">
                  <RefreshCcw className={cn("size-3.5", logLoading && "animate-spin")} />
                </Button>
                <Button variant="ghost" size="icon" className="size-7" onClick={() => setShowLogs(false)}>
                  <XCircle className="size-4" />
                </Button>
              </div>
            </div>
            <div className="flex items-center gap-1 border-b bg-muted/10 px-3 py-2">
              {(["backend", "frontend", "harness"] as const).map((source) => (
                <button
                  key={source}
                  type="button"
                  onClick={() => setLogSource(source)}
                  className={cn(
                    "rounded-md px-3 py-1 font-mono text-[10px] font-semibold uppercase tracking-wide transition-colors",
                    logSource === source ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:bg-muted/60 hover:text-foreground",
                  )}
                >
                  {source}
                </button>
              ))}
            </div>
            <div className="flex-1 overflow-auto bg-[#0b0d10]">
              <pre className="min-h-full whitespace-pre-wrap p-3 font-mono text-[10.5px] leading-relaxed text-zinc-300">
                {logLoading && !rawLog ? "Loading…" : (rawLog || "(empty)")}
              </pre>
            </div>
          </div>
        </div>
      )}
    </main>
  );
}

// Helper Components

function MetricCard({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg border border-border/60 bg-card px-2.5 py-1.5">
      <p className="mb-0.5 font-mono text-[9px] font-medium uppercase tracking-[0.1em] text-muted-foreground">{label}</p>
      <p className="font-mono text-[13px] font-semibold tabular-nums text-foreground/90">{value}</p>
    </div>
  );
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

function ModelPanel({
  icon,
  name,
  connection,
  summary,
}: {
  icon: ReactNode;
  name: string;
  connection: string;
  summary: ModelSummary;
}) {
  const isRunning = summary.status === "running";
  return (
    <div className={cn(
      "rounded-xl border bg-card/60 p-3 transition-colors",
      isRunning ? "border-primary/40 ring-1 ring-primary/15" : "border-border/60",
    )}>
      <div className="flex items-center justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2">
          <span className="flex size-6 shrink-0 items-center justify-center rounded-md bg-muted/60">{icon}</span>
          <div className="min-w-0">
            <p className="truncate text-[12px] font-semibold leading-tight text-foreground/90">{name}</p>
            <p className="truncate font-mono text-[9px] uppercase tracking-[0.1em] text-muted-foreground/70">{connection}</p>
          </div>
        </div>
        <span className={cn(
          "flex shrink-0 items-center gap-1 rounded-full px-2 py-0.5 font-mono text-[9px] font-semibold uppercase tracking-wide",
          isRunning ? "bg-blue-500/15 text-blue-600" :
          summary.status === "pending" ? "bg-amber-500/15 text-amber-600" :
          "bg-muted text-muted-foreground",
        )}>
          {isRunning ? <Loader2 className="size-2.5 animate-spin" /> : <Circle className="size-2 fill-current" />}
          {summary.status}
        </span>
      </div>
      <div className="mt-2.5 grid grid-cols-2 gap-1.5">
        <MetricCard label="Requests" value={summary.requests.toString()} />
        <MetricCard label="Tokens" value={summary.tokens.toLocaleString()} />
      </div>
      {isRunning && (summary.thinking || summary.lastEvent) && (
        <details className="mt-2 rounded-lg border border-border/60 bg-muted/30" open={Boolean(summary.thinking)}>
          <summary className="flex cursor-pointer list-none select-none items-center gap-1.5 px-2.5 py-1.5 font-mono text-[9px] font-semibold uppercase tracking-wide text-muted-foreground/80 [&::-webkit-details-marker]:hidden">
            <BrainCircuit className="size-3 animate-pulse text-primary/70" /> {summary.thinking ? "Thinking" : "Status"}
          </summary>
          <div className="whitespace-pre-wrap border-t border-border/50 px-2.5 pb-2 pt-1.5 font-mono text-[10px] leading-relaxed text-muted-foreground/90">
            {summary.thinking ?? summary.lastEvent}
          </div>
        </details>
      )}
    </div>
  );
}

function ActionButton({
  busy,
  children,
  disabled,
  label,
  onClick,
  className,
  variant = "secondary",
}: {
  busy: string | null;
  children: ReactNode;
  disabled?: boolean;
  label: string;
  onClick: () => void;
  className?: string;
  variant?: ComponentProps<typeof Button>["variant"];
}) {
  return (
    <Button
      disabled={disabled === true || busy === label}
      onClick={onClick}
      size="sm"
      type="button"
      variant={variant}
      className={className}
    >
      {children}
      {busy === label ? <Loader2 className="ml-2 size-3 animate-spin" /> : null}
    </Button>
  );
}

function ModeButton({
  active,
  children,
  onClick,
  disabled,
}: {
  active: boolean;
  children: ReactNode;
  onClick: () => void;
  disabled?: boolean;
}) {
  return (
    <button
      onClick={onClick}
      type="button"
      disabled={disabled}
      className={cn(
        "h-8 rounded-lg border px-3 text-[11px] font-medium tracking-tight transition-all",
        active
          ? "border-primary bg-primary text-primary-foreground shadow-sm shadow-primary/20"
          : "border-border/70 bg-card text-muted-foreground hover:border-primary/40 hover:bg-muted/50 hover:text-foreground",
        disabled && "cursor-not-allowed opacity-40 hover:border-border/70 hover:bg-card",
      )}
    >
      {children}
    </button>
  );
}

function EmptyText({ children }: { children: ReactNode }) {
  return <p className="rounded-lg border border-dashed border-border/60 bg-muted/20 p-3 text-center text-[12px] italic text-muted-foreground">{children}</p>;
}

function Separator() {
  return <div className="h-px w-full bg-border/40" />;
}

function parseAgentResponse(raw: string) {
  let thought: string | undefined = undefined;
  let text = raw;

  const thoughtMatch = /<(?:thought|thinking)>([\s\S]*?)<\/(?:thought|thinking)>/i.exec(raw);
  if (thoughtMatch) {
    thought = thoughtMatch[1]?.trim();
    text = text.replace(/<(?:thought|thinking)>[\s\S]*?<\/(?:thought|thinking)>/i, "").trim();
  }

  const lines = text.split(/\r?\n/);
  const planStart = lines.findIndex((line) => /^plan\s*[:：]?$/i.test(line) || /^plan\s*[:：]/i.test(line));
  
  if (planStart >= 0) {
    const before = lines.slice(0, planStart).join("\n").trim();
    const steps = extractPlannerSteps(raw);
    
    let additionalInfo = "";
    const statusMatch = /STATUS:\s*(.+)/i.exec(raw);
    if (statusMatch) {
       additionalInfo += `\n\n**Status:** ${statusMatch[1]}`;
    }
    const stepLines = lines.filter(l => /^-\s*[^:]+:/i.test(l));
    if (stepLines.length > 0) {
       additionalInfo += `\n**Execution Results:**\n` + stepLines.join("\n");
    }
    
    if (steps.length > 0) {
        text = (before ? before + "\n\n" : "") + steps.map((s, i) => `${i + 1}. ${s}`).join("\n") + additionalInfo;
    } else if (before) {
        text = before + additionalInfo;
    } else {
        text = "I am proceeding with the execution of your request." + additionalInfo;
    }
  }

  let agentRole: "planner" | "vision" | "uia" | "system" = "planner";
  if (planStart < 0) {
    if (raw.toLowerCase().includes("[vision]")) agentRole = "vision";
    else if (raw.toLowerCase().includes("[uia]")) agentRole = "uia";
  }

  return { text: text || "Done.", thought, agentRole };
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

  // Plan-execute returns a structured result ({status, plan, steps, summary}).
  // Show its human-readable summary rather than dumping the raw JSON into chat.
  const summary = record.summary;
  if (typeof summary === "string" && summary.trim()) return summary.trim();

  const status = record.status;
  if (typeof status === "string" && status.trim()) {
    return `Task ${status}.`;
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

function extractPlannerSteps(text: string | null): string[] {
  if (text === null) return [];
  const lines = text
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);
  const planStart = lines.findIndex((line) => /^plan\s*[:：]?$/i.test(line) || /^plan\s*[:：]/i.test(line));
  const steps: string[] = [];
  for (let i = planStart + 1; i < lines.length; i++) {
     const line = lines[i];
     if (!line) continue;
     if (/^(status|replans|requests_estimate|metadata)[:：]/i.test(line) || /^-\s*s\d+:/i.test(line) || /^s\d+:/i.test(line)) {
        break;
     }
     const clean = line.replace(/^[-*]\s+/, "").replace(/^\d+[.)]\s+/, "").replace(/^\[[ xX]\]\s+/, "").trim();
     if (clean.length > 0 && !/^done\b/i.test(clean) && !/^error\b/i.test(clean)) {
        steps.push(clean);
     }
  }
  return steps.slice(0, 8);
}
