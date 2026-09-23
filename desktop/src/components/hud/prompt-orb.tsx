"use client";

import { ChevronsDown, Plus, Send, Square } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { cn } from "@/lib/utils";

/**
 * The task box at the bottom: a glass bar that folds into an orb. It folds itself after a
 * send; while a run is going the orb is the stop button.
 */
export function PromptOrb({ value, onChange, onSend, onStop, running, startOpen }: { value: string; onChange: (value: string) => void; onSend: () => void; onStop: () => void; running: boolean; startOpen: boolean }) {
  const [open, setOpen] = useState(startOpen);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const canSend = Boolean(value.trim()) && !running;

  useEffect(() => {
    if (open) inputRef.current?.focus();
  }, [open]);

  // Grow with the text, up to a few lines.
  useEffect(() => {
    const input = inputRef.current;
    if (!input) return;
    input.style.height = "0px";
    input.style.height = `${Math.min(input.scrollHeight, 120)}px`;
  }, [value, open]);

  const send = () => {
    if (!canSend) return;
    onSend();
    setOpen(false);
  };

  if (running && !open) {
    return (
      <div className="fixed bottom-6 left-1/2 z-40 -translate-x-1/2">
        <button
          type="button"
          title="Stop the run"
          onClick={onStop}
          className="relative grid size-12 place-items-center rounded-full border border-rose-400/60 bg-rose-500/15 text-rose-100 shadow-[0_0_26px_-4px_rgba(251,59,107,0.85)] backdrop-blur-md transition-transform hover:scale-105 active:scale-95"
        >
          <span className="absolute inset-0 animate-ping rounded-full border border-rose-400/40" />
          <Square className="size-4 fill-current" />
        </button>
      </div>
    );
  }

  return (
    <div className="fixed bottom-6 left-1/2 z-40 -translate-x-1/2">
      <div
        className={cn(
          "hud-glass flex items-end overflow-hidden bg-[#0d0618]/70 transition-[width,border-radius,box-shadow,border-color] duration-300 ease-out",
          open ? "w-[min(640px,92vw)] gap-2 rounded-2xl p-2 pl-3.5 focus-within:border-fuchsia-400/70 focus-within:shadow-[0_0_28px_-4px_rgba(217,70,239,0.75)]" : "w-12 rounded-full",
        )}
      >
        {open ? (
          <>
            <textarea
              ref={inputRef}
              rows={1}
              value={value}
              onChange={(event) => onChange(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault();
                  send();
                }
                if (event.key === "Escape") setOpen(false);
              }}
              placeholder={running ? "A task is running…" : "Describe a task for the agent…"}
              aria-label="Task"
              className="max-h-[120px] min-h-8 flex-1 resize-none bg-transparent py-1.5 text-[12px] leading-relaxed text-zinc-100 outline-none placeholder:text-zinc-500"
            />
            {running ? (
              <OrbButton title="Stop the run" onClick={onStop} className="border-rose-400/60 bg-rose-500/20 text-rose-100">
                <Square className="size-3 fill-current" />
              </OrbButton>
            ) : (
              <OrbButton title="Send (Enter)" onClick={send} disabled={!canSend} className="border-fuchsia-400/60 bg-gradient-to-br from-fuchsia-500/80 to-violet-600/80 text-white">
                <Send className="size-3.5" />
              </OrbButton>
            )}
            <OrbButton title="Fold" onClick={() => setOpen(false)} className="border-white/10 text-zinc-400 hover:text-fuchsia-100">
              <ChevronsDown className="size-3.5" />
            </OrbButton>
          </>
        ) : (
          <button type="button" title="New task" onClick={() => setOpen(true)} className="grid size-12 place-items-center text-fuchsia-100 transition-transform hover:scale-110">
            <Plus className="size-5 drop-shadow-[0_0_6px_rgba(217,70,239,0.9)]" />
          </button>
        )}
      </div>
    </div>
  );
}

function OrbButton({ title, onClick, disabled, className, children }: { title: string; onClick: () => void; disabled?: boolean; className: string; children: React.ReactNode }) {
  return (
    <button type="button" title={title} onClick={onClick} disabled={disabled} className={cn("grid size-8 flex-none place-items-center rounded-xl border transition-all hover:scale-105 disabled:scale-100 disabled:opacity-30", className)}>
      {children}
    </button>
  );
}
