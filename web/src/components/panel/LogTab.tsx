// Log tab (design doc 8, 10.3, US-8): the decision log as a timeline grouped by run, newest
// first, with the selected run's lineage breadcrumb (seed baseline -> ... -> run) and expandable
// payloads. Events with no run (intake, agent, approvals) form their own groups in time order.
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api, type EventOut } from "../../api/client";
import QueryState from "../common/QueryState";

const ACTOR_TONE: Record<string, string> = {
  solver: "bg-slate-100 text-slate-700",
  planner: "bg-violet-100 text-violet-800",
  system: "bg-slate-100 text-slate-500",
};

function actorTone(actor: string): string {
  if (actor.startsWith("llm:")) return "bg-indigo-100 text-indigo-800";
  if (actor.startsWith("user:")) return "bg-emerald-100 text-emerald-800";
  return ACTOR_TONE[actor] ?? "bg-slate-100 text-slate-700";
}

function headline(e: EventOut): string {
  const p = e.payload as Record<string, unknown>;
  switch (e.kind) {
    case "stage.done":
      return `${e.stage}: ${p.status} best ${p.best ?? "-"}${p.proven ? " ✓ proven" : ""}`;
    case "run.proposed":
      return `${p.changed_cells ?? 0} changed cells`;
    case "run.published":
      return `published${p.superseded ? `, replaces ${p.superseded}` : ""}`;
    case "agent.step":
      return `${p.tool}: ${p.brief ?? ""}`;
    case "agent.done":
    case "agent.escalated":
      return String(p.outcome ?? "");
    case "message.drafted":
      return `to ${p.nurse}: ${String(p.message ?? "").slice(0, 80)}`;
    case "intake.parsed":
    case "request.confirmed":
      return String(p.request_id ?? "");
    case "explain.answered":
      return String(p.answer ?? "").slice(0, 90);
    default:
      return "";
  }
}

type Group = { key: string; runId: string | null; events: EventOut[] };

function group(events: EventOut[]): Group[] {
  const out: Group[] = [];
  for (const e of events) {
    const last = out[out.length - 1];
    if (last && last.runId === (e.run_id ?? null)) last.events.push(e);
    else out.push({ key: `${e.seq}`, runId: e.run_id ?? null, events: [e] });
  }
  return out.reverse();
}

function Row({ e }: { e: EventOut }) {
  const [open, setOpen] = useState(false);
  const noisy = e.kind === "stage.improved" || e.kind === "stage.start";
  return (
    <li className={noisy ? "opacity-60" : ""}>
      <button onClick={() => setOpen((o) => !o)} className="flex w-full items-baseline gap-2 text-left text-[11px]">
        <span className="w-14 shrink-0 font-mono text-slate-400">{e.ts.slice(11, 19)}</span>
        <span className={`shrink-0 rounded px-1 text-[10px] ${actorTone(e.actor)}`}>{e.actor}</span>
        <span className="shrink-0 font-mono font-semibold text-slate-700">{e.kind}</span>
        <span className="min-w-0 truncate text-slate-600">{headline(e)}</span>
      </button>
      {open && (
        <pre className="ml-16 mt-1 max-h-56 overflow-auto whitespace-pre-wrap break-all rounded bg-slate-50 p-2 text-[10px] text-slate-700">
          {JSON.stringify(e.payload, null, 1)}
        </pre>
      )}
    </li>
  );
}

function Lineage({ runId }: { runId: string }) {
  const chain = useQuery({ queryKey: ["lineage", runId], queryFn: () => api.lineage(runId) });
  if (!chain.data) return null;
  return (
    <nav className="flex flex-wrap items-center gap-1 text-[10px] text-slate-500" aria-label="lineage">
      {chain.data.map((r, i) => (
        <span key={r.run_id} className="flex items-center gap-1">
          {i > 0 && <span>→</span>}
          <span className="rounded bg-slate-100 px-1 font-mono" title={`${r.trigger ?? ""} · ${r.status}`}>
            {r.trigger ?? r.kind} · {r.changed_cells ?? 0}Δ{r.published_by ? ` · by ${r.published_by}` : ""}
          </span>
        </span>
      ))}
    </nav>
  );
}

export default function LogTab() {
  const events = useQuery({ queryKey: ["events-log"], queryFn: () => api.lastEvents(400), refetchInterval: 5000 });
  const [quiet, setQuiet] = useState(true);
  const [run, setRun] = useState<string | null>(null);
  const rows = (events.data ?? []).filter((e) => !quiet || (e.kind !== "stage.improved" && e.kind !== "stage.start"));
  const groups = group(rows).filter((g) => run === null || g.runId === run);
  return (
    <div className="space-y-2">
      <div className="flex items-center gap-3 text-[11px]">
        <label className="flex items-center gap-1 text-slate-600">
          <input type="checkbox" checked={quiet} onChange={(e) => setQuiet(e.target.checked)} /> hide search progress
        </label>
        {run && (
          <button onClick={() => setRun(null)} className="text-indigo-600 hover:underline">
            all runs
          </button>
        )}
      </div>
      <QueryState query={events} label="event log">
        {events.data &&
          groups.map((g) => (
            <section key={g.key} className="rounded-lg border border-slate-200 bg-white p-2" data-testid="log-group">
          <header className="mb-1 space-y-1">
            {g.runId ? (
              <>
                <button onClick={() => setRun(g.runId)} className="font-mono text-[11px] font-semibold text-slate-700 hover:underline">
                  {g.runId}
                </button>
                {run === g.runId && <Lineage runId={g.runId} />}
              </>
            ) : (
              <span className="text-[11px] font-semibold text-slate-500">Requests, agent and decisions</span>
            )}
          </header>
          <ol className="space-y-0.5">
            {g.events.map((e) => (
              <Row key={e.seq} e={e} />
            ))}
          </ol>
            </section>
          ))}
        {events.data && groups.length === 0 && <p className="text-xs text-slate-500">No events yet.</p>}
      </QueryState>
    </div>
  );
}
