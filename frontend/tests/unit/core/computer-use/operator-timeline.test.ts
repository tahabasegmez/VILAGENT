import { describe, expect, test } from "vitest";

import { createDefaultOperatorDraft } from "@/core/computer-use/operator-console";
import {
  buildOperatorEvidenceSources,
  buildOperatorTimeline,
  fromLifecycleEvent,
  mergeOperatorTimelineEvents,
} from "@/core/computer-use/operator-timeline";
import type {
  ActionLifecycleRecord,
  ComputerUseLifecycleEvent,
} from "@/core/computer-use/types";

const draft = createDefaultOperatorDraft();

function baseInput() {
  return {
    approvals: [],
    browserState: null,
    draft,
    health: null,
    latestAction: null,
    lifecycleEvents: [],
    observation: null,
    target: null,
  };
}

function action(status: ActionLifecycleRecord["status"]): ActionLifecycleRecord {
  return {
    action: {
      action_id: "action-1",
      session_id: "session-1",
      kind: "browser_action",
      args: {},
      postconditions: [],
    },
    owner: draft.owner,
    status,
    action_fingerprint: "a".repeat(64),
  };
}

describe("computer-use operator timeline", () => {
  test("stays empty before runtime evidence exists", () => {
    const timeline = buildOperatorTimeline(baseInput());

    expect(timeline).toEqual([]);
  });

  test("marks runtime and action steps from current evidence", () => {
    const timeline = buildOperatorTimeline({
      ...baseInput(),
      browserState: {
        url: "https://example.com",
        title: "Example",
        tab_id: "tab-1",
      },
      draft: { ...draft, browser_session_id: "tab-1" },
      health: {
        enabled: true,
        healthy: true,
        provider_name: "browser-use",
        active_sessions: 1,
      },
      latestAction: action("awaiting_approval"),
      observation: {
        observation_id: "obs-1",
        session_id: "session-1",
        created_at: "2026-01-01T00:00:00Z",
        monitor: {
          monitor_id: "primary",
          bounds: { x: 0, y: 0, width: 10, height: 10 },
        },
        screen_size: { width: 10, height: 10 },
        redaction_applied: true,
      },
      target: {
        strategy: "browser",
        selector: { css: "#save" },
        confidence: 0.9,
        observation_id: "obs-1",
      },
    });

    expect(timeline.map((event) => [event.id, event.status])).toContainEqual([
      "action",
      "running",
    ]);
    expect(timeline.map((event) => [event.id, event.status])).toContainEqual([
      "target",
      "done",
    ]);
  });

  test("builds evidence sources from runtime state", () => {
    const sources = buildOperatorEvidenceSources({
      ...baseInput(),
      health: {
        enabled: true,
        healthy: true,
        provider_name: "browser-use",
        active_sessions: 0,
      },
    });

    expect(sources.map((source) => source.id)).toEqual([
      "operator-owner",
      "gateway-lifecycle",
      "browser-health",
    ]);
  });

  test("converts lifecycle events and merges by id", () => {
    const lifecycleEvent: ComputerUseLifecycleEvent = {
      sequence: 3,
      event_type: "action_status_changed",
      owner: draft.owner,
      session_id: "session-1",
      action_id: "action-1",
      action_kind: "browser_action",
      action_status: "succeeded",
      created_at: "2026-01-01T00:00:00Z",
    };

    const converted = fromLifecycleEvent(lifecycleEvent);
    expect(converted).toMatchObject({
      id: "event-3",
      kind: "action",
      status: "done",
    });

    expect(
      mergeOperatorTimelineEvents(
        [{ ...converted, detail: "old" }],
        [{ ...converted, detail: "new" }],
      ),
    ).toEqual([{ ...converted, detail: "new" }]);
  });
});
