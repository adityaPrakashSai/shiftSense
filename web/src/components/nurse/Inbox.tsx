// Inbox (design doc 10.3, 6.5): asks from the charge nurse, "Can you cover Tue 3 Nov Morning
// (07:00-15:00)?", with Accept / Decline. Answering resumes the agent's workflow.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../api/client";
import QueryState from "../common/QueryState";
import { t, type Lang } from "./i18n";

export default function Inbox({ nurseId, lang }: { nurseId: string; lang: Lang }) {
  const L = t(lang);
  const qc = useQueryClient();
  const inbox = useQuery({ queryKey: ["inbox", nurseId], queryFn: () => api.inbox(nurseId) });
  const respond = useMutation({
    mutationFn: ({ id, accept }: { id: string; accept: boolean }) => api.respond(id, accept),
    onSettled: () => void qc.invalidateQueries({ queryKey: ["inbox"] }),
  });
  const rows = inbox.data ?? [];
  return (
    <QueryState query={inbox} label="inbox">
      {inbox.data &&
        (rows.length === 0 ? (
          <p className="p-4 text-center text-sm text-slate-500">{L.inboxEmpty}</p>
        ) : (
          <ul className="space-y-3 p-3">
      {rows.map((m) => (
        <li key={m.id} className="rounded-lg border border-slate-200 bg-white p-3 shadow-sm" data-testid="inbox-item">
          <p className="text-sm text-slate-800">{m.text}</p>
          {m.status === "asked" ? (
            <div className="mt-2 flex gap-2">
              <button
                onClick={() => respond.mutate({ id: m.id, accept: true })}
                disabled={respond.isPending}
                className="flex-1 rounded-md bg-emerald-700 py-1.5 text-sm font-semibold text-white disabled:opacity-60"
              >
                {L.accept}
              </button>
              <button
                onClick={() => respond.mutate({ id: m.id, accept: false })}
                disabled={respond.isPending}
                className="flex-1 rounded-md border border-slate-300 py-1.5 text-sm font-semibold text-slate-700 disabled:opacity-60"
              >
                {L.decline}
              </button>
            </div>
          ) : (
            <p className={`mt-2 text-xs font-semibold ${m.status === "accepted" ? "text-emerald-700" : "text-slate-500"}`}>
              {m.status === "accepted" ? L.accepted : m.status === "expired" ? L.expired : m.status === "withdrawn" ? L.withdrawn : L.declined}
            </p>
          )}
        </li>
      ))}
          </ul>
        ))}
    </QueryState>
  );
}
