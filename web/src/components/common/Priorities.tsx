// Priority chips (design doc 10.3, 5.3, P1): the tier order the solver uses, editable with the
// arrows; the next solve uses the new order.
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../../api/client";

export default function Priorities({ priorities }: { priorities: string[] }) {
  const qc = useQueryClient();
  const save = useMutation({
    mutationFn: api.setPriorities,
    onSuccess: (ward) => qc.setQueryData(["ward"], ward),
  });
  const move = (i: number, d: -1 | 1) => {
    const next = [...priorities];
    [next[i], next[i + d]] = [next[i + d], next[i]];
    save.mutate(next);
  };
  return (
    <div className="mt-2 flex flex-wrap items-center gap-2 text-xs" data-testid="priorities">
      <span className="text-slate-500">Priorities</span>
      {priorities.map((p, i) => (
        <span key={p} className="flex items-center gap-0.5 rounded-full bg-slate-100 py-0.5 pl-2 pr-1 text-slate-700">
          {i + 1}. {p}
          <button
            onClick={() => move(i, -1)}
            disabled={i === 0 || save.isPending}
            className="px-0.5 text-slate-400 hover:text-slate-800 disabled:opacity-30"
            aria-label={`Move ${p} up`}
          >
            ‹
          </button>
          <button
            onClick={() => move(i, 1)}
            disabled={i === priorities.length - 1 || save.isPending}
            className="px-0.5 text-slate-400 hover:text-slate-800 disabled:opacity-30"
            aria-label={`Move ${p} down`}
          >
            ›
          </button>
        </span>
      ))}
      {save.error && <span className="text-red-600">{save.error.message}</span>}
    </div>
  );
}
