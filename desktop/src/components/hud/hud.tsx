"use client";

import { AlertTriangle, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { createPortal } from "react-dom";

import { ApprovalDialog } from "@/components/hud/approval-dialog";
import { Backdrop } from "@/components/hud/backdrop";
import { BrowserWindow } from "@/components/hud/browser-window";
import { ConnectionsWindow } from "@/components/hud/connections-window";
import { InterruptedCard } from "@/components/hud/interrupted-card";
import { LiveGraph, type FocusRequest } from "@/components/hud/live-graph";
import { Logo } from "@/components/hud/logo";
import { LogsDrawer } from "@/components/hud/logs-drawer";
import { MemoryPanel } from "@/components/hud/memory-panel";
import { FLOAT_HEIGHT, FLOAT_WIDTH, MiniHud, useFloatingWindow } from "@/components/hud/mini-hud";
import { ModelsWindow } from "@/components/hud/models-window";
import { OptionsDrawer } from "@/components/hud/options-drawer";
import { PlanRail } from "@/components/hud/plan-rail";
import { PromptOrb } from "@/components/hud/prompt-orb";
import { SettingsModal, type SettingsWindow } from "@/components/hud/settings-modal";
import { TaskHeader } from "@/components/hud/task-header";
import { UsagePills } from "@/components/hud/usage-pills";
import { useOperator } from "@/components/hud/use-operator";
import { activeSpan } from "@/core/computer-use";

/** The operator's mission-control screen: the live run graph with floating panels around it. */
export function OperatorHud() {
  const operator = useOperator();
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [window_, setWindow] = useState<SettingsWindow | null>(null);
  const [memoryRun, setMemoryRun] = useState<string | null>(null);
  const [focus, setFocus] = useState<FocusRequest>(null);
  const floating = useFloatingWindow({ running: operator.isRunning, acting: operator.acting, outcome: operator.outcome });

  const active = useMemo(() => activeSpan(operator.trace), [operator.trace]);
  // Memory recalled for this run, and whether a running node is reading some right now.
  const memory = useMemo(() => {
    const spans = operator.trace.order.map((id) => operator.trace.spans[id]);
    const recalled = spans.find((span) => span?.name === "recall")?.memory.length ?? 0;
    return { items: recalled, reading: spans.some((span) => span?.status === "running" && span.memory.length > 0) };
  }, [operator.trace]);

  useEffect(() => floating.resize(FLOAT_HEIGHT + operator.approvals.length * 200), [floating, operator.approvals.length]);

  const openWindow = (next: SettingsWindow) => {
    if (next === "memory") setMemoryRun(null);
    setWindow(next);
  };
  const approvals = <ApprovalDialog approvals={operator.approvals} onAnswer={operator.answer} />;
  const mini = <MiniHud active={active} steps={operator.planSteps} thinking={operator.thinking} outcome={operator.outcome} onRestore={floating.restore} onStop={operator.stop} />;

  return (
    <main className="hud-root relative h-screen overflow-hidden text-zinc-200">
      <Backdrop />
      <LiveGraph trace={operator.trace} focus={focus} />

      <Logo />
      <TaskHeader
        prompt={operator.taskPrompt}
        running={operator.isRunning}
        outcome={operator.outcome}
        result={operator.result}
        lastRun={operator.lastRun}
        onRate={operator.rate}
        onShowMemory={(runId) => {
          setMemoryRun(runId);
          setWindow("memory");
        }}
      />
      <UsagePills vlm={operator.usage.vlm} llm={operator.usage.llm} />
      <PlanRail steps={operator.planSteps} memory={memory} onFocus={(stepId) => setFocus({ stepId, at: Date.now() })} />
      <OptionsDrawer settings={operator.settings} onOpenSettings={() => setSettingsOpen(true)} />

      {operator.notice && (
        <div role="alert" className="hud-glass fixed bottom-24 left-1/2 z-50 flex w-[min(460px,92vw)] -translate-x-1/2 items-start gap-2 rounded-xl border-rose-400/50 px-3 py-2 text-[11px] text-rose-100 duration-300 animate-in fade-in slide-in-from-bottom-2">
          <AlertTriangle className="mt-px size-3.5 flex-none text-rose-300" />
          <p className="flex-1">{operator.notice}</p>
          <button type="button" title="Dismiss" onClick={operator.dismissNotice} className="text-rose-200/70 hover:text-white">
            <X className="size-3.5" />
          </button>
        </div>
      )}
      {operator.interrupted && !operator.isRunning && <InterruptedCard run={operator.interrupted} onResume={operator.resume} onDiscard={operator.discard} />}
      <PromptOrb
        value={operator.draft.task_prompt}
        onChange={(task_prompt) => operator.patchDraft({ task_prompt })}
        onSend={() => operator.send(floating.open)}
        onStop={operator.stop}
        running={operator.isRunning}
        startOpen={!operator.isRunning}
      />

      {settingsOpen && <SettingsModal onOpen={openWindow} onClose={() => setSettingsOpen(false)} />}
      {window_ === "connections" && <ConnectionsWindow settings={operator.settings} onClose={() => setWindow(null)} />}
      {window_ === "logs" && <LogsDrawer onClose={() => setWindow(null)} />}
      {window_ === "memory" && <MemoryPanel runId={memoryRun} onClose={() => setWindow(null)} />}
      {window_ === "models" && <ModelsWindow settings={operator.settings} onClose={() => setWindow(null)} />}
      {window_ === "browser" && <BrowserWindow onClose={() => setWindow(null)} />}

      {/* While FARA acts, the run moves to a small always-on-top window (or an overlay when
          no window could be opened) so the agent sees the desktop, not this screen. */}
      {floating.window
        ? createPortal(
            <>
              {mini}
              {approvals}
            </>,
            floating.window.document.body,
          )
        : floating.floating && (
            <div style={{ width: FLOAT_WIDTH, height: FLOAT_HEIGHT }} className="hud-glass fixed right-4 top-16 z-[45] overflow-hidden rounded-2xl bg-[#0a0612]/95 opacity-85 duration-300 animate-in fade-in slide-in-from-right-4">
              {mini}
            </div>
          )}
      {!floating.window && approvals}
      {floating.floating && floating.window && (
        <div className="absolute inset-0 z-[65] grid place-items-center bg-[#0a0612]/85 backdrop-blur-sm">
          <div className="flex flex-col items-center gap-3 text-center">
            <p className="font-mono text-[10px] uppercase tracking-[0.3em] text-fuchsia-200/70">Running in the mini window</p>
            <button type="button" onClick={floating.restore} className="hud-glass hud-hover rounded-lg px-4 py-1.5 text-[11px] text-fuchsia-100">
              Bring back the full view
            </button>
          </div>
        </div>
      )}
    </main>
  );
}
