"use client";

import { Check, XCircle } from "lucide-react";
import { type ReactNode } from "react";


export function Empty({ children }: { children: ReactNode }) {
  return <p className="rounded-lg border border-dashed border-white/10 bg-white/[0.02] p-2.5 text-center text-[11px] italic text-zinc-500">{children}</p>;
}

export function Select({ value, options, onChange, label }: { value: string; options: { value: string; label: string }[]; onChange: (value: string) => void; label: string }) {
  return (
    <select
      aria-label={label}
      value={value}
      onChange={(event) => onChange(event.target.value)}
      className="h-7 w-full rounded-lg border border-white/10 bg-[#120b1a] px-2 text-[11px] text-zinc-200 outline-none transition-colors hover:border-fuchsia-400/30 focus:border-fuchsia-400/50"
    >
      {options.map((option) => (
        <option key={option.value} value={option.value}>
          {option.label}
        </option>
      ))}
    </select>
  );
}

function renderInline(text: string, keyBase: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  const regex = /(\*\*[^*]+\*\*|\*[^*]+\*)/g;
  let last = 0;
  let index = 0;
  let match: RegExpExecArray | null;
  while ((match = regex.exec(text)) !== null) {
    if (match.index > last) nodes.push(text.slice(last, match.index));
    const token = match[0];
    if (token.startsWith("**")) nodes.push(<strong key={`${keyBase}-${index}`} className="font-semibold text-zinc-100">{token.slice(2, -2)}</strong>);
    else nodes.push(<em key={`${keyBase}-${index}`} className="text-zinc-400">{token.slice(1, -1)}</em>);
    last = match.index + token.length;
    index++;
  }
  if (last < text.length) nodes.push(text.slice(last));
  return nodes;
}

/** The small Markdown subset the agent's replies use: bold, italics, ✓/✗ lines, numbered steps, notes. */
export function RichText({ text }: { text: string }) {
  return (
    <div className="space-y-1 text-[11.5px] leading-relaxed">
      {text.split("\n").map((line, idx) => {
        const trimmed = line.trim();
        if (!trimmed) return <div key={idx} className="h-1" />;
        const headline = /^([^*\s][^*]*\s)?\*\*(.+)\*\*$/.exec(trimmed);
        if (headline) {
          return <p key={idx} className="text-[12px] font-semibold text-zinc-100">{headline[1] ?? ""}{renderInline(headline[2] ?? "", `h${idx}`)}</p>;
        }
        if (trimmed.startsWith("✓ ") || trimmed.startsWith("✗ ")) {
          const ok = trimmed.startsWith("✓ ");
          return (
            <div key={idx} className="flex items-start gap-2">
              {ok ? <Check className="mt-0.5 size-3.5 shrink-0 text-emerald-400" /> : <XCircle className="mt-0.5 size-3.5 shrink-0 text-red-400" />}
              <span>{renderInline(trimmed.slice(2), `c${idx}`)}</span>
            </div>
          );
        }
        const numbered = /^(\d+)\.\s+(.*)$/.exec(trimmed);
        if (numbered) {
          return (
            <div key={idx} className="flex items-start gap-2">
              <span className="mt-px font-mono text-[11px] font-semibold text-fuchsia-300/80">{numbered[1]}.</span>
              <span>{renderInline(numbered[2] ?? "", `n${idx}`)}</span>
            </div>
          );
        }
        const note = /^_(.+)_$/.exec(trimmed);
        if (note) return <p key={idx} className="text-[11px] italic text-zinc-500">{renderInline(note[1] ?? "", `i${idx}`)}</p>;
        return <p key={idx}>{renderInline(trimmed, `p${idx}`)}</p>;
      })}
    </div>
  );
}
