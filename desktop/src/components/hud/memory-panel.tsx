"use client";

import { Brain, Check, Pencil, Plus, RefreshCw, Search, ThumbsDown, ThumbsUp, Trash2, XCircle } from "lucide-react";
import { type ReactNode, useCallback, useEffect, useMemo, useState } from "react";

import { BarButton, TerminalWindow } from "@/components/hud/terminal-window";
import { addNote, clearMemory, deleteEpisode, deleteLesson, editLesson, embedMemory, listEpisodes, listLessons, rateEpisode, runMemory, searchMemory } from "@/core/computer-use";
import type { MemoryEntries, MemoryEpisode, MemoryKeyType, MemoryLesson } from "@/core/computer-use";
import { cn } from "@/lib/utils";

type Tab = "runs" | "lessons" | "used";

const EMPTY: MemoryEntries = { episodes: [], lessons: [] };

/** Browse, search, rate and edit what the agent remembers; add your own notes. */
export function MemoryPanel({ runId, onClose }: { runId: string | null; onClose: () => void }) {
  const [tab, setTab] = useState<Tab>(runId ? "used" : "runs");
  const [query, setQuery] = useState("");
  const [entries, setEntries] = useState<MemoryEntries>(EMPTY);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setError(null);
      if (tab === "used" && runId) setEntries(await runMemory(runId));
      else if (query.trim()) setEntries(await searchMemory(query.trim()));
      else setEntries({ episodes: await listEpisodes(), lessons: await listLessons() });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Memory could not be loaded.");
    }
  }, [tab, runId, query]);

  useEffect(() => {
    void load();
  }, [load]);

  // Every change reloads, so the list always shows what is stored.
  const act = (change: () => Promise<unknown>) => void change().then(load).catch((caught: unknown) => setError(caught instanceof Error ? caught.message : "That did not work."));

  const forgetAll = () => {
    if (window.confirm("Forget every remembered run, lesson and note? This cannot be undone.")) act(clearMemory);
  };

  return (
    <TerminalWindow
      title="memory"
      icon={<Brain className="size-3.5" />}
      onClose={onClose}
      actions={
        <BarButton title="Forget everything" onClick={forgetAll}>
          <Trash2 className="size-3.5" />
        </BarButton>
      }
    >

        <div className="flex items-center gap-2 border-b border-white/8 px-3 py-2">
          {(["runs", "lessons", ...(runId ? ["used"] : [])] as Tab[]).map((option) => (
            <button
              key={option}
              type="button"
              onClick={() => setTab(option)}
              className={cn(
                "rounded-md px-3 py-1 font-mono text-[10px] font-semibold uppercase tracking-wide transition-colors",
                tab === option ? "bg-gradient-to-br from-fuchsia-500 to-violet-600 text-white" : "text-zinc-400 hover:bg-white/5 hover:text-fuchsia-200",
              )}
            >
              {{ runs: "Past runs", lessons: "Lessons & notes", used: "Used in last run" }[option]}
            </button>
          ))}
          {tab !== "used" && (
            <label className="ml-auto flex items-center gap-1.5 rounded-md border border-white/10 bg-white/[0.03] px-2">
              <Search className="size-3 text-zinc-500" />
              <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search" className="h-7 w-40 bg-transparent text-[11px] text-zinc-200 outline-none placeholder:text-zinc-600" />
            </label>
          )}
        </div>

        {error && <p className="border-b border-red-500/20 bg-red-500/10 px-4 py-2 text-[11px] text-red-300">{error}</p>}

        <div className="flex-1 space-y-4 overflow-y-auto p-4">
          {tab === "runs" && <About>Runs the agent finished and kept as examples. Before planning, it is shown the few closest to your new task — not these in full.</About>}
          {tab === "used" && <About>What the last run was actually given at recall: these runs as examples, and these lessons as advice.</About>}
          {tab !== "lessons" && <Episodes episodes={entries.episodes} act={act} title={tab === "used" ? "Runs it was shown" : undefined} />}
          {tab !== "runs" && <Lessons lessons={entries.lessons} act={act} canAdd={tab === "lessons"} title={tab === "used" ? "Lessons it was given" : undefined} />}
        </div>
    </TerminalWindow>
  );
}

type Act = (change: () => Promise<unknown>) => void;

function About({ children }: { children: ReactNode }) {
  return <p className="rounded-lg border border-white/[0.07] bg-white/[0.02] px-2.5 py-1.5 text-[10px] leading-snug text-zinc-500">{children}</p>;
}

