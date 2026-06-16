"use client";

import { useEffect, useMemo, useState } from "react";

import {
  createDefaultOperatorDraft,
  type OperatorDraft,
} from "./operator-console";
import {
  buildOperatorEvidenceSources,
  buildOperatorTimeline,
} from "./operator-timeline";
import type {
  ActionLifecycleRecord,
  AgentActivity,
  ApprovalRecord,
  BrowserContext,
  BrowserHealth,
  BrowserStateSummary,
  ComputerUseConfigValidation,
  ComputerUseLifecycleEvent,
  ComputerUseStatus,
  ComputerUseTaskRunResult,
  ComputerUseTextModelHealth,
  DesktopSessionSnapshot,
  Observation,
  TargetRef,
  VisionProviderHealth,
} from "./types";

export const OPERATOR_DRAFT_STORAGE_KEY = "vilagent.operator.draft.v1";

export type OperatorLogEntry = {
  id: number;
  level: "info" | "success" | "error";
  message: string;
  detail?: unknown;
  created_at: string;
};

let logId = 0;

export function useOperatorRuntimeState() {
  const [draft, setDraft] = useState<OperatorDraft>(loadStoredDraft);
  const [health, setHealth] = useState<BrowserHealth | null>(null);
  const [status, setStatus] = useState<ComputerUseStatus | null>(null);
  const [configValidation, setConfigValidation] =
    useState<ComputerUseConfigValidation | null>(null);
  const [textModelHealth, setTextModelHealth] =
    useState<ComputerUseTextModelHealth | null>(null);
  const [visionHealth, setVisionHealth] =
    useState<VisionProviderHealth | null>(null);
  const [desktopSessions, setDesktopSessions] = useState<
    DesktopSessionSnapshot[]
  >([]);
  const [browserState, setBrowserState] = useState<BrowserStateSummary | null>(
    null,
  );
  const [observation, setObservation] = useState<Observation | null>(null);
  const [target, setTarget] = useState<TargetRef | null>(null);
  const [latestAction, setLatestAction] =
    useState<ActionLifecycleRecord | null>(null);
  const [taskRunResult, setTaskRunResult] =
    useState<ComputerUseTaskRunResult | null>(null);
  const [agentActivity, setAgentActivity] = useState<AgentActivity | null>(null);
  const [approvals, setApprovals] = useState<ApprovalRecord[]>([]);
  const [events, setEvents] = useState<ComputerUseLifecycleEvent[]>([]);
  const [logs, setLogs] = useState<OperatorLogEntry[]>([]);
  const [busy, setBusy] = useState<string | null>(null);

  useEffect(() => {
    persistDraft(draft);
  }, [draft]);

  const timelineInput = useMemo(
    () => ({
      approvals,
      browserState,
      draft,
      health,
      latestAction,
      lifecycleEvents: events,
      observation,
      target,
    }),
    [
      approvals,
      browserState,
      draft,
      events,
      health,
      latestAction,
      observation,
      target,
    ],
  );
  const timeline = useMemo(
    () => buildOperatorTimeline(timelineInput),
    [timelineInput],
  );
  const evidenceSources = useMemo(
    () => buildOperatorEvidenceSources(timelineInput),
    [timelineInput],
  );
  const context = useMemo<BrowserContext>(
    () => ({
      owner: draft.owner,
      browser_session_id: draft.browser_session_id,
    }),
    [draft.browser_session_id, draft.owner],
  );

  function patchDraft(update: Partial<OperatorDraft>) {
    setDraft((current) => ({ ...current, ...update }));
  }

  function patchOwner(update: Partial<OperatorDraft["owner"]>) {
    setDraft((current) => ({
      ...current,
      owner: { ...current.owner, ...update },
    }));
  }

  function pushLog(level: OperatorLogEntry["level"], message: string) {
    setLogs((current) =>
      [
        {
          id: ++logId,
          level,
          message,
          created_at: new Date().toISOString(),
        },
        ...current,
      ].slice(0, 12),
    );
  }

  async function run(label: string, task: () => Promise<void>) {
    setBusy(label);
    try {
      await task();
    } catch (error) {
      setLogs((current) =>
        [
          {
            id: ++logId,
            level: "error" as const,
            message:
              error instanceof Error
                ? error.message
                : "Computer-use operation failed.",
            detail: serializeError(error),
            created_at: new Date().toISOString(),
          },
          ...current,
        ].slice(0, 12),
      );
    } finally {
      setBusy(null);
    }
  }

  return {
    approvals,
    browserState,
    busy,
    context,
    draft,
    events,
    desktopSessions,
    health,
    status,
    configValidation,
    textModelHealth,
    visionHealth,
    latestAction,
    agentActivity,
    taskRunResult,
    logs,
    observation,
    patchDraft,
    patchOwner,
    evidenceSources,
    pushLog,
    run,
    setApprovals,
    setBrowserState,
    setEvents,
    setDesktopSessions,
    setHealth,
    setStatus,
    setConfigValidation,
    setTextModelHealth,
    setVisionHealth,
    setLatestAction,
    setAgentActivity,
    setTaskRunResult,
    setObservation,
    setTarget,
    target,
    timeline,
  };
}

