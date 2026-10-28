// My shifts (design doc 10.3, nurse view): the next 14 days of the published roster as chips,
// changed shifts flagged.
import type { RosterOut } from "../../api/client";
import { cellStyle } from "../roster/Cell";
import { t, type Lang } from "./i18n";

const HI_DAYS = ["Ravi", "Som", "Mangal", "Budh", "Guru", "Shukra", "Shani"];

function dayLabel(iso: string, lang: Lang): string {
  const d = new Date(`${iso}T00:00:00`);
  const wd = lang === "hi" ? HI_DAYS[d.getDay()] : d.toLocaleDateString("en-GB", { weekday: "short" });
  return `${wd} ${d.getDate()} ${d.toLocaleDateString("en-GB", { month: "short" })}`;
}

export default function MyShifts({
  roster,
  nurseId,
  lang,
  shiftNames,
}: {
  roster: RosterOut;
  nurseId: string;
  lang: Lang;
  shiftNames: Record<string, string>;
}) {
  const L = t(lang);
  const start = roster.today ?? roster.days[0];
  const cells = roster.grid
    .filter((c) => c.nurse_id === nurseId && c.day >= start)
    .sort((a, b) => a.day.localeCompare(b.day))
    .slice(0, 14);
  return (
    <ul className="divide-y divide-slate-100 rounded-lg border border-slate-200 bg-white">
      {cells.map((c) => {
        const isToday = c.day === roster.today;
        const label = c.shift ? shiftNames[c.shift] ?? c.shift : c.leave_type ?? (c.display === "N/O" ? "N/O" : L.off);
        return (
          <li key={c.day} className={`flex items-center gap-3 px-3 py-2 ${isToday ? "bg-rose-50" : ""}`}>
            <span className="w-24 text-sm text-slate-600">{dayLabel(c.day, lang)}</span>
            <span className={`rounded px-2 py-0.5 text-sm font-semibold ${c.shift ? cellStyle(c) : "bg-slate-100 text-slate-500"}`}>
              {label}
            </span>
            {c.changed && (
              <span className="ml-auto rounded bg-amber-100 px-1.5 py-0.5 text-xs text-amber-800">{L.changed}</span>
            )}
          </li>
        );
      })}
      {cells.length < 14 && <li className="px-3 py-2 text-xs text-slate-400">{L.notPublished}</li>}
    </ul>
  );
}
