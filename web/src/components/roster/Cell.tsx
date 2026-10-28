import type { Cell as CellT } from "../../api/client";

const SHIFT_STYLE: Record<string, string> = {
  M: "bg-amber-100 text-amber-900",
  E: "bg-sky-100 text-sky-900",
  N: "bg-indigo-700 text-white",
  G: "bg-slate-200 text-slate-700",
};

export function cellStyle(c: CellT): string {
  if (c.shift) return SHIFT_STYLE[c.shift] ?? "bg-slate-100";
  if (c.display === "N/O") return "text-indigo-400";
  return "text-slate-300";
}

export default function Cell({ cell, weekend, today }: { cell: CellT; weekend: boolean; today: boolean }) {
  const changed = cell.changed ? "ring-2 ring-inset ring-amber-500" : "";
  const outline = today ? "border-x-2 border-x-rose-400" : "";
  const shade = weekend && !cell.shift ? "bg-slate-50" : "";
  const label = cell.shift ?? (cell.display === "D/O" ? "·" : cell.display);
  const title = `${cell.nurse_id} ${cell.day}: ${cell.display}${
    cell.changed ? ` (was ${cell.prev_shift ?? "off"})` : ""
  }`;
  return (
    <td
      title={title}
      className={`h-7 w-9 min-w-9 text-center text-xs font-medium ${shade} ${outline} ${changed}`}
    >
      <span className={`mx-auto flex h-6 w-8 items-center justify-center rounded ${cellStyle(cell)}`}>
        {label}
      </span>
    </td>
  );
}
