import { beforeEach, describe, expect, test, vi } from "vitest";

vi.mock("@/core/api/fetcher", () => ({
  fetch: vi.fn(),
}));

vi.mock("@/core/config", () => ({
  getBackendBaseURL: () => "",
}));

import { fetch as fetcher } from "@/core/api/fetcher";
import {
  closeBrowserSession,
  ComputerUseApiError,
  createDesktopSession,
  createBrowserSession,
  approveAction,
  cancelAction,
  executeAction,
  getBrowserHealth,
  getComputerUseStatus,
  getTextModelHealth,
  getVisionHealth,
  listDesktopSessions,
  listLifecycleEvents,
  listPendingApprovals,
  observeSessionWithBrowser,
  resolveBrowserTarget,
  runComputerUseTask,
  submitBrowserAction,
  validateComputerUseConfig,
  waitLifecycleEvents,
} from "@/core/computer-use/api";

const mockedFetch = vi.mocked(fetcher);

const owner = {
  thread_id: "thread-1",
  run_id: "run-1",
  agent_id: "agent-1",
};

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

beforeEach(() => {
  mockedFetch.mockReset();
});

describe("computer-use API client", () => {
  test("loads sanitized computer-use status", async () => {
    mockedFetch.mockResolvedValueOnce(
      jsonResponse(200, {
        enabled: true,
        agent_mode: "vilagent",
        assistant_id: "computer_use_agent",
        prompt_profile: "compact",
        platform: "windows",
        runtime_mode: "dedicated_process",
        text_model: {
          provider: "pyngrok",
          model_config_name: "vilagent-text-pyngrok",
          model_name: "Qwen/Qwen3-32B",
          configured: true,
          endpoint_configured: true,
        },
        vision_model: {
          provider: "fara",
          enabled: true,
          model_name: "microsoft/Fara-7B",
          endpoint_configured: true,
          endpoint_path: "/chat/completions",
        },
        browser_enabled: true,
        allowed_actions: ["focus_window"],
        budgets: {
          token_usage_enabled: true,
          planner_calls: 20,
          vision_calls: 10,
          total_actions: 100,
          duration_seconds: 1800,
        },
      }),
    );

    const status = await getComputerUseStatus();

    expect(mockedFetch).toHaveBeenCalledWith("/api/computer-use/status", {
      method: "GET",
      headers: undefined,
      body: undefined,
    });
    expect(status.assistant_id).toBe("computer_use_agent");
    expect(status.vision_model.endpoint_configured).toBe(true);
    expect(status.budgets.token_usage_enabled).toBe(true);
  });

  test("loads sanitized config validation", async () => {
    mockedFetch.mockResolvedValueOnce(
      jsonResponse(200, {
        healthy: true,
        config_path: "D:/code/my-projects/vilagent-main/config.yaml",
        env_path: "D:/code/my-projects/vilagent-main/.env",
        checks: [{ key: "computer_use.text_model.provider", status: "ok", message: "Text planner provider is pyngrok." }],
      }),
    );

    const validation = await validateComputerUseConfig();

    expect(mockedFetch).toHaveBeenCalledWith(
      "/api/computer-use/config/validation",
      { method: "GET", headers: undefined, body: undefined },
    );
    expect(validation.healthy).toBe(true);
  });

  test("loads text model health", async () => {
    mockedFetch.mockResolvedValueOnce(
      jsonResponse(200, {
        provider_name: "vilagent-text-model",
        provider: "pyngrok",
        healthy: true,
        configured: true,
        endpoint_configured: true,
        probe_supported: true,
        model_config_name: "vilagent-text-pyngrok",
        model_name: "Qwen/Qwen3-32B",
        endpoint_kind: "pyngrok",
        details: { model_count: 1 },
      }),
    );

    const health = await getTextModelHealth();

    expect(mockedFetch).toHaveBeenCalledWith(
      "/api/computer-use/text-model/health",
      { method: "GET", headers: undefined, body: undefined },
    );
    expect(health.healthy).toBe(true);
  });

  test("loads UI-TARS vision health", async () => {
    mockedFetch.mockResolvedValueOnce(
      jsonResponse(200, {
        provider_name: "ui-tars-pyngrok",
        enabled: true,
        healthy: true,
        endpoint_configured: true,
        model_name: "ui-tars",
        details: { status: "ok" },
      }),
    );

    const health = await getVisionHealth();

    expect(mockedFetch).toHaveBeenCalledWith(
      "/api/computer-use/vision/health",
      { method: "GET", headers: undefined, body: undefined },
    );
    expect(health.healthy).toBe(true);
  });

  test("starts computer_use_agent task runs through VILAGENT-native endpoint", async () => {
    mockedFetch.mockResolvedValueOnce(
      jsonResponse(200, {
        thread_id: "operator-thread",
        assistant_id: "computer_use_agent",
        output: { messages: [{ type: "ai", content: "done" }] },
      }),
    );

    const result = await runComputerUseTask({
      thread_id: "operator-thread",
      prompt: "Observe the desktop.",
      auto_approve_risk_threshold: "high",
    });

    expect(mockedFetch).toHaveBeenCalledWith("/api/computer-use/tasks/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        thread_id: "operator-thread",
        prompt: "Observe the desktop.",
        auto_approve_risk_threshold: "high",
      }),
    });
    expect(result.assistant_id).toBe("computer_use_agent");
    expect(result.thread_id).toBe("operator-thread");
  });

  test("creates and lists desktop sessions", async () => {
    mockedFetch
      .mockResolvedValueOnce(
        jsonResponse(201, {
          session: {
            session_id: "session-1",
            platform: "windows",
            monitor_id: "primary",
            created_at: "2026-01-01T00:00:00Z",
          },
          status: "ready",
          provider_name: "windows-screen",
          provider_health: "healthy",
        }),
      )
      .mockResolvedValueOnce(jsonResponse(200, []));

    await createDesktopSession(" session-1 ");
    await listDesktopSessions();

    expect(mockedFetch).toHaveBeenNthCalledWith(
      1,
      "/api/computer-use/sessions",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: "session-1" }),
      },
    );
    expect(mockedFetch).toHaveBeenNthCalledWith(
      2,
      "/api/computer-use/sessions",
      { method: "GET", headers: undefined, body: undefined },
    );
  });

  test("loads browser health", async () => {
    mockedFetch.mockResolvedValueOnce(
      jsonResponse(200, {
        enabled: true,
        healthy: true,
        provider_name: "browser-use",
        active_sessions: 1,
      }),
    );

    const health = await getBrowserHealth();

    expect(mockedFetch).toHaveBeenCalledWith("/api/computer-use/browser/health", {
      method: "GET",
      headers: undefined,
      body: undefined,
    });
    expect(health.healthy).toBe(true);
  });

  test("creates and closes owner-scoped browser sessions", async () => {
    mockedFetch
      .mockResolvedValueOnce(
        jsonResponse(201, {
          url: "https://example.com",
          title: "Example",
          tab_id: "tab-1",
          allowed_domain: true,
        }),
      )
      .mockResolvedValueOnce(new Response(null, { status: 204 }));

    await createBrowserSession({ owner, url: "https://example.com" });
    await closeBrowserSession({ owner, browser_session_id: "tab-1" });

    expect(mockedFetch).toHaveBeenNthCalledWith(
      1,
      "/api/computer-use/browser/sessions",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ owner, url: "https://example.com" }),
      },
    );
    expect(mockedFetch).toHaveBeenNthCalledWith(
      2,
      "/api/computer-use/browser/sessions/tab-1",
      {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(owner),
      },
    );
  });

  test("uses body-based browser observe and target resolution endpoints", async () => {
    mockedFetch
      .mockResolvedValueOnce(
        jsonResponse(200, {
          observation_id: "obs-1",
          session_id: "session-1",
          created_at: "2026-01-01T00:00:00Z",
          browser_state: { url: "https://example.com", tab_id: "tab-1" },
          monitor: {
            monitor_id: "primary",
            bounds: { x: 0, y: 0, width: 10, height: 10 },
          },
          screen_size: { width: 10, height: 10 },
          redaction_applied: false,
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse(200, {
          target: {
            strategy: "browser",
            selector: { css: "#save" },
            confidence: 1,
            observation_id: "obs-1",
          },
          attempts: [],
        }),
      );

    await observeSessionWithBrowser("session-1", {
      owner,
      browser_session_id: "tab-1",
    });
    const result = await resolveBrowserTarget(
      "session-1",
      { owner, browser_session_id: "tab-1" },
      { description: "Save", selector_hints: { text: "Save" } },
    );

    expect(mockedFetch).toHaveBeenNthCalledWith(
      1,
      "/api/computer-use/sessions/session-1/browser/observe",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ owner, browser_session_id: "tab-1" }),
      },
    );
    expect(mockedFetch).toHaveBeenNthCalledWith(
      2,
      "/api/computer-use/sessions/session-1/browser/targets/resolve",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          owner,
          browser_session_id: "tab-1",
          query: { description: "Save", selector_hints: { text: "Save" } },
        }),
      },
    );
    expect(result.target?.strategy).toBe("browser");
  });

  test("submits browser actions through the safe helper endpoint", async () => {
    mockedFetch.mockResolvedValueOnce(
      jsonResponse(201, {
        action: {
          action_id: "browser-1",
          session_id: "session-1",
          kind: "browser_action",
          target: {
            strategy: "browser",
            selector: { css: "#save" },
            confidence: 1,
            observation_id: "obs-1",
          },
          args: { tab_id: "tab-1", url: "https://example.com" },
          postconditions: [],
        },
        owner,
        status: "approved",
        action_fingerprint: "a".repeat(64),
      }),
    );

    const result = await submitBrowserAction({
      owner,
      session_id: "session-1",
      action_id: "browser-1",
      target: {
        strategy: "browser",
        selector: { css: "#save" },
        confidence: 1,
        observation_id: "obs-1",
      },
      browser_state: {
        url: "https://example.com",
        tab_id: "tab-1",
        allowed_domain: true,
      },
    });

    expect(mockedFetch).toHaveBeenCalledWith(
      "/api/computer-use/browser/actions",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          owner,
          session_id: "session-1",
          action_id: "browser-1",
          target: {
            strategy: "browser",
            selector: { css: "#save" },
            confidence: 1,
            observation_id: "obs-1",
          },
          browser_state: {
            url: "https://example.com",
            tab_id: "tab-1",
            allowed_domain: true,
          },
        }),
      },
    );
    expect(result.status).toBe("approved");
  });

  test("drives approval, action, and lifecycle helper endpoints", async () => {
    mockedFetch
      .mockResolvedValueOnce(jsonResponse(200, []))
      .mockResolvedValueOnce(
        jsonResponse(200, {
          approval_id: "approval-1",
          action_id: "browser-1",
          session_id: "session-1",
          owner,
          action_fingerprint: "a".repeat(64),
          status: "approved",
          reasons: [],
          consequences: [],
          created_at: "2026-01-01T00:00:00Z",
          expires_at: "2026-01-01T00:10:00Z",
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse(200, {
          action: {
            action_id: "browser-1",
            session_id: "session-1",
            kind: "browser_action",
            args: {},
            postconditions: [],
          },
          owner,
          status: "executing",
          action_fingerprint: "a".repeat(64),
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse(200, {
          action: {
            action_id: "browser-1",
            session_id: "session-1",
            kind: "browser_action",
            args: {},
            postconditions: [],
          },
          owner,
          status: "cancelled",
          action_fingerprint: "a".repeat(64),
        }),
      )
      .mockResolvedValueOnce(jsonResponse(200, []))
      .mockResolvedValueOnce(jsonResponse(200, []));

    await listPendingApprovals(owner);
    await approveAction("approval-1", {
      owner,
      decided_by: "operator-1",
      reason: "ok",
    });
    await executeAction("browser-1", owner);
    await cancelAction("browser-1", { owner, reason: "stop" });
    await listLifecycleEvents(owner, { after_sequence: 2, limit: 10 });
    await waitLifecycleEvents(owner, { timeout_seconds: 0.5 });

    expect(mockedFetch).toHaveBeenNthCalledWith(
      1,
      "/api/computer-use/approvals?thread_id=thread-1&run_id=run-1&agent_id=agent-1",
      { method: "GET", headers: undefined, body: undefined },
    );
    expect(mockedFetch).toHaveBeenNthCalledWith(
      2,
      "/api/computer-use/approvals/approval-1/approve",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          owner,
          decided_by: "operator-1",
          reason: "ok",
        }),
      },
    );
    expect(mockedFetch).toHaveBeenNthCalledWith(
      3,
      "/api/computer-use/actions/browser-1/execute",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ owner }),
      },
    );
    expect(mockedFetch).toHaveBeenNthCalledWith(
      4,
      "/api/computer-use/actions/browser-1/cancel",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ owner, reason: "stop" }),
      },
    );
    expect(mockedFetch).toHaveBeenNthCalledWith(
      5,
      "/api/computer-use/events?thread_id=thread-1&run_id=run-1&agent_id=agent-1&after_sequence=2&limit=10",
      { method: "GET", headers: undefined, body: undefined },
    );
    expect(mockedFetch).toHaveBeenNthCalledWith(
      6,
      "/api/computer-use/events/wait?thread_id=thread-1&run_id=run-1&agent_id=agent-1&timeout_seconds=0.5",
      { method: "GET", headers: undefined, body: undefined },
    );
  });

  test("throws typed API errors with backend detail", async () => {
    mockedFetch.mockResolvedValueOnce(
      jsonResponse(403, { detail: "Browser URL is not allowed" }),
    );

    const error = await getBrowserHealth().catch((exc: unknown) => exc);

    expect(error).toBeInstanceOf(ComputerUseApiError);
    expect(error).toMatchObject({
      name: "ComputerUseApiError",
      status: 403,
      detail: "Browser URL is not allowed",
    });
  });
});
