// Live run timer (design doc 10.3): counts up from the Generate click with one decimal, ticks off
// each tier as its stage.done event arrives, stops when the draft roster is on screen.
import { useEffect, useState } from "react";

export type TierTick = { tier: string; secs: number; skipped: boolean; proven: boolean };

function joinTiers(tiers: string[]): string {
  if (tiers.length <= 2) return tiers.join(" and ");
  return `${tiers.slice(0, -1).join(", ")} and ${tiers[tiers.length - 1]}`;
}

export default function RunTimer({
  startedAt,
  stoppedAt,
  ticks,
  serverSecs,
  pending = [],
}: {
  startedAt: number | null;
  stoppedAt: number | null;
  ticks: TierTick[];
  serverSecs: number | null;
  pending?: string[];
}) {
  const [now, setNow] = useState(performance.now());
  useEffect(() => {
    if (startedAt === null || stoppedAt !== null) return;
    const id = window.setInterval(() => setNow(performance.now()), 100);
    return () => window.clearInterval(id);
  }, [startedAt, stoppedAt]);

  const elapsed = startedAt === null ? null : ((stoppedAt ?? now) - startedAt) / 1000;
  const running = startedAt !== null && stoppedAt === null;
  const unproven = ticks.some((t) => !t.proven);
  return (
    <div className="flex items-center gap-3 text-sm">
      <div className="text-right">
        {elapsed !== null && (
          <div className={`font-mono text-lg tabular-nums ${stoppedAt ? "text-emerald-700" : "text-slate-800"}`}>
            {`${elapsed.toFixed(1)} s`}
          </div>
        )}
        {running && ticks.length > 0 && pending.length > 0 && (
          <div className="text-[11px] text-slate-500">Roster found · improving {joinTiers(pending)}</div>
        )}
        {stoppedAt !== null && unproven && (
          <div className="text-[11px] text-slate-500">Best roster within the 9 s limit</div>
        )}
        <div className="text-[11px] text-slate-500">
          {serverSecs !== null ? `server ${serverSecs.toFixed(2)} s` : "By hand: about 2 hours"}
        </div>
      </div>
      {ticks.length > 0 && (
        <ul className="flex flex-wrap gap-1 text-[11px] text-slate-600">
          {ticks.map((t) => (
            <li key={t.tier} className="rounded bg-slate-100 px-1.5 py-0.5">
              {t.proven ? (
                <>
                  {t.tier} ✓{t.skipped ? "" : ` ${t.secs.toFixed(1)} s`}
                </>
              ) : (
                <>
                  {t.tier} · best in {t.secs.toFixed(1)} s
                </>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
