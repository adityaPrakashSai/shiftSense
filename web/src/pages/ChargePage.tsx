// Charge nurse view (design doc 10.3): header with Generate, the live run timer and editable
// priority chips, roster grid with coverage footer and proposal diff highlighting, stage strip,
// Publish for a draft, and the right panel: Alerts (approvals, proposals with the agent's summary,
// replacement messages), Ask, Agent (live roster-agent traces), Fairness, Log, Versions and Staffing (6.7).
// `ChargeView` is the same view embedded in /demo (left 65%).
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { lazy, Suspense, useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api, ApiError, type Cell, type EventOut, type Ward } from "../api/client";
import { useWardSocket, type SocketMode, type WardMessage } from "../api/ws";
import ModeBadge from "../components/common/ModeBadge";
import ProviderBadge from "../components/common/ProviderBadge";
import RunTimer, { type TierTick } from "../components/common/RunTimer";
import PhotoImport from "../components/roster/PhotoImport";
import RosterGrid from "../components/roster/RosterGrid";
import AgentTab from "../components/panel/AgentTab";
import AlertsTab from "../components/panel/AlertsTab";
import AskTab, { type AskPrefill } from "../components/panel/AskTab";
import LogTab from "../components/panel/LogTab";
import StaffingTab from "../components/panel/StaffingTab";
import VersionsTab from "../components/panel/VersionsTab";
import Priorities from "../components/common/Priorities";
import CellPanel from "../components/roster/CellPanel";
import StageStrip from "../components/roster/StageStrip";

// Recharts is most of the bundle: load it when the Fairness tab first opens, not on /demo's cold load
const FairnessTab = lazy(() => import("../components/panel/FairnessTab"));

type Tab = "alerts" | "ask" | "agent" | "fairness" | "log" | "versions" | "staffing";
const TABS: [Tab, string][] = [
  ["alerts", "Alerts"],
  ["ask", "Ask"],
  ["agent", "Agent"],
  ["fairness", "Fairness"],
  ["log", "Log"],
  ["versions", "Versions"],
  ["staffing", "Staffing"],
];

type Timer = {
  runId: string | null;
  startedAt: number | null;
  stoppedAt: number | null;
  ticks: TierTick[];
  serverSecs: number | null;
};
const IDLE: Timer = { runId: null, startedAt: null, stoppedAt: null, ticks: [], serverSecs: null };

function fmtRange(start: string, days: number, year = true): string {
  const s = new Date(`${start}T00:00:00`);
  const e = new Date(s);
  e.setDate(s.getDate() + days - 1);
  const f = (d: Date) => d.toLocaleDateString("en-GB", { day: "numeric", month: "short" });
  return year ? `${f(s)} – ${f(e)} ${e.getFullYear()}` : `${f(s)} – ${f(e)}`;
}

/** Horizon days from the ward (the empty-period grid needs dates with no roster behind them). */
function horizonDays(ward: Ward): string[] {
  const out: string[] = [];
  const d = new Date(`${ward.horizon_start}T00:00:00`);
  const n = ward.horizon_days ?? 14;
  for (let i = 0; i < n; i++) {
    out.push(`${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`);
    d.setDate(d.getDate() + 1);
  }
  return out;
}

function SocketDot({ mode }: { mode: SocketMode }) {
  const tone = { live: "bg-emerald-500", polling: "bg-amber-500", connecting: "bg-slate-400" }[mode];
  const text = { live: "connected", polling: "polling every 3 s", connecting: "connecting…" }[mode];
  return (
    <span className="flex items-center gap-1 text-xs text-slate-500" title="Live updates connection">
      <span className={`h-2 w-2 rounded-full ${tone}`} /> {text}
    </span>
  );
}

export default function ChargePage() {
  return <ChargeView />;
}

