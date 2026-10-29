// Nurse x day grid (design doc 10.3): groups, weekend shading, today outlined, leave banners,
// night and weekend counts at the row end, daily totals in the footer.
import type { Cell as CellT, Nurse, RosterOut } from "../../api/client";
import Cell from "./Cell";
import CoverageFooter from "./CoverageFooter";

const WEEKDAY = ["Su", "Mo", "Tu", "We", "Th", "Fr", "Sa"];

function dayInfo(iso: string) {
  const d = new Date(`${iso}T00:00:00`);
  return { wd: d.getDay(), label: WEEKDAY[d.getDay()], num: d.getDate() };
}

type Group = { title: string; nurses: Nurse[] };

function groups(nurses: Nurse[]): Group[] {
  const ans = nurses.filter((n) => n.role === "ans");
  const rest = nurses.filter((n) => n.role !== "ans");
  const by = (pred: (n: Nurse) => boolean) => rest.filter(pred);
  return [
    { title: "Outside coverage (ANS)", nurses: ans },
    { title: "Senior", nurses: by((n) => !n.standby && n.grade === "senior") },
    { title: "Staff", nurses: by((n) => !n.standby && n.grade === "staff") },
    { title: "Trainee", nurses: by((n) => !n.standby && n.grade === "trainee") },
    { title: "Standby", nurses: by((n) => !!n.standby) },
  ].filter((g) => g.nurses.length > 0);
}

/** Merge runs of the same leave code into one banner cell. */
type CellClick = (c: CellT) => void;

function Row({
  cells,
  weekend,
  today,
  selected,
  onCell,
}: {
  cells: CellT[];
  weekend: Set<string>;
  today: string | null;
  selected?: string | null;
  onCell?: CellClick;
}) {
  const out = [];
  for (let i = 0; i < cells.length; ) {
    const c = cells[i];
    if (c.leave_type) {
      let j = i;
      while (j + 1 < cells.length && cells[j + 1].leave_type === c.leave_type) j++;
      out.push(
        <td key={c.day} colSpan={j - i + 1} className="h-7 px-0.5">
          <div
            title={`${c.nurse_id}: ${c.leave_type} ${c.day} to ${cells[j].day}`}
            className="flex h-6 items-center justify-center rounded bg-rose-100 text-xs font-semibold text-rose-800"
          >
            {c.leave_type}
          </div>
        </td>,
      );
      i = j + 1;
    } else {
      out.push(
        <Cell
          key={c.day}
          cell={c}
          weekend={weekend.has(c.day)}
          today={c.day === today}
          selected={selected === `${c.nurse_id}|${c.day}`}
          onClick={onCell}
        />,
      );
      i++;
    }
  }
  return <>{out}</>;
}

export default function RosterGrid({
  roster,
  nurses,
  selected,
  onCell,
}: {
  roster: RosterOut;
  nurses: Nurse[];
  selected?: string | null; // "N05|2026-11-03"
  onCell?: CellClick;
}) {
  const byNurse = new Map<string, CellT[]>();
  for (const c of roster.grid) {
    const list = byNurse.get(c.nurse_id) ?? [];
    list.push(c);
    byNurse.set(c.nurse_id, list);
  }
  const days = roster.days;
  const weekend = new Set(days.filter((d) => [0, 6].includes(dayInfo(d).wd)));
  const names = new Map<string, number>();
  for (const n of nurses) names.set(n.name, (names.get(n.name) ?? 0) + 1);
  const label = (n: Nurse) => (names.get(n.name)! > 1 ? `${n.name} (${n.id})` : n.name);

  return (
    <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white">
      <table className="border-collapse text-sm">
        <thead className="sticky top-0 bg-white">
          <tr className="border-b border-slate-200">
            <th className="sticky left-0 z-10 min-w-48 bg-white px-3 py-1 text-left font-medium text-slate-500">
              Nurse
            </th>
            {days.map((d) => {
              const { label: wd, num } = dayInfo(d);
              const isToday = d === roster.today;
              return (
                <th
                  key={d}
                  className={`w-9 py-1 text-center text-xs font-medium ${
                    weekend.has(d) ? "bg-slate-100 text-slate-700" : "text-slate-500"
                  } ${isToday ? "rounded-t bg-rose-50 text-rose-700" : ""}`}
                >
                  <div>{wd}</div>
                  <div className="text-sm text-slate-800">{num}</div>
                </th>
              );
            })}
            <th className="px-2 text-xs font-medium text-slate-500" title="shifts / nights / weekend shifts">
              Sh · N · We
            </th>
          </tr>
        </thead>
        {groups(nurses).map((g) => (
          <tbody key={g.title}>
            <tr>
              <td
                colSpan={days.length + 2}
                className="sticky left-0 bg-slate-50 px-3 py-0.5 text-xs font-semibold uppercase tracking-wide text-slate-500"
              >
                {g.title}
              </td>
            </tr>
            {g.nurses.map((n) => {
              const cells = byNurse.get(n.id) ?? [];
              const shifts = cells.filter((c) => c.shift && c.shift !== "G").length;
              const nights = cells.filter((c) => c.shift === "N").length;
              const we = cells.filter((c) => c.shift && weekend.has(c.day)).length;
              return (
                <tr key={n.id} className="border-b border-slate-100 hover:bg-slate-50/60">
                  <td className="sticky left-0 z-10 whitespace-nowrap bg-white px-3 text-slate-800">
                    <span className="mr-2 font-mono text-xs text-slate-500">{n.id}</span>
                    {label(n)}
                  </td>
                  <Row
                    cells={cells}
                    weekend={weekend}
                    today={roster.today ?? null}
                    selected={selected}
                    onCell={onCell}
                  />
                  <td className="whitespace-nowrap px-2 text-center font-mono text-xs text-slate-600">
                    {shifts} · {nights} · {we}
                  </td>
                </tr>
              );
            })}
          </tbody>
        ))}
        <CoverageFooter totals={roster.totals} days={days} weekend={weekend} />
      </table>
    </div>
  );
}
