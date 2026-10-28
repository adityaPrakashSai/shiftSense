const NAMES: Record<string, string> = {
  baseten: "Muse Spark Open on Baseten",
  meta: "Muse Spark on Meta Model API",
  modal: "Muse Spark Open on Modal",
};

export default function ProviderBadge({ provider, model }: { provider: string; model: string | null }) {
  return (
    <span className="rounded border border-slate-200 bg-white px-2 py-0.5 text-xs text-slate-600" title={model ?? ""}>
      Model: {NAMES[provider] ?? provider}
    </span>
  );
}
