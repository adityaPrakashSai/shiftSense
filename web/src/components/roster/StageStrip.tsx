// One pill per priority tier (design doc 10.3): value, proven or best found, seconds.
import type { RunDetail, StageOut } from "../../api/client";

function text(s: StageOut, detail?: RunDetail): string {
  const denied = detail?.request_outcomes.filter((o) => o.status === "DENIED").length ?? 0;
  const soft = detail?.request_outcomes.filter((o) => o.reason_code !== "HARD").length ?? 0;
  switch (s.tier) {
    case "coverage":
      return `Coverage ${s.best} short`;
    case "stability":
      return s.status === "skip" ? "Stability: new roster" : `Stability ${s.best} changes`;
    case "contract":
      return `Contract ${s.best} under`;
    case "requests":
      return detail ? `Requests ${denied} of ${soft} denied · cost ${s.best}` : `Requests ${s.best}`;
    case "fairness":
      return `Fairness ${s.best}`;
    default:
      return `${s.tier} ${s.best}`;
  }
}

export default function StageStrip({ stages, detail }: { stages: StageOut[]; detail?: RunDetail }) {
  return (
    <div className="flex flex-wrap gap-2">
      {stages.map((s) => {
        const skip = s.status === "skip";
        const tone = skip
          ? "border-slate-200 bg-slate-50 text-slate-500"
          : s.proven
            ? "border-emerald-200 bg-emerald-50 text-emerald-800"
            : "border-amber-200 bg-amber-50 text-amber-800";
        return (
          <span
            key={s.rank}
            title={s.locked ? `locked: ${s.locked}; bound ${s.bound}; ${s.secs.toFixed(2)} s` : undefined}
            className={`rounded-full border px-3 py-1 text-xs font-medium ${tone}`}
          >
            {text(s, detail)}
            {!skip && (s.proven ? " ✓ proven" : " · best found")}
          </span>
        );
      })}
    </div>
  );
}
