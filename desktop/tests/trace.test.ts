import { describe, expect, it } from "vitest";

import { activeSpan, applyTrace, EMPTY_TRACE, layoutTrace, VISIBLE_CHILDREN, type Trace } from "@/core/computer-use/trace";
import type { TraceSpan } from "@/core/computer-use/types";

function span(id: string, name: string, extra: Partial<TraceSpan> = {}): TraceSpan {
  return { id, parent: null, name, kind: "node", status: "done", started_at: "", memory: [], meta: {}, ...extra };
}

function build(spans: TraceSpan[]): Trace {
  return spans.reduce(applyTrace, EMPTY_TRACE);
}

describe("trace", () => {
  it("updates spans in place and keeps the order they started in", () => {
    const trace = build([span("a", "plan", { status: "running" }), span("b", "select_step"), span("a", "plan", { status: "done", output: "1. x" })]);

    expect(trace.order).toEqual(["a", "b"]);
    expect(trace.spans.a?.output).toBe("1. x");
  });

  it("replaces a waiting gate with its re-run once the operator answers", () => {
    const trace = build([span("g1", "gate", { status: "waiting" }), span("g2", "gate", { status: "running" })]);

    expect(trace.order).toEqual(["g2"]);
    expect(activeSpan(trace)?.id).toBe("g2");
  });

  it("lays the graph top to bottom with each node's children in a column to its right", () => {
    const trace = build([
      span("r", "recall"),
      span("s", "execute_step"),
      span("f1", "fara", { parent: "s", kind: "model" }),
      span("f2", "fara", { parent: "s", kind: "model", status: "running" }),
      span("z", "finalize", { status: "running" }),
    ]);

    const { nodes, edges } = layoutTrace(trace);
    const at = Object.fromEntries(nodes.map((node) => [node.id, node]));

    expect(at.f1!.x).toBeGreaterThan(at.s!.x);
    expect(at.f1!.y).toBe(at.s!.y);
    expect(at.z!.y).toBeGreaterThan(at.f2!.y + at.f2!.height); // the next step waits below the step's column
    expect(edges.map((edge) => [edge.source, edge.target, edge.kind, edge.active])).toEqual([
      ["r", "s", "next", false],
      ["s", "f1", "child", false],
      ["f1", "f2", "next", true],
      ["s", "z", "next", true],
    ]);
  });

  it("folds a long column behind a placeholder until it is opened", () => {
    const turns = Array.from({ length: VISIBLE_CHILDREN + 3 }, (_, index) => span(`f${index}`, "fara", { parent: "s", kind: "model" }));
    const trace = build([span("s", "execute_task"), ...turns]);

    const folded = layoutTrace(trace);
    expect(folded.nodes.find((node) => node.id === "more:s")?.hidden).toBe(3);
    expect(folded.nodes.filter((node) => node.span?.name === "fara")).toHaveLength(VISIBLE_CHILDREN);

    const open = layoutTrace(trace, { openGroups: new Set(["s"]), expanded: new Set(["f0"]) });
    expect(open.nodes.filter((node) => node.span?.name === "fara")).toHaveLength(turns.length);
    expect(open.nodes.find((node) => node.id === "f0")!.height).toBeGreaterThan(open.nodes.find((node) => node.id === "f1")!.height);
  });
});

describe("loops", () => {
  it("numbers the rounds when the flow comes back to a node and marks those edges as loops", () => {
    const trace = build([
      span("p", "plan"),
      span("q1", "select_step"),
      span("e1", "execute_step"),
      span("q2", "select_step"),
      span("e2", "execute_step", { status: "running" }),
      span("f1", "fara", { parent: "e2", kind: "model" }),
      span("f2", "fara", { parent: "e2", kind: "model" }),
      span("f3", "fara", { parent: "e2", kind: "model", status: "running" }),
    ]);

    const { nodes, edges } = layoutTrace(trace);
    const at = Object.fromEntries(nodes.map((node) => [node.id, node]));

    expect([at.q1!.iteration, at.q2!.iteration, at.e2!.iteration]).toEqual([undefined, 2, 2]);
    expect(edges.filter((edge) => edge.loop).map((edge) => [edge.source, edge.target, edge.loop])).toEqual([
      ["e1", "q2", 2],
      ["q2", "e2", 2],
      ["f1", "f2", 2],
      ["f2", "f3", 3],
    ]);
    expect(at.e2!.loops).toBe(3); // the FARA loop inside the step
    expect(at.p!.loops).toBeUndefined();
  });
});
