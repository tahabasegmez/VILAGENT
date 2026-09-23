"use client";

import { Brain, Cpu, Globe, Plug, Terminal, X } from "lucide-react";
import type { ReactNode } from "react";

export type SettingsWindow = "connections" | "memory" | "logs" | "models" | "browser";

const TILES: { id: SettingsWindow; title: string; text: string; icon: ReactNode }[] = [
  { id: "connections", title: "Connections", text: "Your model endpoints: interface, address, model and key, kept encrypted.", icon: <Plug className="size-4" /> },
  { id: "models", title: "Models", text: "Which connection plays each role, its limits, and a check per role.", icon: <Cpu className="size-4" /> },
  { id: "browser", title: "Browser", text: "Which browser and profile a browser task drives.", icon: <Globe className="size-4" /> },
  { id: "memory", title: "Memory", text: "Remembered runs, lessons and your own notes.", icon: <Brain className="size-4" /> },
  { id: "logs", title: "Logs", text: "Agent, gateway and UI logs.", icon: <Terminal className="size-4" /> },
];

/** The settings popup: a tile per tool, each opening its own terminal-style window. */
export function SettingsModal({ onOpen, onClose }: { onOpen: (window: SettingsWindow) => void; onClose: () => void }) {
  return (
    <div className="fixed inset-0 z-[70] grid place-items-center bg-black/40 p-4 backdrop-blur-[2px] duration-200 animate-in fade-in" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <div role="dialog" aria-label="Settings" className="hud-glass w-[min(560px,94vw)] rounded-2xl bg-[#0b0614]/90 p-5 duration-200 animate-in zoom-in-95">
        <div className="mb-4 flex items-center justify-between">
          <h2 className="font-mono text-[10.5px] font-semibold uppercase tracking-[0.3em] text-fuchsia-100/90">Settings</h2>
          <button type="button" title="Close" onClick={onClose} className="grid size-6 place-items-center rounded-md text-zinc-400 transition-colors hover:bg-fuchsia-500/15 hover:text-fuchsia-100">
            <X className="size-3.5" />
          </button>
        </div>
        <div className="grid grid-cols-2 gap-3">
          {TILES.map((tile) => (
            <button key={tile.id} type="button" onClick={() => onOpen(tile.id)} className="hud-hover group rounded-xl border border-fuchsia-400/20 bg-black/30 p-3.5 text-left">
              <span className="mb-2 grid size-8 place-items-center rounded-lg border border-fuchsia-400/30 bg-fuchsia-500/10 text-fuchsia-200 group-hover:text-sky-200">{tile.icon}</span>
              <p className="font-mono text-[11px] font-semibold text-zinc-100">{tile.title}</p>
              <p className="mt-0.5 text-[10px] leading-snug text-zinc-500">{tile.text}</p>
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}
