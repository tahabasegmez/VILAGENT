"use client";

import { Handle, Position, type Node, type NodeProps } from "@xyflow/react";
import { Brain, BrainCircuit, Check, ChevronsUp, Loader2, PauseCircle, RotateCw, X } from "lucide-react";

import type { TraceSpan } from "@/core/computer-use";
import { cn } from "@/lib/utils";

export type TraceNodeData = {
  span?: TraceSpan;
  /** For the fold placeholder: how many earlier children it hides. */
  hidden?: number;
  /** Which round of this node this is (the flow came back to it). */
  iteration?: number;
  /** How many rounds this node's own loop ran (FARA's turns inside a step). */
  loops?: number;
  expanded: boolean;
  onToggle: () => void;
};

export type TraceFlowNode = Node<TraceNodeData, "trace">;

const TONE: Record<TraceSpan["status"], string> = {
  running: "hud-running border-fuchsia-400/80 bg-[#1a0b2b]/90",
  done: "border-emerald-400/25 bg-[#120b1d]/85",
  error: "border-rose-500/70 bg-[#220813]/90 shadow-[0_0_22px_-6px_rgba(251,59,107,0.85)]",
  waiting: "border-amber-400/70 bg-[#1f1608]/90 shadow-[0_0_22px_-6px_rgba(251,191,36,0.8)]",
};

const ICON: Record<TraceSpan["status"], React.ReactNode> = {
  running: <Loader2 className="size-3 animate-spin text-fuchsia-300" />,
  done: <Check className="size-3 text-emerald-300/90" />,
  error: <X className="size-3 text-rose-400" />,
  waiting: <PauseCircle className="size-3 text-amber-300" />,
};

/** One span of the run: a graph node, a FARA turn or a model call. A click (the graph's onNodeClick) reads it all. */
export function TraceNode({ data, width, height }: NodeProps<TraceFlowNode>) {
  const { span, hidden, iteration, loops, expanded, onToggle } = data;
  if (!span) {
    return (
      <div title="Show the earlier turns" style={{ width, height }} className="hud-hover flex cursor-pointer items-center justify-center gap-1.5 rounded-full border border-dashed border-sky-400/40 bg-sky-500/5 font-mono text-[9.5px] uppercase tracking-[0.14em] text-sky-200/80">
        <Handles />
        <ChevronsUp className="size-3" /> {hidden} earlier
      </div>
    );
  }

  const reading = span.memory.length > 0;
  const detail = span.kind === "node" ? (span.label ?? span.output) : span.output;
  return (
    <div
      role="button"
      tabIndex={0}
      onKeyDown={(event) => event.key === "Enter" && onToggle()}
      style={{ width, height }}
      className={cn("hud-hover relative flex cursor-pointer flex-col gap-1 overflow-hidden rounded-lg border px-2.5 py-2 backdrop-blur-md", TONE[span.status])}
    >
      <Handles />
      <div className="flex items-center gap-1.5">
        {ICON[span.status]}
        <span className={cn("truncate font-mono text-[10px] font-semibold tracking-wide", span.kind === "node" ? "text-fuchsia-100" : "text-sky-200")}>{span.kind === "node" ? span.name : (span.label ?? span.name)}</span>
        <span className="font-mono text-[8.5px] uppercase tracking-[0.16em] text-zinc-500">{span.kind === "node" ? "node" : span.name}</span>
        {iteration && (
          <span title={`The run came back to this node: round ${iteration}`} className="flex items-center gap-0.5 rounded-full border border-sky-400/40 bg-sky-500/10 px-1 font-mono text-[8.5px] text-sky-200">
            <RotateCw className="size-2.5" /> {iteration}
          </span>
        )}
        {loops && (
          <span title={`This node loops: ${loops} rounds so far`} className="flex items-center gap-0.5 rounded-full border border-violet-400/40 bg-violet-500/10 px-1 font-mono text-[8.5px] text-violet-200">
            <RotateCw className="size-2.5" /> loop ×{loops}
          </span>
        )}
        {reading && (
          <span
            title="Memory used here"
            className={cn("ml-auto flex items-center gap-0.5 rounded-full border border-sky-400/40 bg-sky-500/10 px-1.5 font-mono text-[8.5px] text-sky-200", span.status === "running" && "hud-memory-reading")}
          >
            <Brain className="size-2.5" /> {span.memory.length}
          </span>
        )}
      </div>

      {expanded ? (
        <div className="nowheel nodrag min-h-0 flex-1 cursor-text select-text space-y-1.5 overflow-y-auto pr-1 text-[10.5px] leading-snug" onClick={(event) => event.stopPropagation()}>
          {span.kind === "node" && span.label && <p className="text-zinc-200">{span.label}</p>}
          {span.output && <p className="whitespace-pre-wrap font-mono text-[10px] text-zinc-300">{span.output}</p>}
          {span.thinking && <p className="whitespace-pre-wrap italic text-violet-300/80">{span.thinking}</p>}
          {reading && (
            <ul className="space-y-0.5 border-t border-sky-400/15 pt-1 text-sky-200/80">
              {span.memory.map((item) => (
                <li key={item}>· {item}</li>
              ))}
            </ul>
          )}
        </div>
      ) : (
        <>
          {detail && <p className={cn("text-[10.5px] leading-snug text-zinc-300", span.kind === "node" ? "line-clamp-2" : "truncate font-mono text-[10px]")}>{detail}</p>}
          {span.thinking && (
            <p className="flex items-center gap-1 truncate text-[10px] italic text-violet-300/70">
              <BrainCircuit className="size-2.5 flex-none" />
              <span className="truncate">{span.thinking}</span>
            </p>
          )}
        </>
      )}
    </div>
  );
}

/**
 * Invisible anchors: the main flow enters at the top and leaves at the bottom, child columns hang
 * off the right, and a loop leaves and re-enters on the left so its arrow curves back visibly.
 */
function Handles() {
  return (
    <>
      <Handle type="target" position={Position.Top} id="in" isConnectable={false} />
      <Handle type="source" position={Position.Bottom} id="out" isConnectable={false} />
      <Handle type="source" position={Position.Right} id="child" isConnectable={false} />
      <Handle type="target" position={Position.Left} id="parent" isConnectable={false} style={{ top: "30%" }} />
      <Handle type="source" position={Position.Left} id="loop" isConnectable={false} style={{ top: "70%" }} />
    </>
  );
}
