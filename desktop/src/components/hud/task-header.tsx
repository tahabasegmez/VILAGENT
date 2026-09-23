"use client";

import { Brain, ChevronDown, ThumbsDown, ThumbsUp } from "lucide-react";
import { useState } from "react";

import { RichText } from "@/components/hud/ui-bits";
import type { RunOutcome, RunResult } from "@/components/hud/use-operator";
import type { MemoryRating } from "@/core/computer-use";
import { cn } from "@/lib/utils";

type LastRun = { run_id: string; used: number; rating: MemoryRating } | null;

/** The task at the top ("Start"), and once the run ends, how it went. */
export function TaskHeader({
  prompt,
  running,
  outcome,
  result,
  lastRun,
  onRate,
  onShowMemory,
}: {
  prompt: string | null;
  running: boolean;
  outcome: RunOutcome;
  result: RunResult | null;
  lastRun: LastRun;
  onRate: (rating: MemoryRating) => void;
  onShowMemory: (runId: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const lamp = running ? "bg-fuchsia-400 shadow-[0_0_10px_2px_rgba(217,70,239,0.8)] animate-pulse" : outcome === "success" ? "bg-emerald-400" : outcome === "failed" ? "bg-rose-500" : "bg-zinc-600";

  return (
    <header className="fixed left-1/2 top-4 z-40 w-[min(560px,calc(100vw-500px))] min-w-[260px] -translate-x-1/2">
      <div className="hud-glass rounded-xl px-3.5 py-2">
        <div className="flex items-center gap-2">
          <span className={cn("size-1.5 flex-none rounded-full", lamp)} />
          <span className="font-mono text-[9px] font-semibold uppercase tracking-[0.3em] text-sky-300/80">Start</span>
          <p className={cn("min-w-0 flex-1 text-[11.5px] text-zinc-100", !open && "truncate")} title={prompt ?? undefined}>
            {prompt ?? <span className="text-zinc-500">No task yet. Open the prompt below to start one.</span>}
          </p>
          {result && (
            <button type="button" onClick={() => setOpen((value) => !value)} title={open ? "Hide the report" : "Show the report"} className="text-zinc-500 transition-colors hover:text-fuchsia-200">
              <ChevronDown className={cn("size-3.5 transition-transform", open && "rotate-180")} />
            </button>
          )}
        </div>

        {result && !running && (
          <div className="mt-1.5 flex items-center gap-2 border-t border-white/5 pt-1.5 text-[10.5px]">
            <span className={cn("rounded-full px-1.5 py-px font-mono text-[9px] font-semibold uppercase tracking-wider", outcome === "success" ? "bg-emerald-500/15 text-emerald-300" : "bg-rose-500/15 text-rose-300")}>{result.status}</span>
            <span className="min-w-0 flex-1 truncate text-zinc-400">{firstLine(result.text)}</span>
            {lastRun && (
              <>
                <button type="button" onClick={() => onShowMemory(lastRun.run_id)} className="flex items-center gap-1 text-zinc-400 transition-colors hover:text-sky-200">
                  <Brain className="size-3" /> {lastRun.used === 1 ? "1 memory" : `${lastRun.used} memories`}
                </button>
                <button type="button" title="Good example" onClick={() => onRate(lastRun.rating === "good" ? null : "good")} className={lastRun.rating === "good" ? "text-emerald-300" : "text-zinc-500 hover:text-emerald-300"}>
                  <ThumbsUp className="size-3" />
                </button>
                <button type="button" title="Bad example: never reuse it" onClick={() => onRate(lastRun.rating === "bad" ? null : "bad")} className={lastRun.rating === "bad" ? "text-rose-300" : "text-zinc-500 hover:text-rose-300"}>
                  <ThumbsDown className="size-3" />
                </button>
              </>
            )}
          </div>
        )}

        {open && result && (
          <div className="mt-2 max-h-[40vh] overflow-y-auto border-t border-white/5 pt-2 text-zinc-300 duration-200 animate-in fade-in">
            <RichText text={result.text} />
          </div>
        )}
      </div>
    </header>
  );
}

function firstLine(text: string): string {
  return (
    text
      .split("\n")
      .map((line) => line.replace(/[*_]/g, "").trim())
      .find(Boolean) ?? ""
  );
}
