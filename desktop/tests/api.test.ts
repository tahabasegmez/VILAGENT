import { afterEach, describe, expect, it, vi } from "vitest";

import {
  addNote,
  checkRole,
  ComputerUseApiError,
  deleteConnection,
  deleteLesson,
  rateRun,
  saveConnection,
  searchMemory,
  selectRole,
  setApproach,
  startRun,
} from "@/core/computer-use/api";
import { followRun, parseEvents } from "@/core/computer-use/events";

function mockFetch(body: unknown, init: { status?: number } = {}) {
  const fetchMock = vi.fn(async (_input: string, _init?: RequestInit) =>
    new Response(JSON.stringify(body), { status: init.status ?? 200, headers: { "Content-Type": "application/json" } }),
  );
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

function stubDesktop() {
  vi.stubGlobal("window", { vilagentDesktop: { platform: "win32", apiBase: "http://127.0.0.1:5000", authToken: "tok" } });
}

describe("computer-use api", () => {
  it("posts a trimmed task without any approval threshold", async () => {
    const fetchMock = mockFetch({ run_id: "r1", thread_id: "t1" });

    stubDesktop();

    await startRun({ thread_id: " t1 ", run_id: "r1", prompt: "  open notepad " });

    expect(fetchMock).toHaveBeenCalledWith("http://127.0.0.1:5000/api/computer-use/runs", {
      method: "POST",
      headers: { "X-VILAGENT-Internal-Token": "tok", "Content-Type": "application/json" },
      body: JSON.stringify({ thread_id: "t1", run_id: "r1", prompt: "open notepad" }),
    });
  });

  it("rejects an empty prompt before calling the gateway", () => {
    const fetchMock = mockFetch({});

    expect(() => startRun({ thread_id: "t1", prompt: "   " })).toThrow("Task prompt is required.");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("surfaces the gateway detail on errors", async () => {
    mockFetch({ detail: "Computer use host not available" }, { status: 503 });

    const error = await setApproach("brief").catch((e: unknown) => e);

    expect(error).toBeInstanceOf(ComputerUseApiError);
    expect((error as ComputerUseApiError).status).toBe(503);
    expect((error as ComputerUseApiError).message).toBe("Computer use host not available");
  });
});

describe("run events", () => {
  const stream = 'id: 1\r\nevent: activity\r\ndata: {"activity": {"agents": []}}\r\n\r\n: ping\r\n\r\nid: 2\r\nevent: run.finished\r\ndata: {"status": "completed"}\r\n\r\n';

  it("parses events and skips keep-alive comments", () => {
    const { events, rest } = parseEvents(stream);

    expect(events).toEqual([
      { id: 1, type: "activity", data: { activity: { agents: [] } } },
      { id: 2, type: "run.finished", data: { status: "completed" } },
    ]);
    expect(rest).toBe("");
  });

  it("keeps an event split across chunks, even between \\r and \\n", () => {
    const cut = stream.indexOf("\r\n") + 1;
    const first = parseEvents(stream.slice(0, cut));
    const second = parseEvents(first.rest + stream.slice(cut));

    expect(first.events).toEqual([]);
    expect(second.events.map((event) => event.id)).toEqual([1, 2]);
  });

  it("follows a run until it finishes, sending the auth header and resume point", async () => {
    stubDesktop();
    const fetchMock = vi.fn(async (_input: string, _init?: RequestInit) => new Response(stream, { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const seen: string[] = [];

    await followRun("r 1", (event) => seen.push(event.type));

    expect(seen).toEqual(["activity", "run.finished"]);
    expect(fetchMock.mock.calls[0]?.[0]).toBe("http://127.0.0.1:5000/api/computer-use/runs/r%201/events");
    expect(fetchMock.mock.calls[0]?.[1]?.headers).toEqual({ "X-VILAGENT-Internal-Token": "tok", "Last-Event-ID": "0" });
  });

  it("stops when the gateway no longer has the run", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("", { status: 404 })));
    const seen: string[] = [];

    await followRun("gone", (event) => seen.push(event.type));

    expect(seen).toEqual([]);
  });
});

describe("memory api", () => {
  it("sends notes, edits and ratings to the right routes", async () => {
    const fetchMock = mockFetch({});

    await addNote("domain", "mail.google.com", "Compose is top left.");
    await deleteLesson("abc");
    await rateRun("run 1", "bad");
    await searchMemory("send mail");

    const calls = fetchMock.mock.calls.map(([url, init]) => [url, init?.method, init?.body]);
    expect(calls).toEqual([
      ["/api/computer-use/memory/lessons", "POST", JSON.stringify({ key_type: "domain", key: "mail.google.com", text: "Compose is top left." })],
      ["/api/computer-use/memory/lessons/abc", "DELETE", undefined],
      ["/api/computer-use/runs/run%201/rating", "POST", JSON.stringify({ rating: "bad" })],
      ["/api/computer-use/memory/search?q=send+mail", "GET", undefined],
    ]);
  });
});

describe("models api", () => {
  it("points one role at a connection and checks that role on its own", async () => {
    const fetchMock = mockFetch({});

    await selectRole("planner", { connection: "my-glm" });
    await checkRole("vision");

    expect(fetchMock.mock.calls.map(([url, init]) => [url, init?.method, init?.body])).toEqual([
      ["/api/computer-use/models/planner", "POST", JSON.stringify({ connection: "my-glm" })],
      ["/api/computer-use/models/vision/check", "POST", JSON.stringify({})],
    ]);
  });

  it("adds and forgets an API connection", async () => {
    const fetchMock = mockFetch({});

    const params = { model: "glm-4.5-flash", base_url: "https://open.bigmodel.cn/api/paas/v4", api_key: "secret" };
    await saveConnection({ name: "My GLM", kind: "llm", interface: "langchain_openai:ChatOpenAI", params });
    await deleteConnection("my-glm");

    expect(fetchMock.mock.calls.map(([url, init]) => [url, init?.method])).toEqual([
      ["/api/computer-use/connections", "POST"],
      ["/api/computer-use/connections/my-glm", "DELETE"],
    ]);
    // The arguments go up under LangChain's own names.
    expect(fetchMock.mock.calls[0]?.[1]?.body).toBe(JSON.stringify({ name: "My GLM", kind: "llm", interface: "langchain_openai:ChatOpenAI", params }));
  });
});
