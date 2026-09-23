"use client";

import { Loader2, Pencil, Plug, Plus, Trash2, X } from "lucide-react";
import { useState } from "react";

import { TerminalWindow } from "@/components/hud/terminal-window";
import { Select } from "@/components/hud/ui-bits";
import type { Settings } from "@/components/hud/use-operator";
import type { ApiConnection, InterfaceParam, ModelRoles } from "@/core/computer-use";
import { cn } from "@/lib/utils";

type Draft = { id: string; name: string; kind: string; interface: string; params: Record<string, string> };

/** The saved endpoints. Adding or editing one happens in its own popup. */
export function ConnectionsWindow({ settings, onClose }: { settings: Settings; onClose: () => void }) {
  const roles = settings.roles;
  const [draft, setDraft] = useState<Draft | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const open = (item: ApiConnection | null) => {
    setError(null);
    setDraft(
      item
        ? { id: item.id, name: item.name, kind: item.kind, interface: item.interface, params: { ...item.params } }
        : { id: "", name: "", kind: "llm", interface: firstInterface(roles, "llm"), params: {} },
    );
  };

  const run = (work: Promise<unknown>) => {
    setBusy(true);
    setError(null);
    work
      .then(() => setDraft(null))
      .catch((caught: unknown) => setError(caught instanceof Error ? caught.message : "Not saved."))
      .finally(() => setBusy(false));
  };

  return (
    <>
      <TerminalWindow title="connections" icon={<Plug className="size-3.5" />} onClose={onClose} className="h-auto max-h-[86vh]">
        <div className="space-y-2 overflow-y-auto p-4">
          <div className="flex items-center gap-2">
            <p className="min-w-0 flex-1 text-[10px] text-zinc-500">Every model the app uses. Keys stay encrypted on this machine.</p>
            <button
              type="button"
              onClick={() => open(null)}
              className="hud-hover flex h-6 flex-none items-center gap-1 rounded-md border border-fuchsia-400/30 bg-fuchsia-500/10 px-2 font-mono text-[9.5px] uppercase tracking-[0.16em] text-fuchsia-100"
            >
              <Plus className="size-3" /> Add
            </button>
          </div>

          {roles === null ? (
            <p className="flex items-center gap-2 text-[11px] text-zinc-400">
              <Loader2 className="size-3.5 animate-spin" /> Reading the connections…
            </p>
          ) : roles.connections.length === 0 ? (
            <p className="rounded-lg border border-dashed border-white/10 p-3 text-center text-[10.5px] italic text-zinc-500">
              Nothing yet. A run needs an LLM and a computer-use model.
            </p>
          ) : (
            <ul className="space-y-1">
              {roles.connections.map((item) => (
                <li key={item.id} className="flex items-center gap-2 rounded-lg border border-white/[0.07] bg-white/[0.02] px-2 py-1.5">
                  <span className="w-24 flex-none font-mono text-[9px] uppercase tracking-[0.16em] text-fuchsia-200/70">{item.kind.replace("_", " ")}</span>
                  <span className="w-32 flex-none truncate text-[10.5px] text-zinc-200">{item.name}</span>
                  <span className="min-w-0 flex-1 truncate font-mono text-[9.5px] text-zinc-500">
                    {item.params.model}
                    {item.params.base_url ? ` · ${item.params.base_url}` : ""}
                  </span>
                  <span className={cn("flex-none text-[9.5px]", item.secrets_set.length > 0 ? "text-emerald-300/80" : "text-zinc-600")}>{item.secrets_set.length > 0 ? "key saved" : "no key"}</span>
                  <button type="button" onClick={() => open(item)} title={`Configure ${item.name}`} className="hud-hover flex-none rounded-md border border-white/10 p-1 text-zinc-400">
                    <Pencil className="size-3" />
                  </button>
                  <button
                    type="button"
                    onClick={() => run(settings.onDeleteConnection(item.id))}
                    disabled={busy}
                    title={`Forget ${item.name}`}
                    className="hud-hover flex-none rounded-md border border-white/10 p-1 text-zinc-400 disabled:opacity-30"
                  >
                    <Trash2 className="size-3" />
                  </button>
                </li>
              ))}
            </ul>
          )}

          {error && <p className="text-[10.5px] text-rose-300">{error}</p>}
        </div>
      </TerminalWindow>

      {draft !== null && roles !== null && (
        <EditorDialog
          roles={roles}
          draft={draft}
          setDraft={setDraft}
          busy={busy}
          error={error}
          onClose={() => setDraft(null)}
          onSave={() => run(settings.onSaveConnection({ name: draft.name, kind: draft.kind, interface: draft.interface, params: draft.params }, draft.id))}
        />
      )}
    </>
  );
}

function firstInterface(roles: ModelRoles | null, kind: string): string {
  return roles?.interfaces.find((item) => item.kinds.includes(kind))?.path ?? "";
}

