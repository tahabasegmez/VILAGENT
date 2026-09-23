"use client";

import { BaseEdge, getSmoothStepPath, type Edge, type EdgeProps } from "@xyflow/react";

export type FlowEdgeData = { active: boolean; loop?: number };
export type TraceFlowEdge = Edge<FlowEdgeData, "flow">;

// How far a loop arrow bulges out to the left of the column it returns into.
const BULGE = 54;

/**
 * A connection line. Into the running node it glows and carries travelling light; when the flow
 * comes back to a node it already ran (a plan step's loop, a replan, FARA's turns) it curves out
 * to the left like a flowchart's loop arrow and says which round this is.
 */
export function FlowEdge({ id, sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition, data }: EdgeProps<TraceFlowEdge>) {
  const loop = data?.loop;
  const [stepPath] = getSmoothStepPath({ sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition, borderRadius: 10 });
  // Both ends sit on the left edge, so the curve leaves the node, swings out and comes back in.
  const left = Math.min(sourceX, targetX) - BULGE;
  const path = loop ? `M ${sourceX},${sourceY} C ${left},${sourceY} ${left},${targetY} ${targetX},${targetY}` : stepPath;
  const active = data?.active;

  return (
    <>
      {active ? (
        <>
          <BaseEdge id={id} path={path} style={{ stroke: "rgba(217,70,239,0.35)", strokeWidth: 3, filter: "blur(2px)" }} />
          <path d={path} fill="none" stroke="#c084fc" strokeWidth={1.4} className="hud-edge-flow" />
          <circle r={2.4} fill="#38bdf8" style={{ filter: "drop-shadow(0 0 4px #38bdf8)" }}>
            <animateMotion dur="1.4s" repeatCount="indefinite" path={path} />
          </circle>
        </>
      ) : (
        <BaseEdge id={id} path={path} style={loop ? { stroke: "rgba(56,189,248,0.4)", strokeWidth: 1.2, strokeDasharray: "4 4" } : { stroke: "rgba(192,132,252,0.28)", strokeWidth: 1 }} />
      )}
      {loop && (
        <text
          x={left + 13}
          y={(sourceY + targetY) / 2}
          textAnchor="middle"
          dominantBaseline="middle"
          fill="#7dd3fc"
          style={{ fontSize: 9, fontFamily: "var(--font-agentic-mono, monospace)", letterSpacing: "0.08em", pointerEvents: "none" }}
        >
          ↺ {loop}
        </text>
      )}
    </>
  );
}
