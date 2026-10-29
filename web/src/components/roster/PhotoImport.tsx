// Roster photo import (design doc 7.8, 10.3, US-13): the charge nurse photographs the paper roster,
// Muse Spark reads it into this period's grid, and the preview shows what to check: unreadable
// cells ("?", red), days whose cells miss the printed totals (amber column) and names that match no
// nurse or more than one (amber). Every cell and name is editable; Import stays off until no "?" is
// left, and then the grid becomes the published, planned run.
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { api, type ImportOut, type Nurse, type PhotoReadOut } from "../../api/client";

const BASE_CODES = ["M", "E", "N", "G", "N/O", "D/O"];
const LEAVE_CODES = ["L:CL", "L:EL", "L:ML", "L:RH", "L:GH", "L:CCL", "L:STUDY", "L:L"];
const COUNTED = ["M", "E", "N"] as const;
const SKIP = "";

const CELL_STYLE: Record<string, string> = {
  M: "bg-amber-100 text-amber-900",
  E: "bg-sky-100 text-sky-900",
  N: "bg-indigo-700 text-white",
  G: "bg-slate-200 text-slate-700",
  "?": "bg-red-500 text-white font-bold",
};

type Row = { name: string; nurseId: string; candidates: string[]; flagged: boolean; cells: string[] };

function Elapsed({ since }: { since: number }) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), 500);
    return () => window.clearInterval(t);
  }, []);
  return <span className="tabular-nums">{((now - since) / 1000).toFixed(0)} s</span>;
}

