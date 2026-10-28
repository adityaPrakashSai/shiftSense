// Conflict card (design doc 10.3, 5.5): the solver cannot meet every hard rule. Shows Muse's short
// summary (or the template text), why the other nurses cannot help (rules nobody can waive), and
// the options, which always come from code. Choosing one starts a new run.
import { useMutation, useQueryClient } from "@tanstack/react-query";
import type { ConflictOut } from "../../api/client";
import { api } from "../../api/client";
import { ruleLabels } from "./rules";

export default function ConflictCard({ c }: { c: ConflictOut }) {
  const qc = useQueryClient();
  const resolve = useMutation({
    mutationFn: (optionId: string) => api.resolveConflict(c.conflict_id, optionId),
    onSettled: () => void qc.invalidateQueries({ queryKey: ["conflicts"] }),
  });
  const labels = new Map((c.narrative?.options ?? []).map((o) => [o.option_id, o.label]));
  return (
    <article className="rounded-lg border border-red-200 bg-white p-3 shadow-sm">
      <header className="mb-1 flex items-center gap-2">
        <span className="rounded bg-red-100 px-1.5 py-0.5 text-[11px] font-semibold text-red-800">Conflict</span>
        <span className="font-mono text-[11px] text-slate-500">{c.conflict_id}</span>
        {c.narrative && (
          <span className="ml-auto text-[10px] text-slate-400">
            {c.narrative.source === "model" ? "summary by Muse Spark" : "template text"}
          </span>
        )}
      </header>
      <p className="text-sm text-slate-800">
        {c.narrative?.summary ?? "The solver cannot meet every hard rule here. Pick one option."}
      </p>
      <p className="mt-1 font-mono text-[10px] text-slate-400" title="unsat core: waivable rules that cannot all hold">
        {c.core.join(" · ")}
      </p>
      <ul className="mt-2 space-y-1.5">
        {c.options.map((o) => (
          <li key={o.option_id} className="flex items-center gap-2 rounded border border-slate-100 px-2 py-1.5 text-sm">
            <span className="w-6 text-xs font-semibold text-slate-400">{o.option_id}</span>
            <span className="flex-1" title={o.waive ? `waives ${o.waive}` : "adds a temporary nurse"}>
              {labels.get(o.option_id) ?? o.label}
            </span>
            <button
              onClick={() => resolve.mutate(o.option_id)}
              disabled={resolve.isPending}
              className="rounded bg-slate-800 px-2 py-1 text-xs font-semibold text-white hover:bg-slate-700 disabled:opacity-60"
            >
              Choose
            </button>
          </li>
        ))}
      </ul>
      {c.context.length > 0 && (
        <details className="mt-2 text-xs text-slate-600">
          <summary className="cursor-pointer text-slate-500">Why nobody else can cover</summary>
          <ul className="mt-1 space-y-0.5">
            {c.context.map((x) => (
              <li key={x.nurse} title={x.rules.join(", ")}>
                <span className="font-medium">{x.name}</span>: {ruleLabels(x.rules).join(", ")}
              </li>
            ))}
          </ul>
        </details>
      )}
      {resolve.error && <p className="mt-1 text-xs text-red-600">{resolve.error.message}</p>}
    </article>
  );
}
