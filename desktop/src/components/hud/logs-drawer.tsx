"use client";

import { RefreshCcw, Terminal, Trash2 } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { BarButton, TerminalWindow } from "@/components/hud/terminal-window";
import { clearLog, getLog } from "@/core/computer-use";
import type { LogSource } from "@/core/computer-use";
import { cn } from "@/lib/utils";

const SOURCES: LogSource[] = ["agent", "gateway", "ui"];

export function LogsDrawer({ onClose }: { onClose: () => void }) {
  const [source, setSource] = useState<LogSource>("agent");
  const [text, setText] = useState("");
  const [loading, setLoading] = useState(false);

  const load = useCallback((which: LogSource) => {
    setLoading(true);
    getLog(which)
      .then(setText)
      .catch((error: unknown) => setText(`Failed to load the ${which} log: ${String(error)}`))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => load(source), [source, load]);

  return (
    <TerminalWindow
      title="logs"
      icon={<Terminal className="size-3.5" />}
      onClose={onClose}
      actions={
        <BarButton title="Refresh" onClick={() => load(source)}>
          <RefreshCcw className={cn("size-3.5", loading && "animate-spin")} />
        </BarButton>
      }
    >
        <div className="flex items-center gap-1 border-b border-white/8 px-3 py-2">
          {SOURCES.map((option) => (
            <div key={option} className={cn(
              "flex items-center rounded-md font-mono text-[10px] font-semibold uppercase tracking-wide transition-colors",
              source === option ? "bg-gradient-to-br from-fuchsia-500 to-violet-600 text-white shadow-[0_0_14px_-4px_rgba(192,132,252,0.9)]" : "text-zinc-400 hover:bg-white/5 hover:text-fuchsia-200",
            )}>
              <button type="button" onClick={() => setSource(option)} className="py-1 pl-3 pr-1.5">{option}</button>
              <button
                type="button"
                title={`Clear the ${option} log`}
                onClick={() => void clearLog(option).then(() => load(option)).catch(() => undefined)}
                className={cn("grid place-items-center rounded-md py-1 pl-0.5 pr-2 transition-opacity", source === option ? "text-white/80 hover:text-white" : "text-zinc-500 hover:text-red-300")}
              >
                <Trash2 className="size-3" />
              </button>
            </div>
          ))}
        </div>
        <div className="flex-1 overflow-auto bg-black/40">
          <pre className="min-h-full whitespace-pre-wrap p-3 font-mono text-[10.5px] leading-relaxed text-zinc-400">
            {loading && !text ? "Loading…" : text || "(empty)"}
          </pre>
        </div>
    </TerminalWindow>
  );
}