export function ChargeView({ embedded = false }: { embedded?: boolean }) {
  const qc = useQueryClient();
  const health = useQuery({ queryKey: ["health"], queryFn: api.health });
  const ward = useQuery({ queryKey: ["ward"], queryFn: api.ward });
  const usage = useQuery({ queryKey: ["llm-usage"], queryFn: api.llmUsage, refetchInterval: 15_000 });
  const [viewRunId, setViewRunId] = useState<string | null>(null); // null = published
  const [solving, setSolving] = useState<string | null>(null);
  const [generateRunId, setGenerateRunId] = useState<string | null>(null);
  // the run_started message can beat the POST response; while set, it belongs to this Generate
  const generatePendingRef = useRef(false);
  const [timer, setTimer] = useState<Timer>(IDLE);
  const [tab, setTab] = useState<Tab>("alerts");
  const [cell, setCell] = useState<Cell | null>(null);
  const [prefill, setPrefill] = useState<AskPrefill | null>(null);
  const agentTasks = useQuery({ queryKey: ["agent"], queryFn: api.agentTasks });
  // the run the charge nurse is being asked to publish (an accept, not rank 1)
  const openApprovalRef =
    (agentTasks.data ?? []).find((t) => {
      const w = t.waiting as { approval_kind?: string; cascade?: string[] | null; ref?: string } | null;
      return t.status === "waiting_approval" && w?.approval_kind === "run" && !(w?.cascade?.length ?? 0);
    })?.waiting?.ref ?? null;
  useEffect(() => {
    if (typeof openApprovalRef === "string" && openApprovalRef) setViewRunId(openApprovalRef);
  }, [openApprovalRef]);

  const roster = useQuery({
    queryKey: ["roster", viewRunId],
    queryFn: () => api.roster(viewRunId),
    placeholderData: keepPreviousData,
    retry: (n, e) => !(e instanceof ApiError && e.status === 404) && n < 2,
  });
  const runId = roster.data?.run.run_id;
  const detail = useQuery({
    queryKey: ["run", runId],
    queryFn: () => api.run(runId!),
    enabled: !!runId,
  });

  const onMessage = useCallback(
    (m: WardMessage) => {
      if (m.type === "demo_reset") {
        setViewRunId(null);
        setSolving(null);
        setGenerateRunId(null);
        generatePendingRef.current = false;
        setTimer(IDLE);
        setCell(null);
        setPrefill(null);
        setTab("alerts");
      }
      if (m.type === "run_started") {
        if (generatePendingRef.current) {
          setGenerateRunId(m.run_id);
          generatePendingRef.current = false;
        }
        setSolving(m.run_id);
        // a re-solve started elsewhere (a nurse confirmed): time it from the nudge
        setTimer((t) =>
          t.runId === m.run_id || (t.startedAt !== null && t.runId === null)
            ? { ...t, runId: m.run_id }
            : { ...IDLE, startedAt: performance.now(), runId: m.run_id },
        );
      }
      if (m.type === "proposal_created") void qc.invalidateQueries({ queryKey: ["proposals"] });
      if (m.type === "conflict_created") void qc.invalidateQueries({ queryKey: ["conflicts"] });
      if (m.type === "run_finished") {
        setSolving((cur) => (cur === m.run_id ? null : cur));
        if (m.status === "proposed") setViewRunId(m.run_id);
        // no draft will appear for an infeasible or failed run: stop its timer now
        else setTimer((t) => (t.runId === m.run_id && t.stoppedAt === null ? { ...t, stoppedAt: performance.now() } : t));
        void qc.invalidateQueries({ queryKey: ["roster", m.run_id] });
      }
      if (m.type === "request_updated") void qc.invalidateQueries({ queryKey: ["requests"] });
      if (m.type === "agent_updated") {
        void qc.invalidateQueries({ queryKey: ["agent"] });
        void qc.invalidateQueries({ queryKey: ["proposals"] });
      }
      if (m.type === "message_for_nurse") void qc.invalidateQueries({ queryKey: ["inbox"] });
      if (m.type === "ward_updated") void qc.invalidateQueries({ queryKey: ["ward"] });
      if (m.type === "roster_published") {
        setViewRunId(null);
        void qc.invalidateQueries({ queryKey: ["proposals"] });
        void qc.invalidateQueries({ queryKey: ["roster"] });
        void qc.invalidateQueries({ queryKey: ["run"] });
        void qc.invalidateQueries({ queryKey: ["fairness"] });
        void qc.invalidateQueries({ queryKey: ["versions"] });
      }
      if (m.type === "run_finished") void qc.invalidateQueries({ queryKey: ["versions"] });
    },
    [qc],
  );

  const onEvents = useCallback((events: EventOut[]) => {
    setTimer((t) => {
      if (!t.runId) return t;
      const ticks = [...t.ticks];
      for (const e of events) {
        if (e.run_id !== t.runId || e.kind !== "stage.done" || !e.stage) continue;
        const tier = e.stage.split(":").pop()!;
        if (ticks.some((x) => x.tier === tier)) continue;
        const p = e.payload as { secs?: number; status?: string };
        ticks.push({ tier, secs: p.secs ?? 0, skipped: p.status === "skip" });
      }
      return ticks.length === t.ticks.length ? t : { ...t, ticks };
    });
  }, []);

  const mode = useWardSocket(ward.data?.id, onMessage, onEvents);

  // stop the timer when the draft it measures is on screen
  useEffect(() => {
    const r = roster.data?.run;
    if (!r || !timer.runId || timer.stoppedAt !== null || r.run_id !== timer.runId) return;
    setTimer((t) => ({ ...t, stoppedAt: performance.now(), serverSecs: r.total_secs ?? null }));
  }, [roster.data, timer.runId, timer.stoppedAt]);

  const generate = useMutation({
    mutationFn: api.generate,
    onMutate: () => {
      setTimer({ ...IDLE, startedAt: performance.now() });
      generatePendingRef.current = true;
    },
    onSuccess: ({ run_id }) => {
      setSolving(run_id);
      setGenerateRunId(run_id);
      generatePendingRef.current = false;
      setTimer((t) => ({ ...t, runId: run_id }));
    },
    onError: () => {
      setTimer(IDLE);
      generatePendingRef.current = false;
    },
  });
  const publish = useMutation({
    mutationFn: (id: string) => api.publish(id),
    onSuccess: () => {
      setViewRunId(null);
      void qc.invalidateQueries({ queryKey: ["roster"] });
    },
  });

  const run = roster.data?.run;
  const isDraft = run?.status === "proposed";
  const noRoster = roster.error instanceof ApiError && roster.error.status === 404;
  // an unpublished period is an empty state, not an error: the grid below handles it
  const emptyPeriod = roster.error instanceof ApiError && roster.error.code === "NO_PUBLISHED_RUN";
  const emptyDays = ward.data ? horizonDays(ward.data) : [];
  const emptyGrid: Cell[] = ward.data
    ? ward.data.nurses.flatMap((n) =>
        emptyDays.map(
          (day): Cell => ({
            nurse_id: n.id,
            day,
            shift: null,
            display: "",
            leave_type: null,
            prev_shift: null,
            changed: null,
            cause: null,
          }),
        ),
      )
    : [];
  // the legend explains the diff styling, so it shows exactly when styled cells do
  const hasDiff = roster.data?.grid.some((c) => c.changed) ?? false;

  return (
    <div className={`${embedded ? "" : "min-h-screen"} bg-slate-50 text-slate-800`}>
      <header className={`border-b border-slate-200 bg-white ${embedded ? "px-4 py-2" : "px-6 py-3"}`}>
        <div className="flex flex-wrap items-center gap-x-6 gap-y-2">
          <div>
            <h1 className="text-lg font-semibold">{ward.data?.name ?? "ShiftSense"}</h1>
            {ward.data && (
              <p className="text-xs text-slate-500">
                {fmtRange(ward.data.horizon_start, ward.data.horizon_days ?? 14)} ·{" "}
                {ward.data.horizon_preset} · {ward.data.nurses.length} nurses
              </p>
            )}
          </div>
          <div className="flex items-center gap-2">
            {usage.data && <ProviderBadge usage={usage.data} />}
            {health.data && !embedded && <ModeBadge mode={health.data.llm_mode} />}
            <SocketDot mode={mode} />
            {!embedded && (
              <Link to="/nurse" className="text-xs text-indigo-600 hover:underline">
                Nurse view
              </Link>
            )}
          </div>
          <div className="ml-auto flex items-center gap-4">
            {ward.data && <PhotoImport nurses={ward.data.nurses} />}
            <RunTimer
              startedAt={timer.startedAt}
              stoppedAt={timer.stoppedAt}
              ticks={timer.ticks}
              serverSecs={timer.serverSecs}
            />
            <button
              onClick={() => generate.mutate()}
              disabled={generate.isPending || solving !== null}
              className="rounded-md bg-indigo-600 px-4 py-2 text-sm font-semibold text-white shadow-sm hover:bg-indigo-500 disabled:cursor-wait disabled:opacity-60"
            >
              {solving ? "Solving…" : "Generate"}
            </button>
          </div>
        </div>
        {ward.data && <Priorities priorities={ward.data.priorities ?? []} />}
      </header>

      {/* embedded on a phone or tablet: the panel (approvals) above the grid, full width */}
      <div className={`flex items-start gap-4 ${embedded ? "flex-col px-3 py-3 lg:flex-row lg:px-4" : "px-6 py-4"}`}>
      <main className={`min-w-0 flex-1 space-y-3 ${embedded ? "w-full" : ""}`}>
        {run && (
          <div className="flex flex-wrap items-center gap-3">
            <span
              className={`rounded px-2 py-0.5 text-xs font-semibold ${
                isDraft ? "bg-amber-100 text-amber-800" : "bg-emerald-100 text-emerald-800"
              }`}
            >
              {isDraft ? "Draft (proposed)" : "Published"}
            </span>
            <span className="font-mono text-xs text-slate-500">{run.run_id}</span>
            {run.total_secs != null && (
              <span className="text-xs text-slate-500">solved in {run.total_secs.toFixed(2)} s</span>
            )}
            {isDraft && (
              <>
                <button
                  onClick={() => publish.mutate(run.run_id)}
                  disabled={publish.isPending}
                  className="rounded-md bg-emerald-700 px-3 py-1.5 text-sm font-semibold text-white hover:bg-emerald-800 disabled:opacity-60"
                >
                  Publish
                </button>
                <button
                  onClick={() => setViewRunId(null)}
                  className="text-sm text-slate-600 underline-offset-2 hover:underline"
                >
                  Back to published
                </button>
              </>
            )}
            {publish.error && <span className="text-sm text-red-600">{publish.error.message}</span>}
          </div>
        )}
        {roster.data && <StageStrip stages={roster.data.stages} detail={detail.data} />}
        {emptyPeriod && ward.data && (
          <div className="relative">
            <RosterGrid
              roster={{ days: emptyDays, today: null, grid: emptyGrid, totals: [] }}
              nurses={ward.data.nurses}
            />
            <div className="absolute inset-0 flex items-start justify-center bg-slate-50/70 p-4 pt-24">
              <div
                data-testid="empty-roster"
                className="sticky top-24 max-w-sm rounded-xl border border-slate-200 bg-white p-6 text-center shadow-lg"
              >
                <h2 className="text-lg font-semibold">
                  No roster for {fmtRange(ward.data.horizon_start, ward.data.horizon_days ?? 14, false)}{" "}
                  yet
                </h2>
                <p className="mt-1 text-sm text-slate-500">By hand this takes about 2 hours</p>
                <button
                  onClick={() => generate.mutate()}
                  disabled={generate.isPending || solving !== null}
                  className="mt-4 rounded-lg bg-indigo-600 px-6 py-3 text-base font-semibold text-white shadow hover:bg-indigo-500 disabled:cursor-wait disabled:opacity-60"
                >
                  {solving ? "Solving…" : "Generate roster"}
                </button>
              </div>
            </div>
          </div>
        )}
        {!emptyPeriod && noRoster && (
          <p className="rounded border border-slate-200 bg-white p-4 text-sm text-slate-600">
            No published roster yet. Click Generate to build one.
          </p>
        )}
        {roster.data && ward.data && (
          <RosterGrid
            roster={roster.data}
            nurses={ward.data.nurses}
            selected={cell ? `${cell.nurse_id}|${cell.day}` : null}
            onCell={(c) => setCell((cur) => (cur && cur.nurse_id === c.nurse_id && cur.day === c.day ? null : c))}
          />
        )}
        {hasDiff && (
          <p className="text-[11px] text-slate-500">
            In a proposal: <span className="text-red-600 line-through">M</span> removed by the request,{" "}
            <span className="rounded px-1 ring-2 ring-inset ring-emerald-500">M</span> replacement,{" "}
            <span className="rounded px-1 outline outline-2 outline-dashed outline-amber-500 -outline-offset-2">
              M
            </span>{" "}
            other change.
          </p>
        )}
      </main>
      <aside
        className={`shrink-0 ${embedded ? "order-first w-full lg:sticky lg:top-4 lg:order-none lg:w-[340px]" : "sticky top-4 w-[380px]"}`}
      >
        <div className="relative">
        {cell && run && ward.data && (
          <div className="absolute inset-x-0 top-0 z-20">
          <CellPanel
            cell={cell}
            runId={run.run_id}
            nurses={ward.data.nurses}
            onClose={() => setCell(null)}
            onWhy={() => {
              const name = ward.data!.nurses.find((n) => n.id === cell.nurse_id)?.name ?? cell.nurse_id;
              const when = new Date(`${cell.day}T00:00:00`).toLocaleDateString("en-GB", { day: "numeric", month: "short" });
              const what = cell.shift ? `on ${cell.display} duty` : `off (${cell.display})`;
              setTab("ask");
              setPrefill((p) => ({
                question: `Why is ${name} ${what} on ${when}?`,
                context: { nurse_id: cell.nurse_id, day: cell.day, shift: cell.shift },
                runId: run.run_id,
                n: (p?.n ?? 0) + 1,
              }));
            }}
          />
          </div>
        )}
        <nav className="mb-2 flex flex-wrap gap-1">
          {TABS.map(([k, label]) => (
            <button
              key={k}
              onClick={() => setTab(k)}
              className={`rounded-md px-2.5 py-1 text-xs font-medium ${
                tab === k ? "bg-slate-800 text-white" : "bg-slate-100 text-slate-600 hover:bg-slate-200"
              }`}
            >
              {label}
            </button>
          ))}
        </nav>
        <div className={tab === "alerts" ? "" : "hidden"}>
          {ward.data && (
            <AlertsTab
              nurses={ward.data.nurses}
              solving={solving !== null}
              timer={timer}
              viewing={viewRunId}
              onView={setViewRunId}
              emptyPeriod={emptyPeriod}
              solvingKind={solving !== null && solving === generateRunId ? "generate" : "resolve"}
            />
          )}
        </div>
        <div className={tab === "ask" ? "" : "hidden"}>
          <AskTab runId={run?.run_id ?? null} prefill={prefill} />
        </div>
        {tab === "agent" && ward.data && <AgentTab nurses={ward.data.nurses} />}
        {tab === "fairness" && (
          <Suspense fallback={<p className="text-sm text-slate-500">Loading chart…</p>}>
            <FairnessTab />
          </Suspense>
        )}
        {tab === "log" && <LogTab />}
        {tab === "versions" && ward.data && <VersionsTab nurses={ward.data.nurses} />}
        {tab === "staffing" && ward.data && <StaffingTab nurses={ward.data.nurses} />}
        </div>
      </aside>
      </div>
    </div>
  );
}
