// Replacement messages (design doc 7.7, 6.5): the asks the agent sent after approval, with each
// nurse's answer, so the charge nurse sees "Arun accepted" without leaving Alerts.
import { useQuery } from "@tanstack/react-query";
import { api, type Nurse } from "../../api/client";
import QueryState from "../common/QueryState";

const TONE: Record<string, string> = {
  asked: "bg-sky-100 text-sky-900",
  accepted: "bg-emerald-100 text-emerald-800",
  declined: "bg-red-100 text-red-800",
  noted: "bg-slate-100 text-slate-600",
};

export default function ReplacementMessages({ nurses }: { nurses: Nurse[] }) {
  const inbox = useQuery({ queryKey: ["inbox", "all"], queryFn: () => api.inbox() });
  const rows = (inbox.data ?? []).slice(0, 5);
  if (inbox.isPending) return null;
  const name = (id: string) => nurses.find((n) => n.id === id)?.name ?? id;
  return (
    <QueryState query={inbox} label="messages to nurses">
      {inbox.data &&
        (rows.length === 0 ? null : (
          <section className="rounded-lg border border-slate-200 bg-white p-3">
            <h3 className="mb-1 text-sm font-semibold">Messages to nurses</h3>
      <ul className="space-y-2">
        {rows.map((m) => (
          <li key={m.id} className="text-xs" data-testid="sent-message">
            <div className="flex items-center gap-2">
              <span className="font-medium">{name(m.nurse_id)}</span>
              <span className={`rounded px-1 text-[10px] font-semibold ${TONE[m.status] ?? ""}`}>{m.status}</span>
              <span className="ml-auto text-[10px] text-slate-500">
                {m.kind} · {m.source === "model" ? "written by Muse" : "template"}
              </span>
            </div>
            <p className="mt-0.5 text-slate-600">{m.text}</p>
          </li>
        ))}
      </ul>
          </section>
        ))}
    </QueryState>
  );
}