export function loadStoredDraft(): OperatorDraft {
  const fallback = createDefaultOperatorDraft();
  if (typeof window === "undefined") {
    return fallback;
  }

  try {
    const raw = window.localStorage.getItem(OPERATOR_DRAFT_STORAGE_KEY);
    if (raw === null) {
      return fallback;
    }
    return normalizeOperatorDraft(JSON.parse(raw), fallback);
  } catch {
    return fallback;
  }
}

export function persistDraft(draft: OperatorDraft): void {
  if (typeof window === "undefined") {
    return;
  }
  window.localStorage.setItem(OPERATOR_DRAFT_STORAGE_KEY, JSON.stringify(draft));
}

export function normalizeOperatorDraft(
  value: unknown,
  fallback: OperatorDraft,
): OperatorDraft {
  if (value === null || typeof value !== "object") {
    return fallback;
  }

  const candidate = value as Partial<OperatorDraft>;
  const ownerValue =
    candidate.owner !== undefined &&
    candidate.owner !== null &&
    typeof candidate.owner === "object"
      ? candidate.owner
      : {};
  const owner = ownerValue as Partial<OperatorDraft["owner"]>;
  const browserUrl =
    typeof candidate.browser_url === "string"
      ? stripLegacyDemoValue(candidate.browser_url, "https://example.com")
      : fallback.browser_url;
  const targetDescription =
    typeof candidate.target_description === "string"
      ? stripLegacyDemoValue(
          candidate.target_description,
          "Primary action button",
        )
      : fallback.target_description;
  const selectorJson =
    typeof candidate.selector_json === "string"
      ? stripLegacyDemoValue(candidate.selector_json, '{ "css": "button" }') ||
        "{}"
      : fallback.selector_json;
  const taskPrompt =
    typeof candidate.task_prompt === "string"
      ? stripLegacyDemoValue(
          candidate.task_prompt,
          "Observe the current desktop, find the primary action button, and explain the next safe action before doing anything risky.",
        )
      : fallback.task_prompt;

  return {
    owner: {
      thread_id:
        typeof owner.thread_id === "string"
          ? owner.thread_id
          : fallback.owner.thread_id,
      run_id:
        typeof owner.run_id === "string" ? owner.run_id : fallback.owner.run_id,
      agent_id:
        typeof owner.agent_id === "string"
          ? owner.agent_id
          : fallback.owner.agent_id,
    },
    session_id:
      typeof candidate.session_id === "string"
        ? candidate.session_id
        : fallback.session_id,
    browser_session_id:
      typeof candidate.browser_session_id === "string"
        ? candidate.browser_session_id
        : fallback.browser_session_id,
    browser_url:
      browserUrl,
    target_description: targetDescription,
    selector_json: selectorJson,
    browser_action:
      typeof candidate.browser_action === "string"
        ? candidate.browser_action
        : fallback.browser_action,
    task_prompt: taskPrompt,
  };
}

function stripLegacyDemoValue(value: string, legacyValue: string): string {
  return value.trim() === legacyValue ? "" : value;
}

function serializeError(error: unknown): Record<string, unknown> {
  if (error instanceof Error) {
    const withApiFields = error as Error & {
      status?: unknown;
      detail?: unknown;
      cause?: unknown;
    };
    return {
      name: error.name,
      message: error.message,
      status: withApiFields.status,
      detail: withApiFields.detail,
      cause: withApiFields.cause,
      stack: error.stack,
    };
  }
  return { value: String(error) };
}
