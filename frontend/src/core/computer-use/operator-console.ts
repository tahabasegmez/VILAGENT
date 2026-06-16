import type {
  ActionLifecycleRecord,
  ActionOwner,
  ApprovalRecord,
  ComputerUseLifecycleEvent,
} from "./types";

export type OperatorDraft = {
  owner: ActionOwner;
  session_id: string;
  browser_session_id: string;
  browser_url: string;
  target_description: string;
  selector_json: string;
  browser_action: string;
  task_prompt: string;
};

export function createDefaultOperatorDraft(): OperatorDraft {
  return {
    owner: {
      thread_id: "operator-thread",
      run_id: "operator-run",
      agent_id: "computer-use-agent",
    },
    session_id: "",
    browser_session_id: "",
    browser_url: "",
    target_description: "",
    selector_json: "{}",
    browser_action: "click",
    task_prompt: "",
  };
}

export function createOperatorRunId(): string {
  return `operator-run-${crypto.randomUUID()}`;
}

export function createOperatorTaskOwner(threadId: string): ActionOwner {
  return {
    thread_id: threadId,
    run_id: createOperatorRunId(),
    agent_id: "computer-use-agent",
  };
}

export function parseSelectorJson(raw: string): Record<string, unknown> {
  const value = JSON.parse(raw) as unknown;
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("Selector JSON must be an object.");
  }
  return value as Record<string, unknown>;
}

export function canExecuteAction(
  action: ActionLifecycleRecord | null,
): boolean {
  return action?.status === "approved";
}

export function canCancelAction(action: ActionLifecycleRecord | null): boolean {
  return (
    action?.status === "pending" ||
    action?.status === "awaiting_approval" ||
    action?.status === "approved" ||
    action?.status === "executing"
  );
}

export function summarizeApproval(approval: ApprovalRecord): string {
  const reason = approval.reasons[0] ?? "No reason supplied";
  return `${approval.status}: ${approval.action_id} (${reason})`;
}

export function summarizeLifecycleEvent(
  event: ComputerUseLifecycleEvent,
): string {
  const status =
    event.action_status ?? event.approval_status ?? event.error_code ?? "seen";
  return `#${event.sequence} ${event.event_type} ${event.action_id} -> ${status}`;
}
