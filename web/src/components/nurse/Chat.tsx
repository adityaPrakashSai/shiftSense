// Nurse chat (design doc 10.3, 7.3): messages with the assistant. Confirmation bubbles show
// Confirm / Edit; Edit drops the parse and puts the text back in the input. Replies come from
// server-side templates, so a confirmation always states exactly what will be stored.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { api, type MessageRecord } from "../../api/client";
import { t, type Lang } from "./i18n";

function Status({ m, lang }: { m: MessageRecord; lang: Lang }) {
  const L = t(lang);
  if (!m.requests.length) return null;
  return (
    <div className="mt-1 space-y-0.5">
      {m.requests.map((r) => {
        const o = r.outcome;
        const result = r.status === "active" && o ? (o.status === "DENIED" ? L.denied : L.granted) : null;
        return (
          <p key={r.id} className="text-[11px] text-slate-500">
            <span className="font-mono">{r.id}</span> · {L.status[r.status] ?? r.status}
            {result && ` · ${result}`}
          </p>
        );
      })}
    </div>
  );
}

export default function Chat({ nurseId, lang }: { nurseId: string; lang: Lang }) {
  const L = t(lang);
  const qc = useQueryClient();
  const [text, setText] = useState("");
  const [sent, setSent] = useState<string | null>(null);
  const end = useRef<HTMLDivElement>(null);
  const messages = useQuery({ queryKey: ["messages", nurseId], queryFn: () => api.messages(nurseId) });

  const refresh = () => void qc.invalidateQueries({ queryKey: ["messages", nurseId] });
  const send = useMutation({
    mutationFn: (body: string) => api.sendMessage(nurseId, body),
    onMutate: (body) => setSent(body),
    onSettled: () => {
      setSent(null);
      refresh();
    },
  });
  const confirm = useMutation({ mutationFn: api.confirm, onSettled: refresh });
  const reject = useMutation({ mutationFn: api.reject, onSettled: refresh });

  useEffect(() => {
    end.current?.scrollIntoView({ block: "end" });
  }, [messages.data, sent]);

  const submit = () => {
    const body = text.trim();
    if (!body || send.isPending) return;
    setText("");
    send.mutate(body);
  };

  const rows = messages.data ?? [];
  return (
    <div className="flex h-full flex-col">
      <div className="flex-1 space-y-3 overflow-y-auto p-3">
        {rows.length === 0 && !sent && <p className="text-center text-sm text-slate-400">{L.empty}</p>}
        {rows.map((m) => {
          const awaiting = m.reply_kind === "confirm" && m.requests.length > 0 && m.requests.every((r) => r.status === "pending_confirm");
          return (
            <div key={m.message_id} className="space-y-1.5">
              <div className="ml-auto max-w-[85%] rounded-2xl rounded-br-sm bg-indigo-600 px-3 py-2 text-sm text-white">
                {m.body}
              </div>
              {m.reply && (
                <div
                  className={`max-w-[85%] whitespace-pre-line rounded-2xl rounded-bl-sm px-3 py-2 text-sm ${
                    m.reply_kind === "refuse" ? "bg-slate-200 text-slate-700" : m.reply_kind === "clarify" ? "bg-amber-50 text-amber-900 ring-1 ring-amber-200" : "bg-white text-slate-800 ring-1 ring-slate-200"
                  }`}
                >
                  {m.reply}
                  {awaiting && (
                    <div className="mt-2 flex gap-2">
                      <button
                        onClick={() => confirm.mutate(m.requests[0].id)}
                        disabled={confirm.isPending}
                        className="rounded-md bg-emerald-600 px-3 py-1 text-xs font-semibold text-white hover:bg-emerald-500 disabled:opacity-60"
                      >
                        {L.confirm}
                      </button>
                      <button
                        onClick={() => {
                          reject.mutate(m.requests[0].id);
                          setText(m.body);
                        }}
                        disabled={reject.isPending}
                        className="rounded-md border border-slate-300 px-3 py-1 text-xs font-semibold text-slate-700 hover:bg-slate-50"
                      >
                        {L.edit}
                      </button>
                    </div>
                  )}
                  <Status m={m} lang={lang} />
                </div>
              )}
            </div>
          );
        })}
        {sent && (
          <div className="space-y-1.5">
            <div className="ml-auto max-w-[85%] rounded-2xl rounded-br-sm bg-indigo-400 px-3 py-2 text-sm text-white">{sent}</div>
            <div className="max-w-[85%] animate-pulse rounded-2xl bg-white px-3 py-2 text-sm text-slate-400 ring-1 ring-slate-200">
              {L.thinking}
            </div>
          </div>
        )}
        {send.error && <p className="text-xs text-red-600">{send.error.message}</p>}
        <div ref={end} />
      </div>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
        className="flex items-center gap-2 border-t border-slate-200 bg-white p-2"
      >
        <button type="button" disabled title={L.mic} className="rounded-full p-2 text-slate-300">🎤</button>
        <button type="button" disabled title={L.camera} className="rounded-full p-2 text-slate-300">📷</button>
        <input
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder={L.placeholder}
          className="min-w-0 flex-1 rounded-full border border-slate-300 px-3 py-2 text-sm focus:border-indigo-500 focus:outline-none"
        />
        <button
          type="submit"
          disabled={!text.trim() || send.isPending}
          className="rounded-full bg-indigo-600 px-4 py-2 text-sm font-semibold text-white disabled:opacity-50"
        >
          {L.send}
        </button>
      </form>
    </div>
  );
}
