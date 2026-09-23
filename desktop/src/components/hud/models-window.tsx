"use client";

import { CheckCircle2, Cpu, Loader2, PlugZap, XCircle } from "lucide-react";
import { useState } from "react";

import { TerminalWindow } from "@/components/hud/terminal-window";
import { Select } from "@/components/hud/ui-bits";
import type { Settings } from "@/components/hud/use-operator";
import type { ConnectionCheck, ModelRole, ModelRoles } from "@/core/computer-use";
import { cn } from "@/lib/utils";

const SAME_AS_PLANNER = "planner";
const NOTHING = "";

type Card = { role: ModelRole; title: string; about: string; extra?: { value: string; label: string } };

/** Each role and what it is for. What a model does is set on its connection, not here. */
const CARDS: Card[] = [
  { role: "planner", title: "Planner", about: "Turns your task into steps, writes the brief, and reads a finished run back. An LLM or a VLM." },
  {
    role: "supervisor",
    title: "Supervisor",
    about: "Helps when the computer-use model is stuck, and can check a finished step. A VLM, because it is shown screenshots.",
    extra: { value: SAME_AS_PLANNER, label: "Same as the planner" },
  },
  { role: "vision", title: "Computer use", about: "Looks at the screen and acts on it (FARA)." },
  { role: "embeddings", title: "Memory search", about: "Finds earlier runs by meaning. Off, memory still finds them by keyword.", extra: { value: NOTHING, label: "Keywords only" } },
];

/** Which connection plays each role, with a check per role. */
export function ModelsWindow({ settings, onClose }: { settings: Settings; onClose: () => void }) {
  const roles = settings.roles;
  return (
    <TerminalWindow title="models" icon={<Cpu className="size-3.5" />} onClose={onClose} className="h-auto max-h-[86vh]">
      <div className="space-y-2.5 overflow-y-auto p-4">
        {roles ? (
          <>
            {CARDS.map((card) => (
              <RoleCard key={card.role} card={card} roles={roles} settings={settings} />
            ))}
            <p className="text-[9.5px] leading-snug text-zinc-600">Add the endpoints themselves, and everything about how they answer, in Settings → Connections.</p>
          </>
        ) : (
          <p className="flex items-center gap-2 text-[11px] text-zinc-400">
            <Loader2 className="size-3.5 animate-spin" /> Reading the models…
          </p>
        )}
      </div>
    </TerminalWindow>
  );
}

function RoleCard({ card, roles, settings }: { card: Card; roles: ModelRoles; settings: Settings }) {
  const saved = roles.roles[card.role];
  const where = roles.where[card.role];
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<ConnectionCheck | null>(null);
  const [error, setError] = useState<string | null>(null);

  const wanted = roles.role_kinds[card.role];
  const usable = roles.connections.filter((item) => wanted.includes(item.kind));
  const offered = [...(card.extra ? [card.extra] : []), ...usable.map((item) => ({ value: item.id, label: `${item.name} · ${item.params.model ?? ""}` }))];
  // A select shows its first option when the stored value matches none of them, which would look
  // like a choice nobody made — and picking that same option fires nothing. So say it is unset.
  const options = offered.some((option) => option.value === saved.connection)
    ? offered
    : [{ value: saved.connection, label: usable.length > 0 ? "Not set — pick one" : `No ${wanted.join(" or ")} connection yet` }, ...offered];

  const save = (connection: string) => {
    if (connection === saved.connection) return;
    setBusy(true);
    setError(null);
    setResult(null);
    settings
      .onSelectRole(card.role, { connection })
      .catch((caught: unknown) => setError(caught instanceof Error ? caught.message : "Not saved."))
      .finally(() => setBusy(false));
  };

  const check = () => {
    setBusy(true);
    setError(null);
    settings
      .onCheckRole(card.role)
      .then(setResult)
      .catch((caught: unknown) => setError(caught instanceof Error ? caught.message : "The check could not run."))
      .finally(() => setBusy(false));
  };

  return (
    <section className="rounded-xl border border-white/10 bg-black/20 p-3">
      <div className="mb-2 flex items-center gap-2">
        <h3 className="w-28 flex-none font-mono text-[10px] font-semibold uppercase tracking-[0.2em] text-fuchsia-100/90">{card.title}</h3>
        <div className="w-64 flex-none">
          <Select label={`${card.title} connection`} value={saved.connection} options={options} onChange={save} />
        </div>
        <span className="min-w-0 flex-1 truncate font-mono text-[9.5px] text-zinc-500">
          {where.model ?? "—"} · {where.where}
        </span>
        <button
          type="button"
          onClick={check}
          disabled={busy}
          className="hud-hover flex h-6 flex-none items-center gap-1 rounded-md border border-sky-400/30 bg-sky-500/10 px-2 font-mono text-[9.5px] uppercase tracking-[0.16em] text-sky-100 disabled:opacity-40"
        >
          {busy ? <Loader2 className="size-3 animate-spin" /> : <PlugZap className="size-3" />} Check
        </button>
      </div>

      <p className="text-[9.5px] leading-snug text-zinc-600">{card.about}</p>
      {error && <p className="mt-1.5 text-[10.5px] text-rose-300">{error}</p>}
      {result && <Result check={result} />}
    </section>
  );
}

function Result({ check }: { check: ConnectionCheck }) {
  return (
    <div className={cn("mt-1.5 rounded-lg border px-2.5 py-1.5 font-mono text-[10px]", check.ok ? "border-emerald-500/25 bg-emerald-500/[0.06]" : "border-rose-500/30 bg-rose-500/[0.07]")}>
      <div className="flex items-center gap-1.5">
        {check.ok ? <CheckCircle2 className="size-3 flex-none text-emerald-400" /> : <XCircle className="size-3 flex-none text-rose-400" />}
        <span className="min-w-0 flex-1 truncate text-zinc-300">
          {check.model ?? check.name} · {check.where}
        </span>
        {check.latency_ms != null && <span className="flex-none text-zinc-500">{check.latency_ms} ms</span>}
      </div>
      <p className={cn("mt-0.5 break-words", check.ok ? "text-zinc-500" : "text-rose-300")}>{check.detail}</p>
    </div>
  );
}
