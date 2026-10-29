// Versions tab (design doc 6.5c): the period's planned version against the current one and every
// open proposal: each changed cell with who, from and to, the cause and the decision behind it,
// plus the nights per day against target so a short night stands out.
import { useQuery } from "@tanstack/react-query";
import { api, type Nurse, type VersionOut } from "../../api/client";

const CAUSE: Record<string, string> = {
  request: "text-red-700",
  replacement: "text-emerald-700",
  knock_on: "text-amber-700",
  hand_edit: "text-slate-600",
};

function fmt(iso: string): string {
  return new Date(`${iso}T00:00:00`).toLocaleDateString("en-GB", { day: "numeric", month: "short" });
}

function Nights({ v, days }: { v: VersionOut; days: string[] }) {
  const short = v.night_totals.map((n, i) => (n < v.night_targets[i] ? i : -1)).filter((i) => i >= 0);
  return (
    <p className={`text-[11px] ${short.length ? "text-red-700" : "text-emerald-700"}`}>
      {short.length
        ? `Nights below target on ${short.map((i) => `${fmt(days[i])} (${v.night_totals[i]}/${v.night_targets[i]})`).join(", ")}`
        : "Every night at or above target"}
    </p>
  );
}

function Version({ v, days, nurses }: { v: VersionOut; days: string[]; nurses: Nurse[] }) {
  const name = (id: string) => nurses.find((n) => n.id === id)?.name ?? id;
  return (
    <section className="rounded-lg border border-slate-200 bg-white p-3" data-testid={`version-${v.label}`}>
      <header className="mb-1 flex items-center gap-2">
        <span
          className={`rounded px-1.5 py-0.5 text-[10px] font-semibold ${
            v.label === "current" ? "bg-emerald-100 text-emerald-800" : "bg-amber-100 text-amber-900"
          }`}
        >
          {v.label === "current" ? "Current (published)" : "Proposal"}
        </span>
        <span className="font-mono text-[10px] text-slate-400">{v.run.run_id}</span>
      </header>
      <Nights v={v} days={days} />
      <p className="mt-1 text-[11px] text-slate-500">
        {v.changes.length} cell{v.changes.length === 1 ? "" : "s"} differ from the plan
      </p>
      {v.changes.length > 0 && (
        <table className="mt-1 w-full text-[11px]">
          <thead className="text-left text-slate-400">
            <tr>
              <th>Day</th>
              <th>Nurse</th>
              <th>Plan → now</th>
              <th>Why</th>
            </tr>
          </thead>
          <tbody>
            {v.changes.map((c) => (
              <tr key={`${c.nurse_id}${c.day}`} className="border-t border-slate-100 align-top">
                <td className="whitespace-nowrap">{fmt(c.day)}</td>
                <td>{name(c.nurse_id)}</td>
                <td className="font-mono">
                  {c.from_shift ?? "off"} → {c.to_shift ?? "off"}
                </td>
                <td>
                  <span className={CAUSE[c.cause ?? ""] ?? ""}>{(c.cause ?? "change").replace("_", " ")}</span>
                  <span className="block text-[10px] text-slate-400">
                    {c.request_id ?? c.trigger}
                    {c.approved_by ? ` · approved by ${c.approved_by}` : ""}
                    {c.approved_at ? ` ${c.approved_at.slice(5, 16).replace("T", " ")}` : ""}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

export default function VersionsTab({ nurses }: { nurses: Nurse[] }) {
  const v = useQuery({ queryKey: ["versions"], queryFn: api.versions });
  if (!v.data) return <p className="text-xs text-slate-500">Loading…</p>;
  if (!v.data.planned) return <p className="text-xs text-slate-500">Nothing published for this period yet.</p>;
  return (
    <div className="space-y-3">
      <p className="text-[11px] text-slate-500">
        Planned version <span className="font-mono">{v.data.planned.run_id}</span> ({v.data.planned.trigger}) against
        what changed since.
      </p>
      {v.data.versions.map((x) => (
        <Version key={x.run.run_id} v={x} days={v.data.days} nurses={nurses} />
      ))}
    </div>
  );
}
