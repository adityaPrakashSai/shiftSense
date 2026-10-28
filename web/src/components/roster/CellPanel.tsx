// The clicked cell (design doc 10.3): its shift, whether and why it changed, its cost rows, which
// other shifts would break a rule, and "Why?" which asks the explainer about it.
import { useQuery } from "@tanstack/react-query";
import { api, type Cell, type Nurse } from "../../api/client";

type Evidence = {
  shift: string;
  prev_shift: string | null;
  changed: boolean | null;
  cause: string | null;
  costs: { tier: string; rule: string; amount: number; weight: number; detail: string | null }[];
  other_shifts: { shift: string; allowed: boolean; rules: string[] }[];
};

export default function CellPanel({
  cell,
  runId,
  nurses,
  onWhy,
  onClose,
}: {
  cell: Cell;
  runId: string;
  nurses: Nurse[];
  onWhy: () => void;
  onClose: () => void;
}) {
  const ev = useQuery({
    queryKey: ["cell", runId, cell.nurse_id, cell.day],
    queryFn: () => api.cellEvidence(runId, cell.nurse_id, cell.day) as Promise<Evidence>,
  });
  const name = nurses.find((n) => n.id === cell.nurse_id)?.name ?? cell.nurse_id;
  const e = ev.data;
  return (
    <section className="mb-3 rounded-lg border border-indigo-200 bg-white p-3 text-xs shadow-sm">
      <header className="mb-1 flex items-center gap-2">
        <h3 className="flex-1 text-sm font-semibold">
          {name} · {new Date(`${cell.day}T00:00:00`).toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short" })} ·{" "}
          {cell.display}
        </h3>
        <button onClick={onWhy} className="rounded bg-indigo-600 px-2 py-0.5 font-semibold text-white">
          Why?
        </button>
        <button onClick={onClose} className="text-slate-400 hover:text-slate-700" title="Close">
          ✕
        </button>
      </header>
      {e && (
        <div className="space-y-1 text-slate-600">
          {e.changed && (
            <p>
              Changed from {e.prev_shift ?? "off"}
              {e.cause ? ` (${e.cause.replace("_", "-")})` : ""}
            </p>
          )}
          {e.costs.length > 0 ? (
            <ul>
              {e.costs.map((c, i) => (
                <li key={i}>
                  <span className="font-mono">{c.rule}</span> {c.tier} · {c.amount}×{c.weight}
                  {c.detail ? ` · ${c.detail}` : ""}
                </li>
              ))}
            </ul>
          ) : (
            <p>No cost rows on this cell.</p>
          )}
          <p>
            {e.other_shifts
              .map((o) => `${o.shift}: ${o.allowed ? "allowed" : o.rules.map((r) => r.split(":")[0].replace(/_/g, " ")).join(", ")}`)
              .join(" · ")}
          </p>
        </div>
      )}
    </section>
  );
}
