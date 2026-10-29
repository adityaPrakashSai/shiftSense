// Active provider and model (design doc 7.9): the last call's provider when there is one, else the
// configured default; a "cached" tag when that answer came from the response cache. This is also
// the Stage One evidence that Meta models do the work.
import type { LLMUsage } from "../../api/client";

export const NAMES: Record<string, string> = {
  baseten: "Muse Spark Open on Baseten",
  meta: "Muse Spark on Meta Model API",
  modal: "Muse Spark Open on Modal",
  mock: "Mock (no model)",
};

export default function ProviderBadge({ usage }: { usage: LLMUsage }) {
  const last = usage.last_call;
  const provider = last?.provider ?? usage.default_provider;
  const spent = usage.spent_this_process_usd;
  return (
    <span className="flex items-center gap-1.5">
      <span
        className="rounded border border-slate-200 bg-white px-2 py-0.5 text-xs text-slate-600"
        title={last ? `${last.model} · last call: ${last.role}, ${last.latency_ms} ms` : "no calls yet"}
      >
        Model: {NAMES[provider] ?? provider}
        {last?.cache_hit && <span className="ml-1 rounded bg-sky-100 px-1 text-sky-800">cached</span>}
      </span>
      <span
        className="text-[11px] text-slate-500"
        title={`${usage.calls_today} calls today, ${Math.round(usage.cache_hit_rate * 100)}% cached`}
      >
        ${spent.toFixed(2)} / ${usage.spend_cap_usd.toFixed(2)}
      </span>
    </span>
  );
}
