// Alerts tab (design doc 10.3): requests waiting for approval and open proposals, newest first.
// While a re-solve runs, a card shows the live run timer.
import { useQuery } from "@tanstack/react-query";
import { api, type Nurse } from "../../api/client";
import RunTimer, { type TierTick } from "../common/RunTimer";
import ConflictCard from "./ConflictCard";
import PendingApprovals from "./PendingApprovals";
import ProposalCard from "./ProposalCard";
import ReplacementMessages from "./ReplacementMessages";

export type SolvingTimer = { startedAt: number | null; stoppedAt: number | null; ticks: TierTick[]; serverSecs: number | null };

export default function AlertsTab({
  nurses,
  solving,
  timer,
  viewing,
  onView,
}: {
  nurses: Nurse[];
  solving: boolean;
  timer: SolvingTimer;
  viewing: string | null;
  onView: (runId: string) => void;
}) {
  const proposals = useQuery({ queryKey: ["proposals"], queryFn: api.proposals });
  const conflicts = useQuery({ queryKey: ["conflicts"], queryFn: api.conflicts });
  const rows = proposals.data ?? [];
  const open = conflicts.data ?? [];
  return (
    <div className="space-y-3">
      <PendingApprovals nurses={nurses} />
      {open.map((c) => (
        <ConflictCard key={c.conflict_id} c={c} />
      ))}
      {solving && (
        <div className="rounded-lg border border-indigo-200 bg-indigo-50 p-3">
          <p className="mb-1 text-sm font-semibold text-indigo-900">Re-solving and ranking replacements…</p>
          <RunTimer {...timer} />
        </div>
      )}
      {rows.map((p) => (
        <ProposalCard key={p.run_id} p={p} nurses={nurses} viewing={viewing} onView={onView} />
      ))}
      <ReplacementMessages nurses={nurses} />
      {!solving && rows.length === 0 && open.length === 0 && (
        <p className="rounded border border-dashed border-slate-300 p-4 text-center text-xs text-slate-500">
          No open alerts. Sick calls and requests confirmed by nurses show up here.
        </p>
      )}
    </div>
  );
}
