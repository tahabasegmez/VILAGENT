"use client";

import { RotateCcw } from "lucide-react";

export type InterruptedRun = {
  run_id: string;
  prompt: string;
  /** The step that was running when the app stopped; it starts over on resume. */
  step: string | null;
};

/** The last task was cut off (the app closed or crashed): continue it or let it go. */
export function InterruptedCard({ run, onResume, onDiscard }: { run: InterruptedRun; onResume: () => void; onDiscard: () => void }) {
  return (
    <div role="alert" className="hud-glass fixed bottom-24 left-1/2 z-40 w-[min(460px,92vw)] -translate-x-1/2 rounded-xl border-sky-400/40 bg-[#07101c]/80 p-3 text-[11px] text-sky-50 duration-300 animate-in fade-in slide-in-from-bottom-2">
      <div className="flex items-center gap-2 font-semibold">
        <RotateCcw className="size-4 text-sky-300" /> The last task was interrupted
      </div>
      <p className="mt-1.5 break-words text-zinc-100">{run.prompt}</p>
      <p className="mt-1 text-[11px] text-sky-100/80">
        {run.step ? (
          <>
            The step <span className="italic">“{run.step}”</span> was interrupted and will run again from its start. Completed steps are kept.
          </>
        ) : (
          "It continues from where it stopped. Completed steps are kept."
        )}{" "}
        Check that the screen is ready first.
      </p>
      <div className="mt-2.5 flex gap-1.5">
        <button type="button" onClick={onResume} className="h-7 rounded-lg border border-sky-400/40 bg-sky-500/20 px-2.5 text-[11px] font-medium text-sky-50 transition-colors hover:bg-sky-500/30">
          Resume
        </button>
        <button type="button" onClick={onDiscard} className="h-7 rounded-lg border border-white/10 bg-white/[0.03] px-2.5 text-[11px] font-medium text-zinc-300 transition-colors hover:bg-white/10">
          Discard
        </button>
      </div>
    </div>
  );
}
