// Proposal card (design doc 10.3, 6.4): the open cell, ranked candidates with tier deltas and
// changed cells, excluded nurses with the rule that blocks each, Approve / choose another.
// While a cascade runs the candidates show its progress instead of Approve buttons, and after
// an accept the accepted candidate is the only green Publish on the card.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api, type Candidate, type ClaimOut, type Nurse, type ProposalOut } from "../../api/client";
import { CascadeLine } from "./AgentTab";
import ClaimChip from "./ClaimChip";
import { ruleLabels } from "./rules";

const SHIFT = { M: "Morning", E: "Evening", N: "Night", G: "General" } as Record<string, string>;

function fmt(iso: string): string {
  return new Date(`${iso}T00:00:00`).toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short" });
}

function Deltas({ c }: { c: Candidate }) {
  const items = Object.entries(c.delta_vs_best).filter(([, v]) => v !== 0);
  if (!items.length) return <span className="text-[11px] text-emerald-700">best</span>;
  return (
    <span className="flex flex-wrap gap-1">
      {items.map(([t, v]) => (
        <span key={t} className="rounded bg-amber-100 px-1 text-[11px] text-amber-900">
          {v > 0 ? "+" : ""}
          {v} {t}
        </span>
      ))}
    </span>
  );
}

export default function ProposalCard({
  p,
  nurses,
  viewing,
  onView,
}: {
  p: ProposalOut;
  nurses: Nurse[];
  viewing: string | null;
  onView: (runId: string) => void;
}) {
  const qc = useQueryClient();
  const name = (id: string | null | undefined) => nurses.find((n) => n.id === id)?.name ?? id ?? "";
  const choose = useMutation({
    mutationFn: (nurse: string | null) => api.choose(p.run_id, nurse),
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: ["proposals"] });
      void qc.invalidateQueries({ queryKey: ["roster"] });
    },
  });
  const approvePlan = useMutation({
    mutationFn: () => api.approveCascade(p.agent?.task_id ?? ""),
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: ["proposals"] });
      void qc.invalidateQueries({ queryKey: ["agent"] });
    },
  });
  const publish = useMutation({
    mutationFn: (runId: string) => api.publish(runId),
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: ["proposals"] });
      void qc.invalidateQueries({ queryKey: ["roster"] });
      void qc.invalidateQueries({ queryKey: ["agent"] });
    },
  });
  const reject = useMutation({
    mutationFn: () => api.rejectApproval(p.agent?.task_id ?? ""),
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: ["proposals"] });
      void qc.invalidateQueries({ queryKey: ["agent"] });
    },
  });
  const tasks = useQuery({ queryKey: ["agent"], queryFn: api.agentTasks });
  const task = tasks.data?.find((t) => t.id === p.agent?.task_id);
  const planPending = (p.agent?.cascade?.length ?? 0) > 0;
  // a run approval after an approved plan, for one of the ranked runs: someone accepted
  const acceptedRef =
    p.agent?.kind === "run" && !planPending && task?.cascade?.approved
      ? (p.candidates.find((c) => c.run_id === p.agent!.ref)?.run_id ?? null)
      : null;
  const running =
    (tasks.data ?? []).find(
      (t) => t.status === "waiting_reply" && t.cascade?.approved && t.cascade?.ref === p.run_id,
    ) ?? null;
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    if (!running) return;
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [running]);
  const cell = p.open_cell;
  const req = p.request;
  const title = cell
    ? cell.nurse
      ? `${name(cell.nurse)} off ${fmt(cell.day)} ${SHIFT[cell.shift] ?? cell.shift}`
      : `Extra nurse needed ${fmt(cell.day)} ${SHIFT[cell.shift] ?? cell.shift}`
    : `Re-plan after ${req ? `${req.kind.replace("_", " ")} (${name(req.nurse_id)})` : "a change"}`;

  return (
    <article className="rounded-lg border border-slate-200 bg-white p-3 shadow-sm">
      <header className="flex items-start gap-2">
        <div className="min-w-0 flex-1">
          <h3 className="text-sm font-semibold">{title}</h3>
          <p className="text-[11px] text-slate-500">
            {req && <span className="font-mono">{req.id} · </span>}
            ranked in {((p.timing_ms.total ?? 0) / 1000).toFixed(2)} s · {p.run.changed_cells ?? 0} changed cell
            {p.run.changed_cells === 1 ? "" : "s"}
          </p>
        </div>
        <button
          onClick={() => onView(p.run_id)}
          className={`rounded px-2 py-0.5 text-[11px] ${viewing === p.run_id ? "bg-indigo-600 text-white" : "bg-slate-100 text-slate-700"}`}
        >
          {viewing === p.run_id ? "On grid" : "Show"}
        </button>
      </header>

      {p.agent && (
        <div className="mt-2 rounded bg-indigo-50 px-2 py-1.5 text-xs text-indigo-950" data-testid="agent-summary">
          <p>
            <span className="font-semibold">Muse agent:</span> {p.agent.summary}
          </p>
          {p.agent.claims.length > 0 && (
            <div className="mt-1 flex flex-wrap gap-1">
              {p.agent.claims.map((c, i) => (
                <ClaimChip key={i} claim={c as ClaimOut} />
              ))}
            </div>
          )}
          {p.agent.summary_source === "describe" && (
            <p className="mt-0.5 text-[10px] text-indigo-700">model summary withheld: unverified claims</p>
          )}
        </div>
      )}

      {p.agent?.cascade && p.agent.cascade.length > 0 && (
        <div className="mt-2 rounded bg-sky-50 px-2 py-1.5 text-xs text-sky-950" data-testid="cascade-plan">
          <p>
            <span className="font-semibold">Ask in order:</span>{" "}
            {p.agent.cascade.map((id) => name(id)).join(" → ")}
          </p>
          <p className="mt-0.5 text-[11px] text-sky-800">
            Approving the plan publishes nothing; the first accept comes back for one Publish.
          </p>
          <button
            onClick={() => approvePlan.mutate()}
            disabled={approvePlan.isPending}
            className="mt-1 rounded bg-emerald-700 px-2 py-1 font-semibold text-white hover:bg-emerald-800 disabled:opacity-60"
          >
            Approve plan
          </button>
          {approvePlan.error && <p className="mt-1 text-red-600">{approvePlan.error.message}</p>}
        </div>
      )}

      {p.coverage_worse && (
        <p className="mt-2 rounded bg-red-50 px-2 py-1 text-xs text-red-800">
          Coverage gets worse than the published roster ({p.published_coverage} → {p.candidates[0]?.tiers.coverage}):
          nobody can legally fill every slot.
        </p>
      )}

      {running && !acceptedRef && (
        <div className="mt-2 rounded bg-sky-50 px-2 py-1.5 text-xs text-sky-950" data-testid="cascade-running">
          <CascadeLine t={running} nurses={nurses} now={now} testId="cascade-progress-inline" />
        </div>
      )}
      {p.candidates.length > 0 ? (
        <ol className="mt-2 space-y-1.5">
          {p.candidates.map((c) => (
            <li
              key={c.nurse}
              className={`flex items-center gap-2 rounded border px-2 py-1.5 ${
                acceptedRef === c.run_id ? "border-emerald-300 ring-1 ring-emerald-300" : "border-slate-100"
              }`}
            >
              <span className="w-4 text-xs font-semibold text-slate-500">{c.rank}</span>
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-1.5 text-sm">
                  <span className="font-medium">{name(c.nurse)}</span>
                  {c.standby && <span className="rounded bg-sky-100 px-1 text-[10px] text-sky-800">standby</span>}
                  <span className="text-[11px] text-slate-500">{c.kind}</span>
                  {c.tie && (
                    <span className="rounded bg-violet-100 px-1 text-[10px] text-violet-800" title={`tie-break: ${c.tie_break}`}>
                      TIE
                    </span>
                  )}
                  {acceptedRef === c.run_id && (
                    <span className="rounded bg-emerald-100 px-1 text-[10px] font-semibold text-emerald-800">
                      accepted
                    </span>
                  )}
                </div>
                <div className="flex flex-wrap items-center gap-2 text-[11px] text-slate-500">
                  <Deltas c={c} />
                  <span>
                    {c.changed_cells} changed {c.changed_cells === 1 ? "cell" : "cells"}
                  </span>
                  {!c.proven && <span className="text-amber-700">best found</span>}
                </div>
              </div>
              <button onClick={() => onView(c.run_id)} className="text-[11px] text-indigo-600 hover:underline">
                view
              </button>
              {acceptedRef === c.run_id ? (
                <>
                  <button
                    onClick={() => publish.mutate(c.run_id)}
                    disabled={publish.isPending || reject.isPending}
                    data-testid="proposal-publish"
                    className="rounded bg-emerald-700 px-2 py-1 text-xs font-semibold text-white hover:bg-emerald-800 disabled:opacity-60"
                  >
                    Publish
                  </button>
                  <button
                    onClick={() => reject.mutate()}
                    disabled={publish.isPending || reject.isPending}
                    data-testid="proposal-reject"
                    className="rounded border border-slate-300 px-2 py-1 text-xs font-semibold text-slate-700 disabled:opacity-60"
                  >
                    Reject
                  </button>
                </>
              ) : acceptedRef ? null : running ? (
                <button
                  onClick={() => choose.mutate(c.nurse)}
                  disabled={choose.isPending}
                  title={`Publish ${name(c.nurse)}'s run at once, without asking the plan's nurses`}
                  className="text-[11px] text-slate-500 hover:underline disabled:opacity-60"
                >
                  Publish now
                </button>
              ) : (
                <button
                  onClick={() => choose.mutate(c.nurse)}
                  disabled={choose.isPending}
                  title={
                    planPending && c.rank === 1
                      ? `Publish ${name(c.nurse)}'s run at once, without asking the plan's nurses`
                      : undefined
                  }
                  className={`rounded px-2 py-1 text-xs font-semibold disabled:opacity-60 ${
                    c.rank === 1 && !planPending
                      ? "bg-emerald-700 text-white hover:bg-emerald-800"
                      : "border border-slate-300 text-slate-700"
                  }`}
                >
                  {c.rank === 1 ? (planPending ? `Publish ${name(c.nurse)} now` : "Approve") : "Choose"}
                </button>
              )}
            </li>
          ))}
        </ol>
      ) : (
        <div className="mt-2 flex items-center justify-between text-xs text-slate-600">
          <span>No open shift to fill; the draft shows the re-planned roster.</span>
          <button
            onClick={() => choose.mutate(null)}
            disabled={choose.isPending}
            className="rounded bg-emerald-700 px-2 py-1 font-semibold text-white disabled:opacity-60"
          >
            Approve
          </button>
        </div>
      )}
      {p.candidates[0]?.tie && (
        <p className="mt-1 text-[11px] text-violet-700">Equally good; picked by {p.candidates[0].tie_break}.</p>
      )}

      {p.excluded.length > 0 && (
        <details className="mt-2 text-xs text-slate-600">
          <summary className="cursor-pointer text-slate-500">{p.excluded.length} nurses cannot take it</summary>
          <ul className="mt-1 space-y-0.5">
            {p.excluded.map((e) => (
              <li key={e.nurse} title={e.rules.join(", ")}>
                <span className="font-medium">{name(e.nurse)}</span>: {ruleLabels(e.rules).join(", ")}
              </li>
            ))}
          </ul>
        </details>
      )}
      {choose.error && <p className="mt-1 text-xs text-red-600">{choose.error.message}</p>}
      {(publish.error ?? reject.error) && (
        <p className="mt-1 text-xs text-red-600">{(publish.error ?? reject.error)?.message}</p>
      )}
    </article>
  );
}
