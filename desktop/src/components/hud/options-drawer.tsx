"use client";

import { Settings2, SlidersHorizontal, X } from "lucide-react";
import { useState, type ReactNode } from "react";

import type { Settings } from "@/components/hud/use-operator";
import { cn } from "@/lib/utils";

/** The ⋯ button on the right edge and the options panel it slides in. */
export function OptionsDrawer({ settings, onOpenSettings }: { settings: Settings; onOpenSettings: () => void }) {
  const [open, setOpen] = useState(false);
  // Brief and Direct both hand the whole task to FARA, so the per-step settings do not apply.
  const single = settings.approach === "brief" || settings.approach === "direct";
  const approachHint = {
    plan: "The planner splits the task into steps.",
    brief: "The planner writes one brief and FARA runs the whole task.",
    direct: "Pure vision: FARA gets your words as they are, with no planner call at all.",
  }[settings.approach ?? "plan"];

  return (
    <>
      <button
        type="button"
        title="Options"
        onClick={() => setOpen(true)}
        className={cn("hud-glass hud-hover fixed right-0 top-1/2 z-40 grid h-12 w-7 -translate-y-1/2 place-items-center rounded-l-xl border-r-0 text-fuchsia-200 transition-opacity", open && "pointer-events-none opacity-0")}
      >
        <SlidersHorizontal className="size-3.5" />
      </button>

      <aside
        aria-hidden={!open}
        className={cn(
          "hud-glass fixed right-3 top-1/2 z-50 w-[288px] -translate-y-1/2 rounded-2xl bg-[#0c0616]/80 p-4 transition-all duration-300 ease-out",
          open ? "translate-x-0 opacity-100" : "pointer-events-none translate-x-[110%] opacity-0",
        )}
      >
        <div className="mb-3 flex items-center justify-between">
          <h2 className="font-mono text-[10px] font-semibold uppercase tracking-[0.28em] text-fuchsia-100/90">Options</h2>
          <button type="button" title="Close" onClick={() => setOpen(false)} className="grid size-6 place-items-center rounded-md text-zinc-400 transition-colors hover:bg-fuchsia-500/15 hover:text-fuchsia-100">
            <X className="size-3.5" />
          </button>
        </div>

        <div className="space-y-3">
          <Row label="Approach" hint={approachHint}>
            <Segmented
              value={settings.approach}
              onChange={settings.onApproach}
              options={[
                { value: "plan", label: "Plan" },
                { value: "brief", label: "Brief" },
                { value: "direct", label: "Direct" },
              ]}
            />
          </Row>
          <Row label="Execution" hint={single ? "Plan mode only: FARA drives the whole task here." : undefined}>
            <Segmented value={settings.executionMode} disabled={single} onChange={settings.onExecutionMode} options={[{ value: "hybrid", label: "Hybrid" }, { value: "vision_only", label: "Only vision" }]} />
          </Row>
          <Row label="Memory">
            <Segmented value={settings.memory} onChange={settings.onMemory} options={[{ value: true, label: "On" }, { value: false, label: "Off" }]} />
          </Row>
          <Row label="Supervising" hint="A supervisor model helps FARA when it gets stuck.">
            <Segmented value={settings.recovery} onChange={settings.onRecovery} options={[{ value: true, label: "On" }, { value: false, label: "Off" }]} />
          </Row>
          <Row label="Step check" hint={single ? "Plan mode only: the run ends when FARA says it is done." : undefined}>
            <Segmented
              value={settings.verifier}
              disabled={single}
              onChange={settings.onVerifier}
              options={[
                { value: "fara", label: "Vision" },
                { value: "supervisor", label: "Supervisor" },
                { value: "none", label: "Off" },
              ]}
            />
            {settings.verifier === "supervisor" && !settings.supervisorSeesImages && <p className="mt-1 text-[10px] text-amber-300/85">The supervisor model can’t read screenshots, so checks will fail. Pick a vision model in Settings → Models.</p>}
          </Row>
          <Row label="Ask first" hint="Steps at this planned risk or above wait for your OK. Risky actions always ask.">
            <Segmented
              value={settings.approvalThreshold}
              onChange={settings.onApprovalThreshold}
              options={[
                { value: "off", label: "Never" },
                { value: "critical", label: "Critical" },
                { value: "high", label: "High+" },
                { value: "medium", label: "Med+" },
              ]}
            />
          </Row>
        </div>

        <button
          type="button"
          onClick={onOpenSettings}
          className="hud-hover mt-4 flex h-8 w-full items-center justify-center gap-1.5 rounded-lg border border-fuchsia-400/30 bg-fuchsia-500/10 font-mono text-[10px] uppercase tracking-[0.2em] text-fuchsia-100"
        >
          <Settings2 className="size-3.5" /> Settings
        </button>
      </aside>
    </>
  );
}

function Row({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <div>
      <p className="mb-1 font-mono text-[9px] font-semibold uppercase tracking-[0.2em] text-zinc-500">{label}</p>
      {children}
      {hint && <p className="mt-1 text-[9.5px] leading-snug text-zinc-600">{hint}</p>}
    </div>
  );
}

/** A segmented toggle whose highlight slides to the chosen option. */
export function Segmented<T extends string | boolean>({ value, options, onChange, disabled }: { value: T | null; options: { value: T; label: string }[]; onChange: (value: T) => void; disabled?: boolean }) {
  const index = options.findIndex((option) => option.value === value);
  return (
    <div className={cn("relative grid rounded-lg border border-white/10 bg-black/30 p-0.5", disabled && "opacity-40")} style={{ gridTemplateColumns: `repeat(${options.length}, minmax(0, 1fr))` }}>
      {index >= 0 && (
        <span
          aria-hidden
          className="absolute inset-y-0.5 rounded-md bg-gradient-to-br from-fuchsia-500/85 to-violet-600/85 shadow-[0_0_16px_-4px_rgba(217,70,239,0.9)] transition-[left] duration-300 ease-out"
          style={{ width: `calc((100% - 4px) / ${options.length})`, left: `calc(2px + (100% - 4px) * ${index} / ${options.length})` }}
        />
      )}
      {options.map((option) => (
        <button
          key={String(option.value)}
          type="button"
          disabled={disabled}
          onClick={() => option.value !== value && onChange(option.value)}
          className={cn("relative z-10 h-6 truncate px-1 text-[10.5px] font-medium transition-colors", option.value === value ? "text-white" : "text-zinc-400 hover:text-fuchsia-100", disabled && "cursor-not-allowed")}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}
