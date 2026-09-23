"use client";

import { useEffect, useRef } from "react";

const PARTICLES = 46;

/** The near-black stage: a faint grid, film noise and a few slowly drifting motes. */
export function Backdrop() {
  const canvasRef = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    const context = canvas?.getContext("2d");
    if (!canvas || !context) return;
    const still = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const motes = Array.from({ length: PARTICLES }, () => ({
      x: Math.random(),
      y: Math.random(),
      r: 0.4 + Math.random() * 1.3,
      dx: (Math.random() - 0.5) * 0.00012,
      dy: -0.00004 - Math.random() * 0.0001,
      hue: Math.random() < 0.7 ? "192,132,252" : "56,189,248",
      alpha: 0.15 + Math.random() * 0.35,
    }));
    let frame = 0;

    const resize = () => {
      canvas.width = window.innerWidth * devicePixelRatio;
      canvas.height = window.innerHeight * devicePixelRatio;
    };
    const draw = () => {
      context.clearRect(0, 0, canvas.width, canvas.height);
      for (const mote of motes) {
        if (!still) {
          mote.x = (mote.x + mote.dx + 1) % 1;
          mote.y = (mote.y + mote.dy + 1) % 1;
        }
        context.beginPath();
        context.arc(mote.x * canvas.width, mote.y * canvas.height, mote.r * devicePixelRatio, 0, Math.PI * 2);
        context.fillStyle = `rgba(${mote.hue},${mote.alpha})`;
        context.shadowColor = `rgba(${mote.hue},0.9)`;
        context.shadowBlur = 8;
        context.fill();
      }
      if (!still) frame = requestAnimationFrame(draw);
    };

    resize();
    draw();
    window.addEventListener("resize", resize);
    return () => {
      cancelAnimationFrame(frame);
      window.removeEventListener("resize", resize);
    };
  }, []);

  return (
    <div aria-hidden className="pointer-events-none absolute inset-0 overflow-hidden">
      <div className="absolute inset-0 [background:radial-gradient(55%_40%_at_50%_0%,rgba(168,85,247,0.16),transparent_70%),radial-gradient(40%_35%_at_100%_100%,rgba(56,189,248,0.08),transparent_70%),radial-gradient(35%_30%_at_0%_80%,rgba(217,70,239,0.07),transparent_70%)]" />
      <div className="hud-grid absolute inset-0" />
      <canvas ref={canvasRef} className="absolute inset-0 size-full" />
      <div className="hud-noise absolute inset-0" />
    </div>
  );
}
