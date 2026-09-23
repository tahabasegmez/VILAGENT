"use client";

import { X } from "lucide-react";
import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

/** A centred, terminal-style window (config.yaml, memory, logs, models) with its own close button. */
export function TerminalWindow({ title, icon, actions, onClose, children, className }: { title: string; icon: ReactNode; actions?: ReactNode; onClose: () => void; children: ReactNode; className?: string }) {
  return (
    <div className="fixed inset-0 z-[80] grid place-items-center bg-black/45 p-4 backdrop-blur-[2px] duration-200 animate-in fade-in" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <div
        role="dialog"
        aria-label={title}
        className={cn("hud-glass flex h-[min(640px,84vh)] w-[min(780px,94vw)] flex-col overflow-hidden rounded-xl bg-[#07040c]/92 duration-200 animate-in zoom-in-95", className)}
      >
        <div className="flex flex-none items-center gap-2 border-b border-fuchsia-400/15 bg-black/30 px-3 py-2">
          <span className="text-fuchsia-300">{icon}</span>
          <h2 className="flex-1 font-mono text-[10.5px] font-semibold uppercase tracking-[0.18em] text-fuchsia-100/90">
            <span className="text-sky-300/80">~/vilagent $</span> {title}
          </h2>
          {actions}
          <button type="button" title="Close" onClick={onClose} className="grid size-6 place-items-center rounded-md text-zinc-400 transition-colors hover:bg-fuchsia-500/15 hover:text-fuchsia-100">
            <X className="size-3.5" />
          </button>
        </div>
        {children}
      </div>
    </div>
  );
}

/** A small icon button for a window's title bar. */
export function BarButton({ title, onClick, disabled, children }: { title: string; onClick: () => void; disabled?: boolean; children: ReactNode }) {
  return (
    <button type="button" title={title} onClick={onClick} disabled={disabled} className="grid size-6 place-items-center rounded-md text-zinc-400 transition-colors hover:bg-fuchsia-500/15 hover:text-fuchsia-100 disabled:opacity-30">
      {children}
    </button>
  );
}
