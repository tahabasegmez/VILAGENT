"use client";

import { Brain, Check, Circle, Loader2, ScrollText, X } from "lucide-react";

import type { PlanStepActivityItem } from "@/core/computer-use";
import { cn } from "@/lib/utils";

// The one-step approaches: the brief FARA was given, or the operator's own words (Direct).
const SINGLE_STEPS: Record<string, string> = { brief: "Brief", direct: "Direct" };

/** The plan beside the graph (or the single step of a brief or direct run); a step click shows it on the graph. */
export function PlanRail({ steps, memory, onFocus }: { steps: PlanStepActivityItem[]; memory: { items: number; reading: boolean }; onFocus: (stepId: string) => void }) {
  if (steps.length === 0) return null;
  const single = steps.length === 1 ? SINGLE_STEPS[steps[0]!.step_id] : undefined;
  return (
    <aside className="hud-glass fixed left-4 top-1/2 z-30 flex max-h-[62vh] w-[236px] -translate-y-1/2 flex-col overflow-hidden rounded-xl duration-300 animate-in fade-in slide-in-from-left-3">
      <div className="flex flex-none items-center gap-1.5 border-b border-fuchsia-400/15 px-3 py-2">
        <ScrollText className="size-3 text-fuchsia-300" />
        <span className="flex-1 font-mono text-[9.5px] font-semibold uppercase tracking-[0.24em] text-fuchsia-100/85">{single ?? `Plan · ${steps.length}`}</span>
        {memory.items > 0 && (
          <span title={memory.reading ? "Memory is being read" : "Memory recalled for this run"} className={cn("flex items-center gap-1 rounded-full border border-sky-400/40 px-1.5 font-mono text-[8.5px] text-sky-200", memory.reading && "hud-memory-reading")}>
            <Brain className="size-2.5" /> {memory.items}
          </span>
        )}
      </div>

      {single ? (
        <button type="button" onClick={() => onFocus(steps[0]!.step_id)} className="overflow-y-auto p-3 text-left text-[11px] leading-relaxed text-zinc-200 hover:text-white">
          <StepMark status={steps[0]!.status} />
          <span className="ml-1.5">{steps[0]!.instruction}</span>
        </button>
      ) : (
        <ol className="space-y-0.5 overflow-y-auto p-1.5">
          {steps.map((step, index) => {
            const running = step.status === "running";
            const failed = ["failed", "blocked", "denied"].includes(step.status);
            return (
              <li key={step.step_id}>
                <button
                  type="button"
                  onClick={() => onFocus(step.step_id)}
                  className={cn(
                    "flex w-full items-start gap-2 rounded-md px-2 py-1.5 text-left transition-colors hover:bg-fuchsia-500/10",
                    running && "bg-fuchsia-500/12 shadow-[inset_2px_0_0_#d946ef,0_0_16px_-8px_rgba(217,70,239,0.9)]",
                  )}
                >
                  <span className="mt-px font-mono text-[9px] text-zinc-600">{String(index + 1).padStart(2, "0")}</span>
                  <StepMark status={step.status} />
                  <span className={cn("line-clamp-2 flex-1 text-[10.5px] leading-snug", running ? "text-white" : step.status === "completed" ? "text-zinc-500" : failed ? "text-rose-300" : "text-zinc-300")}>{step.instruction}</span>
                </button>
              </li>
            );
          })}
        </ol>
      )}
    </aside>
  );
}

function StepMark({ status }: { status: string }) {
  if (status === "running") return <Loader2 className="mt-0.5 inline size-3 flex-none animate-spin text-fuchsia-300" />;
  if (status === "completed") return <Check className="mt-0.5 inline size-3 flex-none text-emerald-300/80" />;
  if (["failed", "blocked", "denied"].includes(status)) return <X className="mt-0.5 inline size-3 flex-none text-rose-400" />;
  return <Circle className="mt-1 inline size-2 flex-none text-zinc-600" />;
}
