import type { TraceSpan } from "./types";

/** The run's spans by id, in the order they started. */
export type Trace = { spans: Record<string, TraceSpan>; order: string[] };

export const EMPTY_TRACE: Trace = { spans: {}, order: [] };

/**
 * Add a span or update it. A graph node that runs again after waiting (an approval gate is
 * re-run by LangGraph once the operator answers) takes the place of its waiting copy.
 */
export function applyTrace(trace: Trace, span: TraceSpan): Trace {
  if (trace.spans[span.id]) return { spans: { ...trace.spans, [span.id]: span }, order: trace.order };
  const spans = { ...trace.spans, [span.id]: span };
  let order = trace.order;
  if (span.parent === null) {
    const previous = order.findLast((id) => spans[id]?.parent === null);
    const waiting = previous ? spans[previous] : undefined;
    if (previous && waiting?.status === "waiting" && waiting.name === span.name) {
      delete spans[previous];
      order = order.filter((id) => id !== previous);
    }
  }
  return { spans, order: [...order, span.id] };
}

/** The span that is running deepest in the tree right now (what the agent is doing). */
export function activeSpan(trace: Trace): TraceSpan | null {
  const id = trace.order.findLast((key) => trace.spans[key]?.status === "running");
  return id ? (trace.spans[id] ?? null) : null;
}

// --- layout -----------------------------------------------------------------------------
// The graph reads top to bottom; a node's children (FARA's turns inside a step, the supervisor
// inside a turn) form a column to its right. Sizes are fixed, so the layout is exact.
//
// The run graph is cyclic: plan steps come back to select_step, a replan goes back into the
// plan, and FARA repeats its turn until the step is done. The trace lists those repeats one
// after another, so a node that runs again carries its round number and the edge into it is
// marked as a loop, which the graph draws as an arrow curving back.

export const VISIBLE_CHILDREN = 4;
const WIDTH = [264, 224];
const HEIGHT = { node: 88, model: 72, action: 72, more: 30, expanded: 260 };
const COLUMN_GAP = 56;
const ROW_GAP = 26;

export type LaidNode = {
  id: string;
  x: number;
  y: number;
  width: number;
  height: number;
  depth: number;
  /** The span, or none for the "N earlier" placeholder of a folded column. */
  span?: TraceSpan;
  /** How many earlier children the placeholder stands for. */
  hidden?: number;
  /** Which round of this node this is (2 and up mean the flow came back to it). */
  iteration?: number;
  /** For a parent: how many rounds its children loop for (the FARA loop inside a step). */
  loops?: number;
  /** For the placeholder: the span whose children it folds. */
  group?: string;
};

export type LaidEdge = {
  id: string;
  source: string;
  target: string;
  kind: "next" | "child";
  active: boolean;
  /** The flow came back to a node it already ran: drawn as a loop, labelled with the round. */
  loop?: number;
};

export function layoutTrace(trace: Trace, { expanded = new Set<string>(), openGroups = new Set<string>() }: { expanded?: Set<string>; openGroups?: Set<string> } = {}) {
  const nodes: LaidNode[] = [];
  const edges: LaidEdge[] = [];
  const children = new Map<string, string[]>();
  const roots: string[] = [];
  for (const id of trace.order) {
    const parent = trace.spans[id]?.parent;
    if (parent && trace.spans[parent]) children.set(parent, [...(children.get(parent) ?? []), id]);
    else roots.push(id);
  }

  const link = (source: string, target: string, kind: LaidEdge["kind"], loop?: number) =>
    edges.push({ id: `${source}->${target}`, source, target, kind, active: trace.spans[target]?.status === "running", ...(loop ? { loop } : {}) });

  /** Round number per node name within one column: 1 the first time, 2 when the flow returns. */
  const rounds = (ids: string[]): Map<string, number> => {
    const seen = new Map<string, number>();
    const byId = new Map<string, number>();
    for (const id of ids) {
      const name = trace.spans[id]?.name;
      if (!name) continue;
      const round = (seen.get(name) ?? 0) + 1;
      seen.set(name, round);
      byId.set(id, round);
    }
    return byId;
  };

  /** Place a column of siblings from (x, y); returns the bottom of the column. */
  const place = (ids: string[], x: number, y: number, depth: number, parent: string | null): number => {
    const width = WIDTH[Math.min(depth, WIDTH.length - 1)] ?? 224;
    const hidden = parent && !openGroups.has(parent) ? Math.max(0, ids.length - VISIBLE_CHILDREN) : 0;
    const iterations = rounds(ids);
    let previous: string | null = null;
    let cursor = y;
    if (parent && hidden > 0) {
      previous = `more:${parent}`;
      nodes.push({ id: previous, x, y: cursor, width, height: HEIGHT.more, depth, hidden, group: parent });
      link(parent, previous, "child");
      cursor += HEIGHT.more + ROW_GAP;
    }
    for (const id of ids.slice(hidden)) {
      const span = trace.spans[id];
      if (!span) continue;
      const height = expanded.has(id) ? HEIGHT.expanded : HEIGHT[span.kind];
      const iteration = iterations.get(id) ?? 1;
      const node: LaidNode = { id, x, y: cursor, width, height, depth, span, ...(iteration > 1 ? { iteration } : {}) };
      nodes.push(node);
      if (previous) link(previous, id, "next", iteration > 1 ? iteration : undefined);
      else if (parent) link(parent, id, "child");
      const kids = children.get(id);
      const loops = kids ? Math.max(...[...rounds(kids).values()]) : 1;
      if (loops > 1) node.loops = loops;
      const bottom = kids ? place(kids, x + width + COLUMN_GAP, cursor, depth + 1, id) : cursor + height;
      cursor = Math.max(cursor + height, bottom) + ROW_GAP;
      previous = id;
    }
    return cursor - ROW_GAP;
  };

  place(roots, 0, 0, 0, null);
  return { nodes, edges };
}
