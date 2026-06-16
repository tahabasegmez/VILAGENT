import { describe, expect, test } from "vitest";

import { createDefaultOperatorDraft } from "@/core/computer-use/operator-console";
import { normalizeOperatorDraft } from "@/core/computer-use/operator-runtime";

describe("computer-use operator runtime helpers", () => {
  test("normalizes partial stored drafts without dropping defaults", () => {
    const fallback = createDefaultOperatorDraft();

    expect(
      normalizeOperatorDraft(
        {
          owner: { thread_id: "thread-from-storage" },
          browser_session_id: "tab-1",
          selector_json: '{ "text": "Save" }',
        },
        fallback,
      ),
    ).toEqual({
      ...fallback,
      owner: {
        ...fallback.owner,
        thread_id: "thread-from-storage",
      },
      browser_session_id: "tab-1",
      selector_json: '{ "text": "Save" }',
    });
  });

  test("falls back when stored draft shape is invalid", () => {
    const fallback = createDefaultOperatorDraft();

    expect(normalizeOperatorDraft(null, fallback)).toEqual(fallback);
    expect(normalizeOperatorDraft("bad", fallback)).toEqual(fallback);
  });
});
