// Agent tab (design doc 7.10): every roster-agent task as a live step list: plan line, tool
// called, short result, and the waiting state ("Waiting for Arun's reply, 2 min"). Tasks that
// switched to the fixed-code workflow are labelled "fallback path" with the reason.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api, type AgentTaskOut, type Nurse, type TraceStep } from "../../api/client";
import QueryState from "../common/QueryState";

const WORKFLOW: Record<string, string> = {
  sick_call: "Sick call",
  demand_change: "Demand change",
  conflict: "Conflict",
  leave: "Leave request",
  swap: "Swap",
  new_period: "New period",
};

const STATUS: Record<string, { text: string; tone: string }> = {
  running: { text: "running", tone: "bg-indigo-100 text-indigo-800" },
  waiting_approval: { text: "waiting for you", tone: "bg-amber-100 text-amber-900" },
  waiting_reply: { text: "waiting for a reply", tone: "bg-sky-100 text-sky-900" },
  done: { text: "done", tone: "bg-emerald-100 text-emerald-800" },
  escalated: { text: "escalated", tone: "bg-red-100 text-red-800" },
  failed: { text: "failed", tone: "bg-red-100 text-red-800" },
};

function since(iso: string | null | undefined, now: number): string {
  if (!iso) return "";
  const s = Math.max(0, Math.round((now - Date.parse(iso)) / 1000));
  return s < 60 ? `${s} s` : `${Math.floor(s / 60)} min`;
}