/** Add or configure one connection: what it is, then the interface's own arguments. */
function EditorDialog({
  roles,
  draft,
  setDraft,
  busy,
  error,
  onClose,
  onSave,
}: {
  roles: ModelRoles;
  draft: Draft;
  setDraft: (draft: Draft) => void;
  busy: boolean;
  error: string | null;
  onClose: () => void;
  onSave: () => void;
}) {
  const offered = roles.interfaces.filter((item) => item.kinds.includes(draft.kind));
  const chosen = roles.interfaces.find((item) => item.path === draft.interface);
  const saved = roles.connections.find((item) => item.id === draft.id);
  const stored = (param: InterfaceParam) => param.type === "secret" && (saved?.secrets_set.includes(param.name) ?? false);
  const missing = (param: InterfaceParam) => param.required && !draft.params[param.name]?.trim() && !stored(param);
  const ready = draft.name.trim() !== "" && chosen !== undefined && !chosen.params.some(missing);

  const set = (name: string, value: string) => setDraft({ ...draft, params: { ...draft.params, [name]: value } });
  const required = chosen?.params.filter((param) => param.required || param.type === "secret") ?? [];
  const optional = chosen?.params.filter((param) => !param.required && param.type !== "secret") ?? [];

  return (
    <div
      className="fixed inset-0 z-[80] grid place-items-center bg-black/50 p-4 backdrop-blur-[2px] duration-200 animate-in fade-in"
      onMouseDown={(event) => event.target === event.currentTarget && onClose()}
    >
      <div role="dialog" aria-label="Connection" className="hud-glass w-[min(680px,94vw)] rounded-2xl bg-[#0b0614]/95 p-4 duration-200 animate-in zoom-in-95">
        <div className="mb-3 flex items-center justify-between">
          <h2 className="font-mono text-[10.5px] font-semibold uppercase tracking-[0.3em] text-fuchsia-100/90">{draft.id ? "Configure connection" : "New connection"}</h2>
          <button type="button" title="Close" onClick={onClose} className="grid size-6 place-items-center rounded-md text-zinc-400 transition-colors hover:bg-fuchsia-500/15 hover:text-fuchsia-100">
            <X className="size-3.5" />
          </button>
        </div>

        <div className="grid grid-cols-3 gap-2">
          <Labelled label="Type">
            <Select
              label="Type"
              value={draft.kind}
              options={roles.kinds.map((kind) => ({ value: kind.id, label: kind.label }))}
              onChange={(kind) => setDraft({ ...draft, kind, interface: firstInterface(roles, kind), params: {} })}
            />
          </Labelled>
          <Labelled label="Provider">
            <Select label="Provider" value={draft.interface} options={offered.map((item) => ({ value: item.path, label: item.label }))} onChange={(path) => setDraft({ ...draft, interface: path, params: {} })} />
          </Labelled>
          <Labelled label="Nickname">
            <Input value={draft.name} placeholder="My GLM" onChange={(name) => setDraft({ ...draft, name })} />
          </Labelled>
        </div>

        <div className="mt-3 grid grid-cols-3 gap-2">
          {required.map((param) => (
            <Labelled key={param.name} label={param.name} required={param.required}>
              <Field param={param} value={draft.params[param.name] ?? ""} stored={stored(param)} onChange={(value) => set(param.name, value)} />
            </Labelled>
          ))}
        </div>

        {optional.length > 0 && (
          <div className="mt-3 rounded-lg border border-white/[0.07] bg-white/[0.02] p-2">
            <div className="grid grid-cols-4 gap-2">
              {optional.map((param) => (
                <Labelled key={param.name} label={param.name}>
                  <Field param={param} value={draft.params[param.name] ?? ""} stored={false} onChange={(value) => set(param.name, value)} />
                </Labelled>
              ))}
            </div>
          </div>
        )}

        {error && <p className="mt-2 text-[10.5px] text-rose-300">{error}</p>}

        <div className="mt-3 flex items-center gap-2">
          <button
            type="button"
            disabled={!ready || busy}
            onClick={onSave}
            className="hud-hover h-7 rounded-md border border-fuchsia-400/40 bg-fuchsia-500/15 px-3 text-[11px] text-fuchsia-100 disabled:opacity-30"
          >
            {busy ? "Saving…" : draft.id ? "Save" : "Add"}
          </button>
          <button type="button" onClick={onClose} className="hud-hover h-7 rounded-md border border-white/10 px-3 text-[11px] text-zinc-300">
            Cancel
          </button>
          <span className="ml-auto font-mono text-[9px] text-zinc-600">{chosen?.path}</span>
        </div>
      </div>
    </div>
  );
}

function Labelled({ label, required, children }: { label: string; required?: boolean; children: React.ReactNode }) {
  return (
    <label className="block min-w-0">
      <span className="mb-1 block font-mono text-[9px] uppercase tracking-[0.14em] text-zinc-500">
        {label}
        {required && <span className="text-fuchsia-300"> *</span>}
      </span>
      {children}
    </label>
  );
}

/** One argument, typed as its LangChain parameter is. */
function Field({ param, value, stored, onChange }: { param: InterfaceParam; value: string; stored: boolean; onChange: (value: string) => void }) {
  if (param.type === "boolean") {
    return (
      <Select label={param.name} value={value || param.default} options={[{ value: "false", label: "false" }, { value: "true", label: "true" }]} onChange={onChange} />
    );
  }
  const secret = param.type === "secret";
  const number = param.type === "number" || param.type === "integer";
  return (
    <Input
      value={value}
      placeholder={secret && stored ? "stored — type to replace" : param.default || param.hint || (param.required ? "required" : "optional")}
      type={secret ? "password" : number ? "number" : "text"}
      step={param.type === "number" ? "0.1" : undefined}
      onChange={onChange}
    />
  );
}

function Input({ value, placeholder, type = "text", step, onChange }: { value: string; placeholder: string; type?: string; step?: string; onChange: (value: string) => void }) {
  return (
    <input
      value={value}
      placeholder={placeholder}
      type={type}
      step={step}
      spellCheck={false}
      onChange={(event) => onChange(event.target.value)}
      className="h-7 w-full min-w-0 rounded-lg border border-white/10 bg-[#120b1a] px-2 font-mono text-[10.5px] text-zinc-200 outline-none transition-colors hover:border-fuchsia-400/30 focus:border-fuchsia-400/50"
    />
  );
}
