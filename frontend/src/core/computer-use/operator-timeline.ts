import type { OperatorDraft } from "./operator-console";
import type {
  ActionLifecycleRecord,
  ApprovalRecord,
  BrowserHealth,
  BrowserStateSummary,
  ComputerUseLifecycleEvent,
  Observation,
  TargetRef,
} from "./types";

export type OperatorTimelineKind =
  | "system"
  | "session"
  | "runtime"
  | "approval"
  | "action";

export type OperatorTimelineStatus =
  | "pending"
  | "running"
  | "done"
  | "blocked"
  | "failed";

export type OperatorTimelineEvent = {
  id: string;
  kind: OperatorTimelineKind;
  status: OperatorTimelineStatus;
  title: string;
  detail: string;
  source_ids: string[];
  created_at?: string;
};

export type OperatorEvidenceKind =
  | "memory"
  | "gateway"
  | "runtime"
  | "operator"
  | "tool";

export type OperatorEvidenceSource = {
  id: string;
  kind: OperatorEvidenceKind;
  title: string;
  detail: string;
  uri?: string;
};

export type OperatorTimelineInput = {
  draft: OperatorDraft;
  health: BrowserHealth | null;
  browserState: BrowserStateSummary | null;
  observation: Observation | null;
  target: TargetRef | null;
  latestAction: ActionLifecycleRecord | null;
  approvals: ApprovalRecord[];
  lifecycleEvents: ComputerUseLifecycleEvent[];
};

export function buildOperatorEvidenceSources(
  input: OperatorTimelineInput,
): OperatorEvidenceSource[] {
  const sources: OperatorEvidenceSource[] = [
    {
      id: "operator-owner",
      kind: "operator",
      title: "Owner scope",
      detail: `${input.draft.owner.thread_id} / ${input.draft.owner.run_id} / ${input.draft.owner.agent_id}`,
    },
    {
      id: "gateway-lifecycle",
      kind: "gateway",
      title: "Gateway lifecycle API",
      detail: "Approvals, actions, events, observation, and target resolution.",
    },
  ];

  if (input.health !== null) {
    sources.push({
      id: "browser-health",
      kind: "runtime",
      title: input.health.provider_name,
      detail: input.health.healthy
        ? "Browser provider is healthy."
        : `Browser provider unhealthy: ${input.health.error_code ?? "unknown"}`,
    });
  }

  if (input.browserState !== null) {
    sources.push({
      id: "browser-state",
      kind: "runtime",
      title: input.browserState.title ?? input.browserState.url ?? "Browser",
      detail: `tab=${input.browserState.tab_id ?? "unknown"} url=${
        input.browserState.url ?? "unknown"
      }`,
    });
  }

  if (input.observation !== null) {
    sources.push({
      id: "observation",
      kind: "runtime",
      title: input.observation.observation_id,
      detail: input.observation.summary ?? "Latest redacted observation.",
    });
  }

  if (input.target !== null) {
    sources.push({
      id: "target",
      kind: "runtime",
      title: "Resolved target",
      detail: `${input.target.strategy} confidence=${input.target.confidence.toFixed(2)}`,
    });
  }

  return sources;
}

