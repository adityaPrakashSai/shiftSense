// Staffing check (design doc 6.7, M14): a read-only what-if for this period. The charge nurse picks
// nurses who are out for the whole period, hypothetical hires, standby and target or floor
// coverage; CP-SAT returns the minimum headcount, the shortfall cells, standby shifts and each
// nurse's load. Nothing is stored, and every number is a lower bound (the caveat is always shown).
import { useMutation } from "@tanstack/react-query";
import { useState } from "react";
import { api, type Nurse, type StaffingOut } from "../../api/client";

const STAGE: Record<string, string> = {
  shortfall: "Shortfall",
  nurses_used: "Nurses",
  standby_shifts: "Standby",
  balance: "Balance",
};

function fmt(iso: string): string {
  return new Date(`${iso}T00:00:00`).toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short" });
}

function Result({ r }: { r: StaffingOut }) {
  if (r.status !== "solved") {
    return <p className="text-sm text-red-700">No staffing found ({r.status}).</p>;
  }
  const covered = r.shortfall === 0;
  return (
    <div className="space-y-2" data-testid="staffing-result">
      <div className={`rounded-md p-2 text-sm ${covered ? "bg-emerald-50 text-emerald-900" : "bg-red-50 text-red-900"}`}>
        {covered ? (
          <>
            Target covered with <b>{r.headcount}</b> nurses
            {r.hires_needed > 0 && <> ({r.hires_needed} of them new hires)</>}
            {(r.standby_shifts ?? 0) > 0 && <> and {r.standby_shifts} standby shifts</>}.
          </>
        ) : (
          <>
            <b>{r.shortfall}</b> {r.shortfall === 1 ? "slot" : "slots"} short of target with {r.headcount} nurses
            {(r.standby_shifts ?? 0) > 0 && <> and {r.standby_shifts} standby shifts</>}.
          </>
        )}
      </div>
      <div className="flex flex-wrap gap-1">
        {r.stages.map((s) => (
          <span key={s.stage} className="rounded-full bg-slate-100 px-2 py-0.5 text-[11px] text-slate-700">
            {STAGE[s.stage] ?? s.stage} {s.value} {s.proven ? "✓ proven" : "· best found"}
          </span>
        ))}
        <span className="text-[11px] text-slate-500">{r.secs.toFixed(1)} s</span>
      </div>
      {r.short_cells.length > 0 && (
        <div>
          <p className="text-xs font-medium text-slate-600">Below target</p>
          <ul className="text-xs text-slate-700">
            {r.short_cells.map((c) => (
              <li key={`${c.day}${c.shift}`}>
                {fmt(c.day)} {c.shift}: {c.assigned}/{c.target}
              </li>
            ))}
          </ul>
        </div>
      )}
      <table className="w-full text-xs">
        <thead>
          <tr className="text-left text-slate-500">
            <th className="font-medium">Nurse</th>
            {["Total", "M", "E", "N", "Wkend"].map((h) => (
              <th key={h} className="text-right font-medium">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {r.load.map((l) => (
            <tr key={l.nurse_id} className="border-t border-slate-100">
              <td className={l.nurse_id.startsWith("H") ? "text-indigo-700" : ""}>{l.name}</td>
              {[l.total, l.M, l.E, l.N, l.weekend].map((v, i) => (
                <td key={i} className="text-right tabular-nums">
                  {v}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function StaffingTab({ nurses }: { nurses: Nurse[] }) {
  const [removed, setRemoved] = useState<string[]>([]);
  const [hires, setHires] = useState(0);
  const [standby, setStandby] = useState(false);
  const [floor, setFloor] = useState(false);
  const [fromScratch, setFromScratch] = useState(false);
  const run = useMutation({ mutationFn: api.staffingCheck });
  const regular = nurses.filter((n) => !n.standby);

  return (
    <div className="space-y-3 rounded-lg border border-slate-200 bg-white p-3">
      <p className="text-xs text-slate-500">
        What would this period need? A what-if on the solver's hard rules and coverage; the published roster is not
        changed.
      </p>
      <fieldset>
        <legend className="text-xs font-medium text-slate-600">Out for the whole period</legend>
        <div className="mt-1 flex flex-wrap gap-1">
          {regular.map((n) => {
            const on = removed.includes(n.id);
            return (
              <button
                key={n.id}
                aria-pressed={on}
                onClick={() => setRemoved((r) => (on ? r.filter((x) => x !== n.id) : [...r, n.id]))}
                className={`rounded-full px-2 py-0.5 text-[11px] ${on ? "bg-red-600 text-white" : "bg-slate-100 text-slate-700 hover:bg-slate-200"}`}
              >
                {n.name.split(" ")[0]}
              </button>
            );
          })}
        </div>
      </fieldset>
      <div className="grid grid-cols-2 gap-2 text-xs text-slate-700">
        <label className="flex items-center gap-1">
          Hypothetical hires
          <select value={hires} onChange={(e) => setHires(Number(e.target.value))} className="rounded border border-slate-300 px-1">
            {[0, 1, 2, 3].map((k) => (
              <option key={k}>{k}</option>
            ))}
          </select>
        </label>
        <label className="flex items-center gap-1">
          <input type="checkbox" checked={standby} onChange={(e) => setStandby(e.target.checked)} />
          Standby may work
        </label>
        <label className="flex items-center gap-1">
          <input type="checkbox" checked={floor} onChange={(e) => setFloor(e.target.checked)} />
          Hard floor only
        </label>
        <label className="flex items-center gap-1">
          <input type="checkbox" checked={fromScratch} onChange={(e) => setFromScratch(e.target.checked)} />
          Minimum from scratch
        </label>
      </div>
      <button
        onClick={() =>
          run.mutate({ hires, removed, standby, coverage: floor ? "floor" : "target", keep_staff: !fromScratch })
        }
        disabled={run.isPending}
        className="w-full rounded-md bg-indigo-600 py-1.5 text-sm font-semibold text-white hover:bg-indigo-500 disabled:cursor-wait disabled:opacity-60"
      >
        {run.isPending ? "Checking…" : "Run staffing check"}
      </button>
      {run.error && <p className="text-xs text-red-600">{run.error.message}</p>}
      {run.data && <Result r={run.data} />}
      <p className="rounded bg-amber-50 p-2 text-[11px] text-amber-900" data-testid="staffing-caveat">
        Lower bound: requests, sequence rules and history-based fairness are ignored, so a publishable roster may need
        more.
      </p>
    </div>
  );
}
