// Ask tab (design doc 10.3, 7.4): why-questions answered from tool results only. Every fact is a
// claim chip that opens the cited JSON; an answer that fails the claim check is withheld and the
// deterministic explanation is shown instead.
import { useMutation } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { api, type ExplainBody, type ExplainOut } from "../../api/client";
import ClaimChip from "./ClaimChip";

export type AskPrefill = { question: string; context?: ExplainBody["context"]; runId?: string | null; n: number };

type Turn = { q: string; a?: ExplainOut; error?: string; started: number };

const SUGGESTIONS = ["Why was Arun chosen and not Priya?", "Is this roster proven optimal?", "Why was this request denied?"];

export default function AskTab({ runId, prefill }: { runId: string | null; prefill: AskPrefill | null }) {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [text, setText] = useState("");
  const seen = useRef(0);
  const ask = useMutation({
    mutationFn: (b: ExplainBody) => api.explain(b),
  });

  const send = (question: string, context?: ExplainBody["context"], run?: string | null) => {
    const q = question.trim();
    if (!q) return;
    const started = performance.now();
    setTurns((t) => [...t, { q, started }]);
    ask.mutate(
      { question: q, asker_id: "charge", run_id: run ?? runId, context: context ?? null },
      {
        onSuccess: (a) => setTurns((t) => t.map((x) => (x.started === started ? { ...x, a } : x))),
        onError: (e) => setTurns((t) => t.map((x) => (x.started === started ? { ...x, error: e.message } : x))),
      },
    );
  };

  useEffect(() => {
    if (prefill && prefill.n !== seen.current) {
      seen.current = prefill.n;
      send(prefill.question, prefill.context, prefill.runId);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [prefill]);

  return (
    <div className="space-y-3">
      {turns.length === 0 && (
        <div className="space-y-1">
          <p className="text-xs text-slate-500">Ask why the roster looks the way it does. Answers cite the evidence.</p>
          {SUGGESTIONS.map((s) => (
            <button
              key={s}
              onClick={() => send(s)}
              className="block w-full rounded border border-slate-200 bg-white px-2 py-1 text-left text-xs text-slate-700 hover:bg-slate-50"
            >
              {s}
            </button>
          ))}
        </div>
      )}
      {turns.map((t) => (
        <div key={t.started} className="space-y-1.5">
          <p className="ml-auto max-w-[90%] rounded-lg bg-indigo-600 px-3 py-1.5 text-sm text-white">{t.q}</p>
          {!t.a && !t.error && (
            <p className="animate-pulse rounded-lg bg-white px-3 py-2 text-sm text-slate-400 ring-1 ring-slate-200">
              Looking at the evidence…
            </p>
          )}
          {t.error && <p className="text-xs text-red-600">{t.error}</p>}
          {t.a && (
            <div className="rounded-lg bg-white px-3 py-2 text-sm ring-1 ring-slate-200">
              {t.a.fallback_used && (
                <p className="mb-1 rounded bg-amber-50 px-1.5 py-0.5 text-[11px] text-amber-800">
                  Model answer withheld: unverified claims. Showing the checked explanation.
                </p>
              )}
              <p className="text-slate-800">{t.a.answer}</p>
              {t.a.claims.length > 0 && (
                <div className="mt-1.5 flex flex-wrap gap-1">
                  {t.a.claims.map((c, i) => (
                    <ClaimChip key={i} claim={c} calls={t.a!.tool_calls} />
                  ))}
                </div>
              )}
              <p className="mt-1 text-[10px] text-slate-400">
                {t.a.tool_calls.map((c) => c.tool).join(" → ") || "no tools"} · {((t.a.latency_ms ?? 0) / 1000).toFixed(1)} s
                {t.a.grounded ? " · grounded" : ""}
              </p>
            </div>
          )}
        </div>
      ))}
      <form
        onSubmit={(e) => {
          e.preventDefault();
          send(text);
          setText("");
        }}
        className="flex gap-2"
      >
        <input
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder="Why Arun and not Priya?"
          className="min-w-0 flex-1 rounded border border-slate-300 px-2 py-1.5 text-sm focus:border-indigo-500 focus:outline-none"
        />
        <button
          type="submit"
          disabled={ask.isPending || !text.trim()}
          className="rounded bg-indigo-600 px-3 py-1.5 text-sm font-semibold text-white disabled:opacity-50"
        >
          Ask
        </button>
      </form>
    </div>
  );
}
