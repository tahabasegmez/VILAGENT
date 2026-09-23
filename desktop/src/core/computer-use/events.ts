import { apiUrl, authHeaders } from "./api";
import type { RunEvent } from "./types";

const RETRY_MS = 1000;

/**
 * Split Server-Sent Events text into events. Returns the parsed events and the unfinished tail,
 * which the caller prepends to the next chunk. A trailing "\r" is held back because its "\n" may
 * arrive in the next chunk (the gateway ends lines with "\r\n").
 */
export function parseEvents(buffer: string): { events: RunEvent[]; rest: string } {
  const hold = buffer.endsWith("\r") ? "\r" : "";
  const blocks = buffer.slice(0, buffer.length - hold.length).replace(/\r\n/g, "\n").split("\n\n");
  const rest = (blocks.pop() ?? "") + hold;
  return { events: blocks.map(parseBlock).filter((event): event is RunEvent => event !== null), rest };
}

function parseBlock(block: string): RunEvent | null {
  let id = 0;
  let type = "message";
  const data: string[] = [];
  for (const line of block.split("\n")) {
    if (!line || line.startsWith(":")) continue; // keep-alive comments
    const colon = line.indexOf(":");
    const field = colon < 0 ? line : line.slice(0, colon);
    const value = colon < 0 ? "" : line.slice(colon + 1).replace(/^ /, "");
    if (field === "id") id = Number(value);
    else if (field === "event") type = value;
    else if (field === "data") data.push(value);
  }
  if (data.length === 0) return null;
  return { id, type, data: JSON.parse(data.join("\n")) as unknown };
}

/**
 * Follow a run's events until `run.finished`, reconnecting with `Last-Event-ID` if the stream
 * drops. Resolves when the run finished, when the gateway no longer has the run live (404), or
 * when `signal` aborts.
 */
export async function followRun(runId: string, onEvent: (event: RunEvent) => void, signal?: AbortSignal): Promise<void> {
  let lastId = 0;
  while (!signal?.aborted) {
    try {
      const response = await fetch(apiUrl(`/runs/${encodeURIComponent(runId)}/events`), {
        headers: { ...authHeaders(), "Last-Event-ID": String(lastId) },
        signal,
      });
      if (response.status === 404) return;
      if (!response.ok || !response.body) throw new Error(`Run events failed: ${response.status}`);
      const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
      let buffer = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        const parsed = parseEvents(buffer + value);
        buffer = parsed.rest;
        for (const event of parsed.events) {
          lastId = event.id;
          onEvent(event);
          if (event.type === "run.finished") return;
        }
      }
    } catch (error) {
      if (signal?.aborted) return;
      console.warn("run events", error);
    }
    await new Promise((resolve) => setTimeout(resolve, RETRY_MS));
  }
}