/** Give this entry a vector with the embedding model in use, so recall can find it by meaning. */
function EmbedButton({ embedded, onClick }: { embedded: boolean; onClick: () => void }) {
  return (
    <IconButton title={embedded ? "Embed again with the model in use" : "Not embedded: found by keyword only. Click to embed it."} active={!embedded} onClick={onClick}>
      <RefreshCw className="size-3.5" />
    </IconButton>
  );
}

function Episodes({ episodes, act, title }: { episodes: MemoryEpisode[]; act: Act; title?: string }) {
  if (episodes.length === 0) return <Empty>No remembered runs yet. Every run that finishes is remembered; a step check marks it as confirmed.</Empty>;
  return (
    <section className="space-y-2">
      {title && <p className="font-mono text-[10px] font-semibold uppercase tracking-wide text-fuchsia-300/70">{title}</p>}
      {episodes.map((episode) => (
        <div key={episode.id} className={cn("rounded-xl border bg-white/[0.02] p-3", episode.rating === "bad" ? "border-red-500/25 opacity-60" : "border-white/8")}>
          <div className="flex items-start gap-2">
            <p className="flex-1 break-words text-[12px] text-zinc-100">{episode.task_text}</p>
            <IconButton title="Good example" active={episode.rating === "good"} onClick={() => act(() => rateEpisode(episode.id, episode.rating === "good" ? null : "good"))}>
              <ThumbsUp className="size-3.5" />
            </IconButton>
            <IconButton title="Bad example: never reuse it" active={episode.rating === "bad"} onClick={() => act(() => rateEpisode(episode.id, episode.rating === "bad" ? null : "bad"))}>
              <ThumbsDown className="size-3.5" />
            </IconButton>
            <EmbedButton embedded={episode.embedded} onClick={() => act(() => embedMemory("episodes", episode.id))} />
            <IconButton title="Forget this run" onClick={() => act(() => deleteEpisode(episode.id))}>
              <Trash2 className="size-3.5" />
            </IconButton>
          </div>
          <ol className="mt-1.5 list-decimal space-y-0.5 pl-5 text-[11px] text-zinc-400">
            {episode.plan_outline.map((step, index) => (
              <li key={index}>{step.instruction}</li>
            ))}
          </ol>
          <p className="mt-1.5 font-mono text-[10px] text-zinc-600">
            {[...episode.apps, ...episode.domains].join(" · ") || "—"} · recalled {episode.uses}× · {new Date(episode.created_at).toLocaleDateString()}
            {!episode.verified && <span title="No step check confirmed this run, so it is recalled after checked ones." className="ml-1.5 rounded-full border border-amber-400/40 px-1.5 text-amber-300/90">unchecked</span>}
          </p>
        </div>
      ))}
    </section>
  );
}

function Lessons({ lessons, act, canAdd, title }: { lessons: MemoryLesson[]; act: Act; canAdd: boolean; title?: string }) {
  const groups = useMemo(() => {
    const byKey = new Map<string, MemoryLesson[]>();
    for (const lesson of lessons) {
      const key = lesson.key_type === "general" ? "general" : `${lesson.key_type}: ${lesson.key}`;
      byKey.set(key, [...(byKey.get(key) ?? []), lesson]);
    }
    return [...byKey.entries()];
  }, [lessons]);

  return (
    <section className="space-y-3">
      {title && <p className="font-mono text-[10px] font-semibold uppercase tracking-wide text-fuchsia-300/70">{title}</p>}
      {canAdd && (
        <div className="flex items-center gap-2">
          <p className="min-w-0 flex-1 text-[10px] text-zinc-500">Advice recalled before a run: learned when a run struggles, or written by you.</p>
          <button
            type="button"
            title="Embed every lesson again with the embedding model in use"
            onClick={() => act(() => embedMemory("lessons"))}
            className="hud-hover flex h-6 flex-none items-center gap-1 rounded-md border border-white/10 bg-white/[0.04] px-2 font-mono text-[9.5px] uppercase tracking-[0.16em] text-zinc-300"
          >
            <RefreshCw className="size-3" /> Re-embed all
          </button>
        </div>
      )}
      {canAdd && <NoteForm act={act} />}
      {groups.length === 0 && <Empty>No lessons yet. They are learned when a run struggles, or written by you.</Empty>}
      {groups.map(([key, items]) => (
        <div key={key} className="space-y-1.5">
          <p className="font-mono text-[10px] font-semibold uppercase tracking-wide text-fuchsia-300/70">{key}</p>
          {items.map((lesson) => (
            <LessonRow key={lesson.id} lesson={lesson} act={act} />
          ))}
        </div>
      ))}
    </section>
  );
}