function clock(totalS: number): string {
  const s = Math.max(0, Math.round(totalS));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

function CascadeLine({ t, nurses, now }: { t: AgentTaskOut; nurses: Nurse[]; now: number }) {
  const c = t.cascade;
  if (!c) return null;
  const name = (id: string) => nurses.find((n) => n.id === id)?.name ?? id;
  const parts = c.order.map((id) => {
    const st = c.asked[id];
    if (!st) return null;
    if (st === "waiting" && c.deadline_at) {
      const left = (Date.parse(c.deadline_at) - now) / 1000;
      const total = c.since ? (Date.parse(c.deadline_at) - Date.parse(c.since)) / 1000 : left;
      return `asked ${name(id)} (waiting ${clock(left)} / ${clock(total)})`;
    }
    return `asked ${name(id)} (${st})`;
  });
  return (
    <p className="mt-1 text-xs text-slate-700" data-testid="cascade-progress">
      Plan: {c.order.map(name).join(" → ")}
      {parts.filter(Boolean).map((p) => ` · ${p}`).join("")}
      {!c.approved && " · waiting for your approval"}
    </p>
  );
}

function Step({ s }: { s: TraceStep }) {
  const icon: Record<string, string> = {
    tool: "→",
    wait: "⏸",
    resumed: "▶",
    error: "!",
    fallback: "⤳",
    done: "✓",
    escalated: "⚠",
  };
  const tone =
    s.kind === "error" || s.kind === "escalated"
      ? "text-red-700"
      : s.kind === "fallback"
        ? "text-amber-700"
        : s.kind === "done"
          ? "text-emerald-700"
          : "text-slate-700";
  return (
    <li className={`flex gap-2 text-[11px] ${tone}`}>
      <span className="w-3 shrink-0 text-center text-slate-400">{icon[s.kind] ?? "·"}</span>
      <div className="min-w-0 flex-1">
        {s.plan && s.kind !== "resumed" && <p className="italic text-slate-500">{s.plan}</p>}
        <p className="break-words">
          {s.tool && <span className="font-mono font-semibold">{s.tool}</span>}
          {s.tool && s.brief ? " · " : ""}
          {s.brief}
        </p>
      </div>
      {s.mode === "fallback" && s.kind !== "fallback" && (
        <span className="shrink-0 rounded bg-amber-50 px-1 text-[9px] text-amber-700">fixed code</span>
      )}
    </li>
  );
}

function Task({ t, nurses, now }: { t: AgentTaskOut; nurses: Nurse[]; now: number }) {
  const [open, setOpen] = useState(t.status !== "done");
  const qc = useQueryClient();
  const st = STATUS[t.status] ?? { text: t.status, tone: "bg-slate-100 text-slate-700" };
  const who = nurses.find((n) => n.id === t.waiting_on)?.name ?? t.waiting_on;
  const w = t.waiting as { kind?: string; since?: string; summary?: string } | null;
  const settled = () => {
    void qc.invalidateQueries({ queryKey: ["agent"] });
    void qc.invalidateQueries({ queryKey: ["inbox"] });
  };
  const skip = useMutation({ mutationFn: () => api.skipTask(t.id), onSettled: settled });
  const cancel = useMutation({ mutationFn: () => api.cancelTask(t.id), onSettled: settled });
  return (
    <article className="rounded-lg border border-slate-200 bg-white p-3 shadow-sm" data-testid="agent-task">
      <header className="flex flex-wrap items-center gap-2">
        <button onClick={() => setOpen((o) => !o)} className="text-sm font-semibold hover:underline">
          {WORKFLOW[t.workflow] ?? t.workflow}
        </button>
        <span className={`rounded px-1.5 py-0.5 text-[10px] font-semibold ${st.tone}`}>{st.text}</span>
        {t.mode === "fallback" && (
          <span className="rounded bg-amber-100 px-1.5 py-0.5 text-[10px] font-semibold text-amber-900" title={t.fallback_reason ?? ""}>
            fallback path
          </span>
        )}
        <span className="ml-auto font-mono text-[10px] text-slate-500">
          {t.id} · {t.trigger_ref}
        </span>
      </header>
      {t.status === "waiting_reply" && (
        <p className="mt-1 text-xs text-sky-800">
          Waiting for {who}'s reply, {since(w?.since ?? t.updated_at, now)}
        </p>
      )}
      {t.workflow === "sick_call" && t.status === "waiting_reply" && (
        <div className="mt-1 flex gap-2">
          <button
            onClick={() => skip.mutate()}
            disabled={skip.isPending || cancel.isPending}
            title="Treat this ask as timed out and ask the next nurse in the plan"
            className="rounded border border-slate-300 px-2 py-0.5 text-[11px] font-semibold text-slate-700 hover:bg-slate-50 disabled:opacity-60"
          >
            Skip to next
          </button>
          <button
            onClick={() => cancel.mutate()}
            disabled={skip.isPending || cancel.isPending}
            title="Withdraw the open ask and end this task without publishing"
            className="rounded border border-slate-300 px-2 py-0.5 text-[11px] font-semibold text-slate-700 hover:bg-slate-50 disabled:opacity-60"
          >
            Cancel task
          </button>
          {(skip.error ?? cancel.error) && (
            <span className="text-[11px] text-red-600">{(skip.error ?? cancel.error)?.message}</span>
          )}
        </div>
      )}
      {t.status === "waiting_approval" && w?.summary && (
        <p className="mt-1 rounded bg-amber-50 px-2 py-1 text-xs text-amber-900">
          Approval needed: {w.summary}
        </p>
      )}
      <CascadeLine t={t} nurses={nurses} now={now} />
      {t.outcome && <p className="mt-1 text-xs text-slate-600">Outcome: {t.outcome}</p>}
      {t.mode === "fallback" && t.fallback_reason && (
        <p className="mt-1 text-[11px] text-amber-800">Fallback because: {t.fallback_reason}</p>
      )}
      {open && (
        <ol className="mt-2 space-y-1 border-t border-slate-100 pt-2">
          {t.trace.map((s) => (
            <Step key={s.n} s={s} />
          ))}
        </ol>
      )}
      <p className="mt-1 text-[10px] text-slate-500">
        {t.counters.tool_calls ?? 0}/12 tool calls · {t.counters.messages ?? 0}/4 messages ·{" "}
        {((t.counters.model_ms ?? 0) / 1000).toFixed(1)} s model time
        {(t.counters.forbidden ?? 0) > 0 && ` · ${t.counters.forbidden} refused`}
      </p>
    </article>
  );
}

export default function AgentTab({ nurses }: { nurses: Nurse[] }) {
  const tasks = useQuery({ queryKey: ["agent"], queryFn: api.agentTasks });
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 5000);
    return () => window.clearInterval(id);
  }, []);
  const rows = tasks.data ?? [];
  return (
    <div className="space-y-3">
      <p className="text-[11px] text-slate-500">
        Muse Spark runs each workflow through planner tools; you approve before anything is published. There is no
        publish tool.
      </p>
      <QueryState query={tasks} label="agent tasks">
        {tasks.data &&
          rows.map((t) => <Task key={t.id} t={t} nurses={nurses} now={now} />)}
        {tasks.data && rows.length === 0 && (
          <p className="rounded border border-dashed border-slate-300 p-4 text-center text-xs text-slate-500">
            No agent tasks yet. A confirmed sick call starts one.
          </p>
        )}
      </QueryState>
    </div>
  );
}
