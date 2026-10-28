// Charge nurse view (design doc 10.3), M4 scope: header with Generate and the live run timer,
// roster grid with coverage footer, stage strip, and Publish for a draft. Live via useWardSocket.
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useState } from "react";
import { api, ApiError, type EventOut } from "../api/client";
import { useWardSocket, type SocketMode, type WardMessage } from "../api/ws";
import ModeBadge from "../components/common/ModeBadge";
import ProviderBadge from "../components/common/ProviderBadge";
import RunTimer, { type TierTick } from "../components/common/RunTimer";
import RosterGrid from "../components/roster/RosterGrid";
import StageStrip from "../components/roster/StageStrip";

type Timer = {
  runId: string | null;
  startedAt: number | null;
  stoppedAt: number | null;
  ticks: TierTick[];
  serverSecs: number | null;
};
const IDLE: Timer = { runId: null, startedAt: null, stoppedAt: null, ticks: [], serverSecs: null };

function fmtRange(start: string, days: number): string {
  const s = new Date(`${start}T00:00:00`);
  const e = new Date(s);
  e.setDate(s.getDate() + days - 1);
  const f = (d: Date) => d.toLocaleDateString("en-GB", { day: "numeric", month: "short" });
  return `${f(s)} – ${f(e)} ${e.getFullYear()}`;
}

function SocketDot({ mode }: { mode: SocketMode }) {
  const tone = { live: "bg-emerald-500", polling: "bg-amber-500", connecting: "bg-slate-400" }[mode];
  const text = { live: "live", polling: "polling every 3 s", connecting: "connecting…" }[mode];
  return (
    <span className="flex items-center gap-1 text-xs text-slate-500">
      <span className={`h-2 w-2 rounded-full ${tone}`} /> {text}
    </span>
  );
}

export default function ChargePage() {
  const qc = useQueryClient();
  const health = useQuery({ queryKey: ["health"], queryFn: api.health });
  const ward = useQuery({ queryKey: ["ward"], queryFn: api.ward });
  const usage = useQuery({ queryKey: ["llm-usage"], queryFn: api.llmUsage, refetchInterval: 15_000 });
  const [viewRunId, setViewRunId] = useState<string | null>(null); // null = published
  const [solving, setSolving] = useState<string | null>(null);
  const [timer, setTimer] = useState<Timer>(IDLE);

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
      if (m.type === "run_started") setSolving(m.run_id);
      if (m.type === "run_finished") {
        setSolving((cur) => (cur === m.run_id ? null : cur));
        if (m.status === "proposed") setViewRunId(m.run_id);
        void qc.invalidateQueries({ queryKey: ["roster", m.run_id] });
      }
      if (m.type === "roster_published") {
        setViewRunId(null);
        void qc.invalidateQueries({ queryKey: ["roster"] });
        void qc.invalidateQueries({ queryKey: ["run"] });
      }
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
    onMutate: () => setTimer({ ...IDLE, startedAt: performance.now() }),
    onSuccess: ({ run_id }) => {
      setSolving(run_id);
      setTimer((t) => ({ ...t, runId: run_id }));
    },
    onError: () => setTimer(IDLE),
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

  return (
    <div className="min-h-screen bg-slate-50 text-slate-800">
      <header className="border-b border-slate-200 bg-white px-6 py-3">
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
            {health.data && <ModeBadge mode={health.data.llm_mode} />}
            <SocketDot mode={mode} />
          </div>
          <div className="ml-auto flex items-center gap-4">
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
        <div className="mt-2 flex flex-wrap items-center gap-2 text-xs">
          <span className="text-slate-500">Priorities</span>
          {(ward.data?.priorities ?? []).map((p, i) => (
            <span key={p} className="rounded-full bg-slate-100 px-2 py-0.5 text-slate-700">
              {i + 1}. {p}
            </span>
          ))}
        </div>
      </header>

      <main className="space-y-3 px-6 py-4">
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
                  className="rounded-md bg-emerald-600 px-3 py-1.5 text-sm font-semibold text-white hover:bg-emerald-500 disabled:opacity-60"
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
        {noRoster && (
          <p className="rounded border border-slate-200 bg-white p-4 text-sm text-slate-600">
            No published roster yet. Click Generate to build one.
          </p>
        )}
        {roster.data && ward.data && <RosterGrid roster={roster.data} nurses={ward.data.nurses} />}
      </main>
    </div>
  );
}
