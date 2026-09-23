"use client";

import { BrainCircuit, Check, Loader2, Maximize2, Square, XCircle } from "lucide-react";
import { useCallback, useEffect, useRef, useState, type CSSProperties } from "react";

import type { LiveThinking, RunOutcome } from "@/components/hud/use-operator";
import type { PlanStepActivityItem, TraceSpan } from "@/core/computer-use";
import { cn } from "@/lib/utils";

export const FLOAT_WIDTH = 360;
const FLOAT_FRAME = "vilagent-floating";

export const FLOAT_HEIGHT = 300;

/**
 * A real, separate always-on-top OS window that shows the run while the agent works.
 *
 * It must be opened inside the send gesture: opening a window needs a transient user
 * activation, so an automatic open after planning would be rejected. Electron's Chromium
 * does not implement Document Picture-in-Picture cleanly (it tries to open an `about:`
 * target and Windows offers the Store), so `window.open` is used there; in a plain
 * browser the PiP API is preferred, and if both are blocked the caller falls back to an
 * in-view overlay.
 */
export function useFloatingWindow({ running, acting, outcome }: { running: boolean; acting: boolean; outcome: RunOutcome }) {
  const [floating, setFloating] = useState(false);
  const [dismissed, setDismissed] = useState(false);
  const [window_, setWindow] = useState<Window | null>(null);
  const windowRef = useRef<Window | null>(null);

  const decorate = useCallback((win: Window) => {
    // Copy the app's styles into the new document so Tailwind works there too.
    for (const sheet of Array.from(document.styleSheets)) {
      try {
        const css = Array.from(sheet.cssRules).map((rule) => rule.cssText).join("");
        const style = win.document.createElement("style");
        style.textContent = css;
        win.document.head.appendChild(style);
      } catch {
        const href = sheet.href;
        if (href) {
          const link = win.document.createElement("link");
          link.rel = "stylesheet";
          link.href = href;
          win.document.head.appendChild(link);
        }
      }
    }
    win.document.title = "VILAGENT";
    win.document.documentElement.classList.add("dark");
    win.document.body.style.margin = "0";
    win.document.body.style.background = "#0a0612";
    win.addEventListener("pagehide", () => {
      windowRef.current = null;
      setWindow(null);
      setFloating(false);
      setDismissed(true);
    });
    windowRef.current = win;
    setWindow(win);
  }, []);

  const open = useCallback(() => {
    if (windowRef.current) return;
    const isElectron = typeof navigator !== "undefined" && /electron/i.test(navigator.userAgent);
    const pip = !isElectron && typeof window !== "undefined"
      ? (window as unknown as { documentPictureInPicture?: { requestWindow: (options: { width: number; height: number }) => Promise<Window> } }).documentPictureInPicture
      : undefined;

    const openPopup = () => {
      try {
        const popup = window.open("", FLOAT_FRAME, `popup=yes,width=${FLOAT_WIDTH},height=${FLOAT_HEIGHT}`);
        if (popup) decorate(popup);
      } catch {
        // Blocked: the in-view overlay takes over once the agent starts acting.
      }
    };

    if (pip) pip.requestWindow({ width: FLOAT_WIDTH, height: FLOAT_HEIGHT }).then(decorate).catch(openPopup);
    else openPopup();
  }, [decorate]);

  const close = useCallback(() => {
    try {
      windowRef.current?.close();
    } catch {
      // Already closed.
    }
    windowRef.current = null;
    setWindow(null);
  }, []);

  // Show the panel once the agent is actually driving the screen (not while planning);
  // when the run ends, linger on the Done/Failed badge before tearing it down.
  useEffect(() => {
    if (!running) {
      if (windowRef.current && outcome) {
        const timer = setTimeout(() => {
          setFloating(false);
          setDismissed(false);
          close();
        }, 2000);
        return () => clearTimeout(timer);
      }
      setFloating(false);
      setDismissed(false);
      close();
      return;
    }
    if (acting && !dismissed) setFloating(true);
  }, [running, acting, dismissed, outcome, close]);

  const restore = useCallback(() => {
    setFloating(false);
    setDismissed(true);
    close();
  }, [close]);

  const resize = useCallback((height: number) => {
    try {
      window_?.resizeTo(FLOAT_WIDTH, height);
    } catch {
      // Not resizable.
    }
  }, [window_]);

  return { floating, window: window_, open, restore, resize };
}

