import { describe, expect, test } from "vitest";

import {
  canCancelAction,
  canExecuteAction,
  createDefaultOperatorDraft,
  createOperatorRunId,
  createOperatorTaskOwner,
  parseSelectorJson,
  summarizeLifecycleEvent,
} from "@/core/computer-use/operator-console";
import type {
  ActionLifecycleRecord,
  ComputerUseLifecycleEvent,
} from "@/core/computer-use/types";

function action(status: ActionLifecycleRecord["status"]): ActionLifecycleRecord {
  return {
    action: {
      action_id: "action-1",
      session_id: "session-1",
      kind: "browser_action",
      args: {},
      postconditions: [],
    },
    owner: {
      thread_id: "thread-1",
      run_id: "run-1",
      agent_id: "agent-1",
    },
    status,
    action_fingerprint: "a".repeat(64),
  };
}

describe("computer-use operator console helpers", () => {
  test("creates an auto-routed empty operator draft", () => {
    const draft = createDefaultOperatorDraft();

    expect(draft.session_id).toBe("");
    expect(draft.browser_url).toBe("");
    expect(draft.target_description).toBe("");
    expect(draft.selector_json).toBe("{}");
    expect(draft.task_prompt).toBe("");
  });

  test("creates a fresh owner run id for each task", () => {
    const first = createOperatorRunId();
    const second = createOperatorRunId();

    expect(first).toMatch(/^operator-run-/);
    expect(second).toMatch(/^operator-run-/);
    expect(first).not.toBe(second);
  });

  test("creates the exact owner used for task approval polling", () => {
    const owner = createOperatorTaskOwner("thread-1");

    expect(owner.thread_id).toBe("thread-1");
    expect(owner.run_id).toMatch(/^operator-run-/);
    expect(owner.agent_id).toBe("computer-use-agent");
  });

  test("parses selector hints as objects only", () => {
    expect(parseSelectorJson('{ "css": "#save" }')).toEqual({
      css: "#save",
    });
    expect(() => parseSelectorJson("[]")).toThrow(
      "Selector JSON must be an object.",
    );
  });

  test("gates execute and cancel controls from lifecycle status", () => {
    expect(canExecuteAction(action("approved"))).toBe(true);
    expect(canExecuteAction(action("pending"))).toBe(false);
    expect(canCancelAction(action("executing"))).toBe(true);
    expect(canCancelAction(action("succeeded"))).toBe(false);
    expect(canCancelAction(null)).toBe(false);
  });

  test("summarizes lifecycle events", () => {
    const event: ComputerUseLifecycleEvent = {
      sequence: 7,
      event_type: "action_status_changed",
      owner: {
        thread_id: "thread-1",
        run_id: "run-1",
        agent_id: "agent-1",
      },
      session_id: "session-1",
      action_id: "action-1",
      action_kind: "browser_action",
      action_status: "succeeded",
      created_at: "2026-01-01T00:00:00Z",
    };

    expect(summarizeLifecycleEvent(event)).toBe(
      "#7 action_status_changed action-1 -> succeeded",
    );
  });
});