function LessonRow({ lesson, act }: { lesson: MemoryLesson; act: Act }) {
  const [draft, setDraft] = useState<string | null>(null);
  const disabled = lesson.disabled === 1;
  return (
    <div className={cn("flex items-start gap-2 rounded-lg border border-white/8 bg-white/[0.02] px-2.5 py-2", disabled && "opacity-50")}>
      <div className="min-w-0 flex-1">
        {draft === null ? (
          <p className="break-words text-[12px] text-zinc-200">{lesson.text}</p>
        ) : (
          <textarea value={draft} onChange={(event) => setDraft(event.target.value)} rows={2} className="w-full resize-none rounded-md border border-white/10 bg-black/30 p-1.5 text-[12px] text-zinc-100 outline-none" />
        )}
        <p className="mt-0.5 font-mono text-[10px] text-zinc-600">
          {lesson.source === "operator" ? "your note" : `learned · seen ${lesson.hits}×`} · recalled {lesson.uses}×{disabled ? " · off" : ""}
          {!lesson.embedded && <span className="ml-1.5 text-amber-300/80">· keyword only</span>}
        </p>
      </div>
      {draft === null ? (
        <IconButton title="Edit" onClick={() => setDraft(lesson.text)}>
          <Pencil className="size-3.5" />
        </IconButton>
      ) : (
        <IconButton title="Save" onClick={() => act(async () => { await editLesson(lesson.id, { text: draft }); setDraft(null); })}>
          <Check className="size-3.5" />
        </IconButton>
      )}
      <IconButton title={disabled ? "Switch on" : "Switch off (never used)"} active={disabled} onClick={() => act(() => editLesson(lesson.id, { disabled: !disabled }))}>
        <XCircle className="size-3.5" />
      </IconButton>
      <EmbedButton embedded={lesson.embedded} onClick={() => act(() => embedMemory("lessons", lesson.id))} />
      <IconButton title="Delete" onClick={() => act(() => deleteLesson(lesson.id))}>
        <Trash2 className="size-3.5" />
      </IconButton>
    </div>
  );
}

function NoteForm({ act }: { act: Act }) {
  const [keyType, setKeyType] = useState<MemoryKeyType>("domain");
  const [key, setKey] = useState("");
  const [text, setText] = useState("");
  const ready = text.trim() !== "" && (keyType === "general" || key.trim() !== "");
  const add = () => act(async () => { await addNote(keyType, key.trim() || "general", text.trim()); setText(""); });
  return (
    <div className="space-y-1.5 rounded-xl border border-fuchsia-500/20 bg-fuchsia-500/[0.04] p-2.5">
      <p className="text-[11px] font-medium text-fuchsia-100">Add a note (used before learned lessons)</p>
      <div className="flex gap-1.5">
        <select value={keyType} onChange={(event) => setKeyType(event.target.value as MemoryKeyType)} className="h-7 rounded-md border border-white/10 bg-black/30 px-1.5 text-[11px] text-zinc-200">
          <option value="domain">Website</option>
          <option value="app">App</option>
          <option value="general">Everywhere</option>
        </select>
        {keyType !== "general" && (
          <input value={key} onChange={(event) => setKey(event.target.value)} placeholder={keyType === "domain" ? "mail.google.com" : "Notepad"} className="h-7 flex-1 rounded-md border border-white/10 bg-black/30 px-2 text-[11px] text-zinc-200 outline-none" />
        )}
      </div>
      <div className="flex gap-1.5">
        <input value={text} onChange={(event) => setText(event.target.value)} maxLength={500} placeholder="e.g. The Compose button is at the top left." className="h-7 flex-1 rounded-md border border-white/10 bg-black/30 px-2 text-[11px] text-zinc-200 outline-none" />
        <button type="button" disabled={!ready} onClick={add} className="flex h-7 items-center gap-1 rounded-md border border-fuchsia-400/40 bg-fuchsia-500/15 px-2.5 text-[11px] text-fuchsia-100 hover:bg-fuchsia-500/25 disabled:opacity-40">
          <Plus className="size-3" /> Add
        </button>
      </div>
    </div>
  );
}

function IconButton({ title, active = false, onClick, children }: { title: string; active?: boolean; onClick: () => void; children: ReactNode }) {
  return (
    <button type="button" title={title} onClick={onClick} className={cn("grid size-6 flex-none place-items-center rounded-md transition-colors", active ? "bg-fuchsia-500/20 text-fuchsia-200" : "text-zinc-500 hover:bg-white/5 hover:text-zinc-200")}>
      {children}
    </button>
  );
}

function Empty({ children }: { children: string }) {
  return <p className="text-[11px] text-zinc-600">{children}</p>;
}