/** The run while FARA acts: what is running, the current step, the model's last note, and stop. */
export function MiniHud({
  active,
  steps,
  thinking,
  outcome,
  onRestore,
  onStop,
}: {
  active: TraceSpan | null;
  steps: PlanStepActivityItem[];
  thinking: LiveThinking;
  outcome: RunOutcome;
  onRestore: () => void;
  onStop: () => void;
}) {
  const index = steps.findIndex((step) => step.status === "running");
  const step = steps[index];
  return (
    <div className="hud-root relative flex h-full flex-col text-zinc-200">
      {outcome && (
        <div className="absolute inset-0 z-20 flex flex-col items-center justify-center gap-3 bg-[#0a0612]/95 backdrop-blur-sm duration-300 animate-in fade-in">
          <div
            className={cn(
              "grid size-14 place-items-center rounded-full ring-1 duration-500 animate-in zoom-in-50",
              outcome === "success" ? "bg-emerald-500/10 text-emerald-300 ring-emerald-400/50 shadow-[0_0_40px_-6px_rgba(52,211,153,0.7)]" : "bg-rose-500/10 text-rose-300 ring-rose-400/50 shadow-[0_0_40px_-6px_rgba(251,59,107,0.7)]",
            )}
          >
            {outcome === "success" ? <Check className="size-7" /> : <XCircle className="size-7" />}
          </div>
          <p className={cn("font-mono text-[11px] uppercase tracking-[0.3em]", outcome === "success" ? "text-emerald-300" : "text-rose-300")}>{outcome === "success" ? "Done" : "Failed"}</p>
        </div>
      )}
      {/* The header doubles as the drag handle of the frameless window. */}
      <div className="flex items-center justify-between border-b border-fuchsia-400/15 px-3 py-2" style={{ WebkitAppRegion: "drag" } as CSSProperties}>
        <div className="flex items-center gap-2">
          <Loader2 className="size-3 animate-spin text-fuchsia-300" />
          <span className="font-mono text-[9.5px] font-semibold uppercase tracking-[0.3em] text-fuchsia-100/90">Vilagent · live</span>
        </div>
        <button type="button" title="Back to the full view" onClick={onRestore} style={{ WebkitAppRegion: "no-drag" } as CSSProperties} className="grid size-6 place-items-center rounded-md text-zinc-400 transition-colors hover:bg-fuchsia-500/15 hover:text-fuchsia-100">
          <Maximize2 className="size-3" />
        </button>
      </div>
      <div className="flex-1 space-y-2.5 overflow-hidden px-3 py-2.5">
        <div className="hud-running relative rounded-lg border border-fuchsia-400/60 bg-[#1a0b2b]/80 px-2.5 py-1.5">
          <p className="font-mono text-[8.5px] uppercase tracking-[0.24em] text-zinc-500">Now</p>
          <p className="truncate font-mono text-[11px] font-semibold text-fuchsia-100">{active ? (active.kind === "node" ? active.name : (active.label ?? active.name)) : "starting"}</p>
          {active?.output && <p className="truncate font-mono text-[10px] text-zinc-400">{active.output}</p>}
        </div>
        {step && (
          <div>
            <p className="font-mono text-[8.5px] uppercase tracking-[0.24em] text-zinc-500">{steps.length > 1 ? `Step ${index + 1} of ${steps.length}` : "Task"}</p>
            <p className="line-clamp-2 text-[11px] leading-snug text-zinc-200">{step.instruction}</p>
          </div>
        )}
        {thinking.thought && (
          <p key={thinking.thought} className="flex items-start gap-1.5 text-[10.5px] italic leading-relaxed text-violet-300/80 duration-300 animate-in fade-in">
            <BrainCircuit className="mt-0.5 size-3 shrink-0 text-fuchsia-400/70" />
            <span className="shimmer line-clamp-3">{thinking.thought}</span>
          </p>
        )}
      </div>
      <div className="border-t border-fuchsia-400/15 p-2" style={{ WebkitAppRegion: "no-drag" } as CSSProperties}>
        <button type="button" onClick={onStop} className="flex w-full items-center justify-center gap-1.5 rounded-lg border border-rose-400/60 bg-rose-500/15 py-1.5 font-mono text-[10px] font-semibold uppercase tracking-[0.2em] text-rose-100 shadow-[0_0_18px_-6px_rgba(251,59,107,0.9)] transition-transform hover:bg-rose-500/25 active:scale-[0.98]">
          <Square className="size-3 fill-current" /> Stop
        </button>
      </div>
    </div>
  );
}
