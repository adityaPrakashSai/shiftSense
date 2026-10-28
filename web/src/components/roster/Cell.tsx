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

// proposal diff (6.3): the cell the request removed is struck through in red, the replacement is
// outlined green, any other change amber
const CAUSE_RING: Record<string, string> = {
  replacement: "ring-2 ring-inset ring-emerald-500",
  knock_on: "ring-2 ring-inset ring-amber-500",
  request: "ring-2 ring-inset ring-red-400",
};

export default function Cell({ cell, weekend, today }: { cell: CellT; weekend: boolean; today: boolean }) {
  const changed = cell.changed ? CAUSE_RING[cell.cause ?? "knock_on"] ?? CAUSE_RING.knock_on : "";
  const outline = today ? "border-x-2 border-x-rose-400" : "";
  const shade = weekend && !cell.shift ? "bg-slate-50" : "";
  const removed = cell.cause === "request" && cell.prev_shift;
  const label = removed ? cell.prev_shift : cell.shift ?? (cell.display === "D/O" ? "·" : cell.display);
  const title = `${cell.nurse_id} ${cell.day}: ${cell.display}${
    cell.changed ? ` (was ${cell.prev_shift ?? "off"}${cell.cause ? `, ${cell.cause.replace("_", "-")}` : ""})` : ""
  }`;
  return (
    <td
      title={title}
      className={`h-7 w-9 min-w-9 text-center text-xs font-medium ${shade} ${outline} ${changed}`}
    >
      <span
        className={`mx-auto flex h-6 w-8 items-center justify-center rounded ${
          removed ? "bg-red-50 text-red-600 line-through" : cellStyle(cell)
        }`}
      >
        {label}
      </span>
    </td>
  );
}
