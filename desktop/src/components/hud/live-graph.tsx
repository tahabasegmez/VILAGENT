"use client";

import { ReactFlow, ReactFlowProvider, useReactFlow } from "@xyflow/react";
import { Crosshair, Hexagon, Minus, Plus } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import { FlowEdge, type TraceFlowEdge } from "@/components/hud/flow-edge";
import { TraceNode, type TraceFlowNode } from "@/components/hud/trace-node";
import { layoutTrace, type Trace } from "@/core/computer-use";

const NODE_TYPES = { trace: TraceNode };
const EDGE_TYPES = { flow: FlowEdge };
// After the operator pans or zooms, leave the camera alone for a while.
const MANUAL_HOLD_MS = 8000;
const FOCUS_NODES = 3;

export type FocusRequest = { stepId: string; at: number } | null;

/** The run as a live graph: nodes appear as they run and the camera follows the newest. */
export function LiveGraph(props: { trace: Trace; focus: FocusRequest }) {
  return (
    <ReactFlowProvider>
      <Graph {...props} />
    </ReactFlowProvider>
  );
}

function Graph({ trace, focus }: { trace: Trace; focus: FocusRequest }) {
  const flow = useReactFlow<TraceFlowNode, TraceFlowEdge>();
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [openGroups, setOpenGroups] = useState<Set<string>>(new Set());
  const heldUntil = useRef(0);

  // A new run starts with a clean slate.
  const firstId = trace.order[0];
  useEffect(() => {
    setExpanded(new Set());
    setOpenGroups(new Set());
    heldUntil.current = 0;
  }, [firstId]);

  const { nodes, edges } = useMemo(() => {
    const laid = layoutTrace(trace, { expanded, openGroups });
    const toggle = (set: Set<string>, id: string) => {
      const next = new Set(set);
      if (!next.delete(id)) next.add(id);
      return next;
    };
    return {
      nodes: laid.nodes.map(
        (node): TraceFlowNode => ({
          id: node.id,
          type: "trace",
          position: { x: node.x, y: node.y },
          width: node.width,
          height: node.height,
          draggable: false,
          data: {
            span: node.span,
            hidden: node.hidden,
            iteration: node.iteration,
            loops: node.loops,
            expanded: expanded.has(node.id),
            onToggle: () => (node.group ? setOpenGroups((set) => toggle(set, node.group!)) : setExpanded((set) => toggle(set, node.id))),
          },
        }),
      ),
      edges: laid.edges.map(
        (edge): TraceFlowEdge => ({
          id: edge.id,
          source: edge.source,
          target: edge.target,
          sourceHandle: edge.loop ? "loop" : edge.kind === "child" ? "child" : "out",
          targetHandle: edge.loop || edge.kind === "child" ? "parent" : "in",
          type: "flow",
          data: { active: edge.active, loop: edge.loop },
        }),
      ),
    };
  }, [trace, expanded, openGroups]);

  // Follow the newest nodes unless the operator is looking around.
  const newest = trace.order.at(-1);
  useEffect(() => {
    if (!newest || Date.now() < heldUntil.current) return;
    const shown = new Set(nodes.map((node) => node.id));
    const recent = trace.order.filter((id) => shown.has(id)).slice(-FOCUS_NODES);
    const frame = requestAnimationFrame(() => void flow.fitView({ nodes: recent.map((id) => ({ id })), duration: 600, padding: 0.4, maxZoom: 1.15 }));
    return () => cancelAnimationFrame(frame);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [newest, nodes.length]);

  // The plan rail asked to show a step: its latest execution node.
  useEffect(() => {
    if (!focus) return;
    const id = trace.order.findLast((key) => trace.spans[key]?.meta.step_id === focus.stepId && trace.spans[key]?.name.startsWith("execute"));
    if (!id) return;
    heldUntil.current = Date.now() + MANUAL_HOLD_MS;
    void flow.fitView({ nodes: [{ id }], duration: 600, padding: 0.6, maxZoom: 1.2 });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focus]);

  const recenter = () => {
    heldUntil.current = 0;
    void flow.fitView({ duration: 500, padding: 0.25, maxZoom: 1.1 });
  };

  return (
    <div className="hud-flow absolute inset-0">
      {trace.order.length === 0 ? (
        <div className="pointer-events-none absolute inset-0 grid place-items-center">
          <div className="flex flex-col items-center gap-3 text-center">
            <Hexagon className="size-10 animate-pulse text-fuchsia-400/60 drop-shadow-[0_0_12px_rgba(168,85,247,0.8)]" strokeWidth={1} />
            <p className="font-mono text-[10px] uppercase tracking-[0.3em] text-fuchsia-200/60">Awaiting a task</p>
            <p className="max-w-xs text-[11px] text-zinc-500">The run graph draws itself here as the agent recalls, plans and acts.</p>
          </div>
        </div>
      ) : (
        <ReactFlow
          nodes={nodes}
          edges={edges}
          nodeTypes={NODE_TYPES}
          edgeTypes={EDGE_TYPES}
          nodesConnectable={false}
          elementsSelectable={false}
          minZoom={0.2}
          maxZoom={2}
          proOptions={{ hideAttribution: true }}
          onNodeClick={(_, node) => node.data.onToggle()}
          onMoveStart={(event) => {
            if (event) heldUntil.current = Date.now() + MANUAL_HOLD_MS;
          }}
        />
      )}
      {trace.order.length > 0 && (
        <div className="absolute bottom-5 right-4 z-30 flex flex-col gap-1">
          <CameraButton title="Zoom in" onClick={() => void flow.zoomIn({ duration: 200 })}>
            <Plus className="size-3" />
          </CameraButton>
          <CameraButton title="Zoom out" onClick={() => void flow.zoomOut({ duration: 200 })}>
            <Minus className="size-3" />
          </CameraButton>
          <CameraButton title="Show the whole run and follow it again" onClick={recenter}>
            <Crosshair className="size-3" />
          </CameraButton>
        </div>
      )}
    </div>
  );
}

function CameraButton({ title, onClick, children }: { title: string; onClick: () => void; children: React.ReactNode }) {
  return (
    <button type="button" title={title} onClick={onClick} className="hud-glass hud-hover grid size-7 place-items-center rounded-md text-fuchsia-200/80">
      {children}
    </button>
  );
}
