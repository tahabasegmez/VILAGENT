"use client";

import { ShieldAlert } from "lucide-react";
import { useEffect, useState } from "react";

import type { ApprovalScope, PendingApproval } from "@/core/computer-use";
import { cn } from "@/lib/utils";

export type ApprovalAnswer = (id: string, approve: boolean, scope?: ApprovalScope) => void;

/** Everything the run is waiting for the operator to decide, centred over the graph. No answer in time counts as Deny. */
export function ApprovalDialog({ approvals, onAnswer, className }: { approvals: PendingApproval[]; onAnswer: ApprovalAnswer; className?: string }) {
  if (approvals.length === 0) return null;
  return (
    <div className={cn("fixed inset-0 z-[60] grid place-items-center bg-black/35 p-4 backdrop-blur-[2px] duration-200 animate-in fade-in", className)}>
      <div className="flex w-[min(440px,94vw)] flex-col gap-2">
        {approvals.map((approval) => (
          <ApprovalCard key={approval.id} approval={approval} onAnswer={onAnswer} />
        ))}
      </div>
    </div>
  );
}

function ApprovalCard({ approval, onAnswer }: { approval: PendingApproval; onAnswer: ApprovalAnswer }) {
  const secondsLeft = useSecondsLeft(approval.expires_at);
  const question = approval.kind === "step" ? `Run this ${approval.level ?? ""} risk step?` : "Allow this action?";
  return (
    <div role="alertdialog" aria-label={question} className="rounded-xl border border-amber-400/60 bg-[#1d1407]/90 p-3.5 text-[11.5px] text-amber-50 shadow-[0_0_36px_-6px_rgba(251,191,36,0.7)] backdrop-blur-md duration-200 animate-in zoom-in-95">
      <div className="flex items-center gap-2 font-semibold">
        <ShieldAlert className="size-4 flex-none text-amber-300" />
        <span className="flex-1">{question}</span>
        <span className="font-mono text-[11px] text-amber-200/80">{secondsLeft}s</span>
      </div>
      <p className="mt-1.5 break-words text-zinc-100">{approval.title}</p>
      {approval.reasons.length > 0 && (
        <ul className="mt-1 list-disc space-y-0.5 pl-4 text-[11px] text-amber-100/80">
          {approval.reasons.map((reason) => (
            <li key={reason}>{reason}</li>
          ))}
        </ul>
      )}
      <div className="mt-2.5 flex flex-wrap gap-1.5">
        <AnswerButton tone="allow" onClick={() => onAnswer(approval.id, true, "once")}>
          {approval.kind === "action" ? "Allow once" : "Allow"}
        </AnswerButton>
        {approval.kind === "action" && (
          <AnswerButton tone="allow" onClick={() => onAnswer(approval.id, true, "step")}>
            Allow for this step
          </AnswerButton>
        )}
        <AnswerButton tone="deny" onClick={() => onAnswer(approval.id, false)}>
          Deny
        </AnswerButton>
      </div>
      <p className="mt-1.5 text-[10px] text-amber-200/60">No answer counts as Deny, and a denied step ends the task.</p>
    </div>
  );
}

function AnswerButton({ tone, onClick, children }: { tone: "allow" | "deny"; onClick: () => void; children: string }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "h-7 rounded-lg border px-2.5 text-[11px] font-medium transition-colors",
        tone === "allow" ? "border-emerald-400/40 bg-emerald-500/15 text-emerald-100 hover:bg-emerald-500/25" : "border-red-400/40 bg-red-500/15 text-red-100 hover:bg-red-500/25",
      )}
    >
      {children}
    </button>
  );
}

function useSecondsLeft(expiresAt: string): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);
  return Math.max(0, Math.round((Date.parse(expiresAt) - now) / 1000));
}
