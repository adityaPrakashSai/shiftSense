export default function ModeBadge({ mode }: { mode: string }) {
  if (mode === "mock")
    return <span className="rounded bg-yellow-300 px-2 py-0.5 text-xs font-bold text-yellow-950">MOCK MODE</span>;
  if (mode === "replay")
    return <span className="rounded bg-sky-200 px-2 py-0.5 text-xs font-bold text-sky-900">REPLAY</span>;
  return <span className="rounded bg-emerald-200 px-2 py-0.5 text-xs font-bold text-emerald-900">LIVE</span>;
}
