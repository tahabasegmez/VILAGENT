/** A small floating badge: a thin neon hexagon around a node dot. */
export function Logo() {
  return (
    <div className="pointer-events-none fixed left-4 top-4 z-40 flex items-center gap-2 opacity-70">
      <svg viewBox="0 0 32 32" className="size-7 drop-shadow-[0_0_6px_rgba(168,85,247,0.9)]" aria-hidden>
        <polygon points="16,2.5 28,9.25 28,22.75 16,29.5 4,22.75 4,9.25" fill="rgba(168,85,247,0.06)" stroke="#c084fc" strokeWidth="1" />
        <polygon points="16,9 22,12.5 22,19.5 16,23 10,19.5 10,12.5" fill="none" stroke="#38bdf8" strokeWidth="0.6" opacity="0.7" />
        <circle cx="16" cy="16" r="1.8" fill="#d946ef" />
      </svg>
      <span className="font-mono text-[9px] font-medium uppercase tracking-[0.34em] text-fuchsia-200/70">Vilagent</span>
    </div>
  );
}
