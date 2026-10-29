// Nurse chat (design doc 10.3, 7.3): messages with the assistant. Confirmation bubbles show
// Confirm / Edit; Edit drops the parse and puts the text back in the input. Replies come from
// server-side templates, so a confirmation always states exactly what will be stored. The mic
// records a voice note (M12): Muse Spark Open transcribes it and the transcript is parsed like typed
// text; the bubble shows the transcript with a mic mark.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { api, type MessageRecord } from "../../api/client";
import ClaimChip from "../panel/ClaimChip";
import { useRecorder } from "../../lib/voice";
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

/** A message the /demo bar asks the chat of `nurseId` to send once: text, or a recorded clip. */
export type Suggestion = { nurseId: string; text: string; audioUrl?: string };

type Outgoing = { text: string } | { wav: Blob };

export default function Chat({
  nurseId,
  lang,
  suggestion = null,
  onSuggestionSent,
}: {
  nurseId: string;
  lang: Lang;
  suggestion?: Suggestion | null;
  onSuggestionSent?: () => void;
}) {
  const L = t(lang);
  const qc = useQueryClient();
  const [text, setText] = useState("");
  const [sent, setSent] = useState<string | null>(null);
  const end = useRef<HTMLDivElement>(null);
  const messages = useQuery({ queryKey: ["messages", nurseId], queryFn: () => api.messages(nurseId) });

  const refresh = () => void qc.invalidateQueries({ queryKey: ["messages", nurseId] });
  const send = useMutation({
    mutationFn: (m: Outgoing) => ("wav" in m ? api.sendVoice(nurseId, m.wav) : api.sendMessage(nurseId, m.text)),
    onMutate: (m) => setSent("wav" in m ? `🎤 ${L.voiceSending}` : m.text),
    // A message sent while the chat's first fetch is still in flight (a /demo chip right after
    // the page loads) must not be lost: TanStack Query dedupes a refetch into an in-flight fetch
    // that has no data yet, and that fetch read the list before the message was stored. So cancel
    // it, then fetch again; the pending bubble stays until the list with the message is back.
    onSettled: async () => {
      const key = ["messages", nurseId];
      await qc.cancelQueries({ queryKey: key });
      await qc.refetchQueries({ queryKey: key });
      setSent(null);
    },
  });
  const confirm = useMutation({ mutationFn: api.confirm, onSettled: refresh });
  const reject = useMutation({ mutationFn: api.reject, onSettled: refresh });

  // scroll the message list only (scrollIntoView would also move the page around the demo phone)
  useEffect(() => {
    const list = end.current?.parentElement;
    if (list) list.scrollTop = list.scrollHeight;
  }, [messages.data, sent]);

  // a suggestion is sent once by the chat it belongs to; the parent then clears it, and the ref
  // covers the effect running twice before that
  const handled = useRef<Suggestion | null>(null);
  useEffect(() => {
    if (!suggestion || suggestion.nurseId !== nurseId || handled.current === suggestion) return;
    handled.current = suggestion;
    if (suggestion.audioUrl) {
      setSent(`🎤 ${L.voiceSending}`);
      fetch(suggestion.audioUrl)
        .then((r) => r.blob())
        .then((wav) => send.mutate({ wav }))
        .catch(() => setSent(null));
    } else {
      send.mutate({ text: suggestion.text });
    }
    onSuggestionSent?.();
  }, [suggestion, nurseId, send, onSuggestionSent]);

  const submit = () => {
    const body = text.trim();
    if (!body || send.isPending) return;
    setText("");
    send.mutate({ text: body });
  };

  const [micError, setMicError] = useState<string | null>(null);
  const mic = useRecorder(
    (wav) => send.mutate({ wav }),
    (msg) => setMicError(msg),
  );

  const rows = messages.data ?? [];
  return (
    <div className="flex h-full flex-col">
      <div className="flex-1 space-y-3 overflow-y-auto p-3">
        {rows.length === 0 && !sent && <p className="text-center text-sm text-slate-500">{L.empty}</p>}
        {rows.map((m) => {
          const awaiting = m.reply_kind === "confirm" && m.requests.length > 0 && m.requests.every((r) => r.status === "pending_confirm");
          return (
            <div key={m.message_id} className="space-y-1.5">
              <div className="ml-auto max-w-[85%] rounded-2xl rounded-br-sm bg-indigo-600 px-3 py-2 text-sm text-white">
                {m.channel === "voice" && (
                  <span aria-label={L.voiceNote} title={L.voiceNote} className="mr-1">
                    🎤
                  </span>
                )}
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
                        className="rounded-md bg-emerald-700 px-3 py-1 text-xs font-semibold text-white hover:bg-emerald-800 disabled:opacity-60"
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
                  {m.claims.length > 0 && (
                    <div className="mt-1.5 flex flex-wrap gap-1">
                      {m.claims.map((c, i) => (
                        <ClaimChip key={i} claim={c as { text: string; call_id: string; path: string; value?: unknown }} />
                      ))}
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
            <div className="ml-auto max-w-[85%] rounded-2xl rounded-br-sm bg-indigo-600 px-3 py-2 text-sm text-white opacity-80">{sent}</div>
            <div className="max-w-[85%] animate-pulse rounded-2xl bg-white px-3 py-2 text-sm text-slate-500 ring-1 ring-slate-200">
              {L.thinking}
            </div>
          </div>
        )}
        {send.error && <p className="text-xs text-red-600">{send.error.message}</p>}
        {micError && <p className="text-xs text-red-600">{micError}</p>}
        <div ref={end} />
      </div>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
        className="flex items-center gap-2 border-t border-slate-200 bg-white p-2"
      >
        <button
          type="button"
          onClick={() => {
            setMicError(null);
            if (mic.recording) mic.stop();
            else void mic.start();
          }}
          disabled={send.isPending && !mic.recording}
          aria-label={mic.recording ? L.micStop : L.mic}
          title={mic.recording ? L.micStop : L.mic}
          className={`shrink-0 rounded-full px-2 py-2 text-sm ${mic.recording ? "animate-pulse bg-red-600 text-white" : "text-slate-600 hover:bg-slate-100"}`}
        >
          {mic.recording ? `● 0:${String(mic.seconds).padStart(2, "0")}` : "🎤"}
        </button>
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