export function buildOperatorTimeline(
  input: OperatorTimelineInput,
): OperatorTimelineEvent[] {
  const hasBrowserSession = input.draft.browser_session_id.trim().length > 0;
  const hasHostSession = input.draft.session_id.trim().length > 0;
  const healthOk = input.health?.healthy === true;
  const hasObservation = input.observation !== null;
  const hasTarget = input.target !== null;

  const timeline: OperatorTimelineEvent[] = [];

  if (
    input.health === null &&
    !hasHostSession &&
    !hasBrowserSession &&
    !hasObservation &&
    !hasTarget &&
    input.latestAction === null &&
    input.approvals.length === 0 &&
    input.lifecycleEvents.length === 0
  ) {
    return [];
  }

  timeline.push(
    {
      id: "plan-health",
      kind: "system",
      status: healthOk ? "done" : "pending",
      title: "Runtime health",
      detail:
        input.health === null
          ? "No runtime health check has been run yet."
          : healthOk
            ? `${input.health.provider_name} is healthy.`
            : `${input.health.provider_name} is not healthy.`,
      source_ids: ["gateway-lifecycle", ...(input.health ? ["browser-health"] : [])],
    },
    {
      id: "host-session",
      kind: "session",
      status: hasHostSession ? "done" : "pending",
      title: "Windows host session",
      detail: hasHostSession
        ? `Using host session ${input.draft.session_id}.`
        : "Create a Windows host session before observing native UI.",
      source_ids: ["operator-owner"],
    },
  );

  if (hasBrowserSession || input.browserState !== null) {
    timeline.push({
      id: "browser-session",
      kind: "runtime",
      status: hasBrowserSession ? "done" : "pending",
      title: "Browser context",
      detail: hasBrowserSession
        ? `Browser context available: ${input.draft.browser_session_id}.`
        : "Browser context is optional; the agent can use Windows/UIA/vision without it.",
      source_ids: ["operator-owner", ...(input.browserState ? ["browser-state"] : [])],
    });
  }

  timeline.push(
    {
      id: "observation",
      kind: "runtime",
      status: hasObservation ? "done" : hasHostSession ? "pending" : "blocked",
      title: "Current UI observation",
      detail: hasObservation
        ? `Observation ${input.observation?.observation_id} captured.`
        : "Run Observe to capture Windows UI evidence.",
      source_ids: hasObservation ? ["observation"] : ["gateway-lifecycle"],
    },
    {
      id: "target",
      kind: "runtime",
      status: hasTarget ? "done" : hasObservation ? "pending" : "blocked",
      title: "Target resolution",
      detail: hasTarget
        ? `${input.target?.strategy} target is ready.`
        : "Resolve a target from the latest observation.",
      source_ids: hasTarget ? ["target"] : ["gateway-lifecycle"],
    },
    {
      id: "action",
      kind: "action",
      status: actionTimelineStatus(input.latestAction, hasTarget),
      title: "Guarded action",
      detail:
        input.latestAction === null
          ? "Actions can be submitted after target resolution, or through the agent task runner."
          : `${input.latestAction.action.kind} is ${input.latestAction.status}.`,
      source_ids: ["gateway-lifecycle"],
    },
  );

  for (const approval of input.approvals) {
    timeline.push({
      id: `approval-${approval.approval_id}`,
      kind: "approval",
      status: approval.status === "pending" ? "running" : "done",
      title: `Approval ${approval.status}`,
      detail: `${approval.action_id}: ${
        approval.reasons[0] ?? "No reason supplied"
      }`,
      source_ids: ["gateway-lifecycle"],
      created_at: approval.created_at,
    });
  }

  for (const event of input.lifecycleEvents.slice(-8)) {
    timeline.push(fromLifecycleEvent(event));
  }

  return dedupeTimeline(timeline);
}

export function fromLifecycleEvent(
  event: ComputerUseLifecycleEvent,
): OperatorTimelineEvent {
  return {
    id: `event-${event.sequence}`,
    kind: event.approval_id ? "approval" : "action",
    status: lifecycleTimelineStatus(event),
    title: event.event_type.replaceAll("_", " "),
    detail: `${event.action_id} -> ${
      event.action_status ?? event.approval_status ?? event.error_code ?? "seen"
    }`,
    source_ids: ["gateway-lifecycle"],
    created_at: event.created_at,
  };
}

export function mergeOperatorTimelineEvents(
  current: OperatorTimelineEvent[],
  incoming: OperatorTimelineEvent[],
): OperatorTimelineEvent[] {
  return dedupeTimeline([...current, ...incoming]);
}

function actionTimelineStatus(
  action: ActionLifecycleRecord | null,
  hasTarget: boolean,
): OperatorTimelineStatus {
  if (action === null) {
    return hasTarget ? "pending" : "blocked";
  }
  if (action.status === "failed" || action.status === "uncertain") {
    return "failed";
  }
  if (action.status === "executing" || action.status === "awaiting_approval") {
    return "running";
  }
  if (
    action.status === "approved" ||
    action.status === "succeeded" ||
    action.status === "denied" ||
    action.status === "cancelled"
  ) {
    return "done";
  }
  return "pending";
}

function lifecycleTimelineStatus(
  event: ComputerUseLifecycleEvent,
): OperatorTimelineStatus {
  if (event.error_code) {
    return "failed";
  }
  if (
    event.action_status === "failed" ||
    event.action_status === "uncertain"
  ) {
    return "failed";
  }
  if (
    event.action_status === "executing" ||
    event.action_status === "awaiting_approval" ||
    event.approval_status === "pending"
  ) {
    return "running";
  }
  return "done";
}

function dedupeTimeline(
  events: OperatorTimelineEvent[],
): OperatorTimelineEvent[] {
  const byId = new Map<string, OperatorTimelineEvent>();
  for (const event of events) {
    byId.set(event.id, event);
  }
  return [...byId.values()];
}
