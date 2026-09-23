"use client";

import { useEffect, useState } from "react";

import type { AgentActivity, ComputerUseStatus } from "./types";

const DRAFT_STORAGE_KEY = "vilagent.operator.draft.v2";

export type OperatorDraft = {
  thread_id: string;
  task_prompt: string;
};

export type RunOwner = {
  thread_id: string;
  run_id: string;
};

export type OperatorLogEntry = {
  id: number;
  message: string;
  created_at: string;
};

const DEFAULT_DRAFT: OperatorDraft = { thread_id: "operator-thread", task_prompt: "" };

let logId = 0;

export function createRunOwner(threadId: string): RunOwner {
  return { thread_id: threadId, run_id: `operator-run-${crypto.randomUUID()}` };
}

/** Shared operator state: the persisted draft, status/activity snapshots and a busy-label runner. */
export function useOperatorRuntimeState() {
  const [draft, setDraft] = useState<OperatorDraft>(loadDraft);
  const [status, setStatus] = useState<ComputerUseStatus | null>(null);
  const [agentActivity, setAgentActivity] = useState<AgentActivity | null>(null);
  const [logs, setLogs] = useState<OperatorLogEntry[]>([]);
  const [busy, setBusy] = useState<string | null>(null);

  useEffect(() => {
    try {
      window.localStorage.setItem(DRAFT_STORAGE_KEY, JSON.stringify(draft));
    } catch {
      // Storage may be unavailable (private window); the draft is a convenience only.
    }
  }, [draft]);

  function patchDraft(update: Partial<OperatorDraft>) {
    setDraft((current) => ({ ...current, ...update }));
  }

  async function run(label: string, task: () => Promise<void>) {
    setBusy(label);
    try {
      await task();
    } catch (error) {
      const message = error instanceof Error ? error.message : "Computer-use operation failed.";
      setLogs((current) => [{ id: ++logId, message, created_at: new Date().toISOString() }, ...current].slice(0, 12));
    } finally {
      setBusy(null);
    }
  }

  return { draft, patchDraft, status, setStatus, agentActivity, setAgentActivity, logs, busy, run };
}

function loadDraft(): OperatorDraft {
  if (typeof window === "undefined") return DEFAULT_DRAFT;
  try {
    const parsed = JSON.parse(window.localStorage.getItem(DRAFT_STORAGE_KEY) ?? "null") as Partial<OperatorDraft> | null;
    return {
      thread_id: typeof parsed?.thread_id === "string" && parsed.thread_id ? parsed.thread_id : DEFAULT_DRAFT.thread_id,
      task_prompt: typeof parsed?.task_prompt === "string" ? parsed.task_prompt : "",
    };
  } catch {
    return DEFAULT_DRAFT;
  }
}