function Preview({
  read,
  nurses,
  onImported,
}: {
  read: PhotoReadOut;
  nurses: Nurse[];
  onImported: (out: ImportOut) => void;
}) {
  const qc = useQueryClient();
  const [rows, setRows] = useState<Row[]>(() =>
    read.rows.map((r) => ({
      name: r.name,
      nurseId: r.nurse_id ?? SKIP,
      candidates: r.candidates,
      flagged: r.flagged,
      cells: [...r.cells],
    })),
  );
  const names = Object.fromEntries(nurses.map((n) => [n.id, n.name]));

  // the checksum, recomputed as cells change (7.8: same rule as planner/totals.py)
  const mismatch = useMemo(() => {
    const out = new Map<number, string[]>();
    read.days.forEach((_, d) => {
      for (const s of COUNTED) {
        const printed = read.totals[s]?.[d];
        if (printed === null || printed === undefined) continue;
        const got = rows.filter((r) => r.nurseId && r.cells[d] === s).length;
        if (got !== printed) out.set(d, [...(out.get(d) ?? []), `${s}: cells ${got}, sheet ${printed}`]);
      }
    });
    return out;
  }, [rows, read]);

  const kept = rows.filter((r) => r.nurseId !== SKIP);
  const unreadable = kept.reduce((n, r) => n + r.cells.filter((c) => c === "?").length, 0);
  const ids = kept.map((r) => r.nurseId);
  const dupes = new Set(ids.filter((id, i) => ids.indexOf(id) !== i));
  const toCheck = rows.filter((r) => r.flagged).length;

  const doImport = useMutation({
    mutationFn: () => api.importRoster(kept.map((r) => ({ nurse_id: r.nurseId, cells: r.cells }))),
    onSuccess: (out) => {
      void qc.invalidateQueries();
      onImported(out);
    },
  });

  const setCell = (ri: number, d: number, v: string) =>
    setRows((rs) => rs.map((r, i) => (i === ri ? { ...r, cells: r.cells.map((c, j) => (j === d ? v : c)) } : r)));
  const setNurse = (ri: number, v: string) =>
    setRows((rs) => rs.map((r, i) => (i === ri ? { ...r, nurseId: v, flagged: false } : r)));

  return (
    <div className="space-y-3">
      <p className="text-sm text-slate-600" data-testid="photo-summary">
        <b className={unreadable ? "text-red-600" : "text-emerald-700"}>{unreadable} unreadable</b> ·{" "}
        <b className={mismatch.size ? "text-amber-700" : "text-emerald-700"}>
          {mismatch.size} {mismatch.size === 1 ? "day misses" : "days miss"} the printed totals
        </b>{" "}
        · {toCheck} {toCheck === 1 ? "name" : "names"} to check · read in {read.tiles === 1 ? "one pass" : "two parts"}
        {read.cache_hits > 0 && " (cached)"}
      </p>
      {read.sheet_days.length > read.days.length && (
        <p className="rounded border border-amber-300 bg-amber-50 px-2 py-1 text-sm text-amber-900" data-testid="period-warning">
          The sheet shows {read.sheet_days.length} days but this period has {read.days.length}; only matching day
          numbers were copied. Is this the right period's roster?
        </p>
      )}
      <div className="max-h-[60vh] overflow-auto rounded border border-slate-200">
        <table className="border-collapse text-xs">
          <thead className="sticky top-0 z-10 bg-white">
            <tr>
              <th className="sticky left-0 z-20 bg-white px-2 py-1 text-left font-medium text-slate-500">Name on sheet → nurse</th>
              {read.days.map((iso, d) => {
                const dt = new Date(`${iso}T00:00:00`);
                const bad = mismatch.get(d);
                return (
                  <th
                    key={iso}
                    title={bad?.join("\n")}
                    className={`min-w-[2.6rem] px-0.5 py-1 font-medium ${bad ? "bg-amber-200 text-amber-900" : dt.getDay() % 6 === 0 ? "bg-slate-100 text-slate-600" : "text-slate-600"}`}
                  >
                    {dt.getDate()}
                    <div className="text-[10px] font-normal">{"SMTWTFS"[dt.getDay()]}</div>
                  </th>
                );
              })}
            </tr>
          </thead>
          <tbody>
            {rows.map((r, ri) => (
              <tr key={ri} className={r.nurseId === SKIP ? "opacity-40" : ""}>
                <td className={`sticky left-0 z-10 whitespace-nowrap border-t border-slate-100 px-2 py-0.5 ${r.flagged || dupes.has(r.nurseId) ? "bg-amber-100" : "bg-white"}`}>
                  <span className="mr-1 text-slate-700">{r.name || "?"}</span>
                  <select
                    aria-label={`Nurse for row ${ri + 1} (${r.name})`}
                    value={r.nurseId}
                    onChange={(e) => setNurse(ri, e.target.value)}
                    className="rounded border border-slate-300 bg-white px-0.5 text-[11px]"
                  >
                    <option value={SKIP}>skip row</option>
                    {(r.flagged ? [...r.candidates, ...nurses.map((n) => n.id).filter((id) => !r.candidates.includes(id))] : nurses.map((n) => n.id)).map((id) => (
                      <option key={id} value={id}>
                        {id} {names[id] ?? ""}
                      </option>
                    ))}
                  </select>
                </td>
                {r.cells.map((c, d) => (
                  <td key={d} className={`border-t border-slate-100 p-0 ${mismatch.has(d) ? "bg-amber-50" : ""}`}>
                    <select
                      aria-label={`${r.name} day ${d + 1}`}
                      value={c}
                      onChange={(e) => setCell(ri, d, e.target.value)}
                      className={`w-full appearance-none rounded-none px-0.5 py-1 text-center text-[11px] ${CELL_STYLE[c] ?? (c.startsWith("L:") ? "bg-rose-100 text-rose-800" : "bg-transparent text-slate-500")}`}
                    >
                      {c === "?" && <option value="?">?</option>}
                      {[...BASE_CODES, ...LEAVE_CODES].map((x) => (
                        <option key={x} value={x}>
                          {x === "D/O" ? "off" : x.replace("L:", "")}
                        </option>
                      ))}
                    </select>
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="flex flex-wrap items-center gap-3">
        <button
          onClick={() => doImport.mutate()}
          disabled={unreadable > 0 || dupes.size > 0 || kept.length === 0 || doImport.isPending}
          className="rounded-md bg-indigo-600 px-4 py-2 text-sm font-semibold text-white hover:bg-indigo-500 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {doImport.isPending ? "Importing…" : "Import as published plan"}
        </button>
        <span className="text-xs text-slate-500">
          {unreadable > 0
            ? "Fix the red cells first."
            : dupes.size > 0
              ? `Two rows are ${[...dupes].join(", ")}.`
              : "Replaces the published roster; the import becomes this period's planned version."}
        </span>
        {doImport.error && <span className="text-xs text-red-600">{doImport.error.message}</span>}
      </div>
    </div>
  );
}

export default function PhotoImport({ nurses }: { nurses: Nurse[] }) {
  const [open, setOpen] = useState(false);
  const [startedAt, setStartedAt] = useState(0);
  const [done, setDone] = useState<ImportOut | null>(null);
  const read = useMutation({ mutationFn: api.readPhoto, onMutate: () => setStartedAt(Date.now()) });

  const close = () => {
    setOpen(false);
    setDone(null);
    read.reset();
  };

  return (
    <>
      <button
        onClick={() => setOpen(true)}
        className="rounded-md border border-slate-300 bg-white px-3 py-2 text-sm font-semibold text-slate-700 hover:bg-slate-50"
      >
        Import photo
      </button>
      {open && (
        <div className="fixed inset-0 z-50 flex items-start justify-center overflow-auto bg-slate-900/40 p-4" role="dialog" aria-label="Import roster photo">
          <div className="w-full max-w-[min(1400px,96vw)] space-y-3 rounded-lg bg-white p-4 shadow-xl">
            <div className="flex items-center justify-between">
              <h2 className="text-base font-semibold">Import a paper roster</h2>
              <button onClick={close} aria-label="Close" className="rounded px-2 text-lg text-slate-500 hover:bg-slate-100">
                ×
              </button>
            </div>
            {done ? (
              <div className="space-y-2 text-sm" data-testid="import-done">
                <p className="text-emerald-700">
                  Imported and published as <span className="font-mono">{done.run_id}</span>
                  {done.leave_requests.length > 0 && `, with ${done.leave_requests.length} leave ${done.leave_requests.length === 1 ? "entry" : "entries"}`}.
                </p>
                {done.warnings.length > 0 && (
                  <div className="rounded border border-amber-200 bg-amber-50 p-2 text-amber-900">
                    <p className="font-medium">The paper plan breaks {done.warnings.length} hard {done.warnings.length === 1 ? "rule" : "rules"}:</p>
                    <ul className="mt-1 list-disc pl-5 font-mono text-xs">
                      {done.warnings.slice(0, 8).map((w) => (
                        <li key={w}>{w}</li>
                      ))}
                    </ul>
                  </div>
                )}
                <button onClick={close} className="rounded-md border border-slate-300 px-3 py-1.5 text-sm">
                  Close
                </button>
              </div>
            ) : read.data ? (
              <Preview read={read.data} nurses={nurses} onImported={setDone} />
            ) : (
              <div className="space-y-3">
                <p className="text-sm text-slate-600">
                  Photograph the whole sheet, flat and in good light, for the current period. Muse Spark copies each cell; you
                  check the highlighted ones before anything is imported.
                </p>
                <label className="inline-flex cursor-pointer items-center gap-2 rounded-md bg-indigo-600 px-4 py-2 text-sm font-semibold text-white hover:bg-indigo-500">
                  Take or choose a photo
                  <input
                    type="file"
                    accept="image/*"
                    className="sr-only"
                    disabled={read.isPending}
                    onChange={(e) => {
                      const f = e.target.files?.[0];
                      if (f) read.mutate(f);
                      e.target.value = "";
                    }}
                  />
                </label>
                {read.isPending && (
                  <p className="animate-pulse text-sm text-slate-600">
                    Muse Spark is reading the sheet… <Elapsed since={startedAt} /> (a month takes about 40 s)
                  </p>
                )}
                {read.error && <p className="text-sm text-red-600">{read.error.message}</p>}
              </div>
            )}
          </div>
        </div>
      )}
    </>
  );
}
