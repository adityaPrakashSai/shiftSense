// Daily totals row (design doc 10.3): assigned vs target per day and shift; red below target,
// amber above the maximum. Computed by app/planner/totals.py, the same function as the import
// checksum.
import type { DayTotal } from "../../api/client";

const SHIFTS = [
  ["M", "Morning"],
  ["E", "Evening"],
  ["N", "Night"],
] as const;

export default function CoverageFooter({
  totals,
  days,
  weekend,
}: {
  totals: DayTotal[];
  days: string[];
  weekend: Set<string>;
}) {
  const at = new Map(totals.map((t) => [`${t.day}:${t.shift}`, t]));
  return (
    <tfoot className="border-t-2 border-slate-300">
      {SHIFTS.map(([s, name]) => (
        <tr key={s}>
          <td className="sticky left-0 z-10 bg-white px-3 py-0.5 text-xs font-medium text-slate-600">
            {name} assigned / target
          </td>
          {days.map((d) => {
            const t = at.get(`${d}:${s}`);
            if (!t) return <td key={d} />;
            const under = t.assigned < t.target;
            const over = t.assigned > t.max;
            const tone = under
              ? "bg-red-100 text-red-800 font-semibold"
              : over
                ? "bg-amber-100 text-amber-800"
                : weekend.has(d)
                  ? "bg-slate-50 text-slate-600"
                  : "text-slate-600";
            const range = t.max !== t.target ? ` (max ${t.max})` : "";
            return (
              <td
                key={d}
                title={`${d} ${name}: ${t.assigned} assigned, target ${t.target}${range}, floor ${t.min}`}
                className={`text-center font-mono text-[11px] ${tone}`}
              >
                {t.assigned}/{t.target}
              </td>
            );
          })}
          <td />
        </tr>
      ))}
    </tfoot>
  );
}
