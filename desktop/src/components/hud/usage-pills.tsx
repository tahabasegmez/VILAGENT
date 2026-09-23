"use client";

import { useEffect, useRef, useState } from "react";

import type { Usage } from "@/components/hud/use-operator";
import { cn } from "@/lib/utils";

/** "VLM · 14 req · 3.2k tok" and "LLM · …": model usage of the current run, counting up live. */
export function UsagePills({ vlm, llm }: { vlm: Usage; llm: Usage }) {
  return (
    <div className="fixed right-4 top-4 z-40 flex items-center gap-2">
      <Pill name="VLM" usage={vlm} tone="text-sky-300" />
      <Pill name="LLM" usage={llm} tone="text-fuchsia-300" />
    </div>
  );
}

function Pill({ name, usage, tone }: { name: string; usage: Usage; tone: string }) {
  const requests = useCountUp(usage.requests);
  const tokens = useCountUp(usage.tokens);
  const flash = useFlash(usage.requests + usage.tokens);
  return (
    <div key={flash} className={cn("hud-glass flex items-center gap-1.5 rounded-full px-3 py-1 font-mono text-[10px] tabular-nums text-zinc-300", flash > 0 && "hud-flash")}>
      <span className={cn("font-semibold tracking-[0.14em]", tone)}>{name}</span>
      <span className="text-zinc-600">·</span>
      <span>{requests} req</span>
      <span className="text-zinc-600">·</span>
      <span>{compact(tokens)} tok</span>
    </div>
  );
}

/** Animate a number from its previous value to the new one. */
export function useCountUp(target: number, duration = 600): number {
  const [value, setValue] = useState(target);
  const from = useRef(target);
  useEffect(() => {
    const start = performance.now();
    const origin = from.current;
    let frame = 0;
    const tick = (now: number) => {
      const progress = Math.min(1, (now - start) / duration);
      const eased = 1 - (1 - progress) ** 3;
      const next = Math.round(origin + (target - origin) * eased);
      from.current = next;
      setValue(next);
      if (progress < 1) frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [target, duration]);
  return value;
}

/** A counter that changes whenever `value` grows, to restart the flash animation. */
function useFlash(value: number): number {
  const [count, setCount] = useState(0);
  const last = useRef(value);
  useEffect(() => {
    if (value > last.current) setCount((current) => current + 1);
    last.current = value;
  }, [value]);
  return count;
}

export function compact(value: number): string {
  if (value < 1000) return String(value);
  if (value < 1_000_000) return `${(value / 1000).toFixed(value < 10_000 ? 1 : 0)}k`;
  return `${(value / 1_000_000).toFixed(1)}M`;
}
