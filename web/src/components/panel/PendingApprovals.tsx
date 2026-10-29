// Leave and swap requests waiting for the charge nurse (Q7). A minimal stand-in until the
// Alerts tab (M7) takes over; Approve makes the request hard and starts a solve.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type Nurse } from "../../api/client";
import QueryState from "../common/QueryState";

function fmt(iso: string): string {
  const d = new Date(`${iso}T00:00:00`);
  return d.toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short" });
}

export default function PendingApprovals({ nurses }: { nurses: Nurse[] }) {
  const qc = useQueryClient();
  const pending = useQuery({
    queryKey: ["requests", "pending_approval"],
    queryFn: () => api.requests({ status: "pending_approval" }),
  });
  const done = () => void qc.invalidateQueries({ queryKey: ["requests"] });
  const approve = useMutation({ mutationFn: api.approve, onSettled: done });
  const decline = useMutation({ mutationFn: api.reject, onSettled: done });
  const name = (id: string | null | undefined) => nurses.find((n) => n.id === id)?.name ?? id ?? "";
  const rows = pending.data ?? [];
  if (pending.isPending) return null;
  return (
    <QueryState query={pending} label="pending approvals">
      {pending.data &&
        (rows.length === 0 ? null : (
          <section className="rounded-lg border border-amber-200 bg-amber-50 p-3">
            <h2 className="mb-2 text-sm font-semibold text-amber-900">Waiting for your approval</h2>
      <ul className="space-y-1.5">
        {rows.map((r) => (
          <li key={r.id} className="flex flex-wrap items-center gap-2 text-sm">
            <span className="font-mono text-xs text-slate-500">{r.id}</span>
            <span className="font-medium">{name(r.nurse_id)}</span>
            <span className="text-slate-700">
              {r.kind === "swap"
                ? `swap: gives ${r.dates.map(fmt).join(", ")} ${r.shifts?.join("") ?? ""}, takes ${name(r.counterpart_id)}'s ${r.counterpart_dates.map(fmt).join(", ")} ${r.counterpart_shifts?.join("") ?? ""}`
                : `${r.kind} · ${r.dates.map(fmt).join(", ")}`}
            </span>
            <span className="ml-auto flex gap-2">
              <button
                onClick={() => approve.mutate(r.id)}
                disabled={approve.isPending}
                className="rounded bg-emerald-700 px-2.5 py-1 text-xs font-semibold text-white hover:bg-emerald-800 disabled:opacity-60"
              >
                Approve
              </button>
              <button
                onClick={() => decline.mutate(r.id)}
                disabled={decline.isPending}
                className="rounded border border-slate-300 bg-white px-2.5 py-1 text-xs font-semibold text-slate-700"
              >
                Decline
              </button>
            </span>
          </li>
        ))}
      </ul>
          </section>
        ))}
    </QueryState>
  );
}
