// Inbox (design doc 10.3, 6.5): asks from the charge nurse, "Can you cover Tue 3 Nov Morning
// (07:00-15:00)?", with Accept / Decline / "Yes, if…". Answering resumes the agent's workflow.
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  api,
  type ConditionCheckOut,
  type ConditionIn,
  type NurseMessageOut,
} from "../../api/client";
import QueryState from "../common/QueryState";
import { t, type Lang } from "./i18n";

function ConditionForm({
  m,
  lang,
  onBack,
}: {
  m: NurseMessageOut;
  lang: Lang;
  onBack: () => void;
}) {
  const L = t(lang);
  const qc = useQueryClient();
  const [text, setText] = useState("");
  const [checked, setChecked] = useState<ConditionCheckOut | null>(null);
  const [checkError, setCheckError] = useState<string | null>(null);
  const [checking, setChecking] = useState(false);
  const [day, setDay] = useState("");
  const [mode, setMode] = useState<"day_off" | "avoid_shift">("day_off");
  const [shift, setShift] = useState("M");
  const respond = useMutation({
    mutationFn: (condition: ConditionIn) => api.respond(m.id, true, condition),
    onSettled: () => void qc.invalidateQueries({ queryKey: ["inbox"] }),
  });
  const check = async () => {
    setChecking(true);
    setCheckError(null);
    try {
      setChecked(await api.checkCondition(m.id, text));
    } catch (e) {
      setChecked(null);
      setCheckError(e instanceof Error ? e.message : String(e));
    } finally {
      setChecking(false);
    }
  };
  const sendPicked = () => {
    if (!day) return;
    respond.mutate({ kind: mode, day, shift: mode === "avoid_shift" ? shift : null });
  };
  return (
    <div className="mt-2 space-y-2 rounded-md border border-indigo-200 bg-indigo-50/50 p-2">
      <div className="flex gap-2">
        <input
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder={L.condPlaceholder}
          data-testid="cond-text"
          className="min-w-0 flex-1 rounded-md border border-slate-300 px-2 py-1 text-sm"
        />
        <button
          onClick={check}
          disabled={checking || !text.trim() || respond.isPending}
          data-testid="cond-check"
          className="rounded-md bg-indigo-600 px-3 py-1 text-sm font-semibold text-white disabled:opacity-60"
        >
          {L.condCheck}
        </button>
      </div>
      {checkError && <p className="text-xs text-red-700">{checkError}</p>}
      {checked && (
        <div className="flex items-center gap-2">
          <span
            data-testid="cond-chip"
            className="rounded-full bg-indigo-100 px-3 py-1 text-xs font-semibold text-indigo-800"
          >
            {checked.chip}
          </span>
          <button
            onClick={() => respond.mutate(checked.condition)}
            disabled={respond.isPending}
            data-testid="cond-confirm"
            className="rounded-md bg-emerald-700 px-3 py-1 text-sm font-semibold text-white disabled:opacity-60"
          >
            {L.send}
          </button>
        </div>
      )}
      <div className="flex flex-wrap items-center gap-2">
        <input
          type="date"
          value={day}
          onChange={(e) => setDay(e.target.value)}
          data-testid="cond-date"
          className="rounded-md border border-slate-300 px-2 py-1 text-sm"
        />
        <div className="flex overflow-hidden rounded-md border border-slate-300 text-xs font-semibold">
          <button
            onClick={() => setMode("day_off")}
            data-testid="cond-mode-off"
            className={`px-2 py-1 ${mode === "day_off" ? "bg-indigo-600 text-white" : "bg-white text-slate-700"}`}
          >
            {L.condDayOff}
          </button>
          <button
            onClick={() => setMode("avoid_shift")}
            data-testid="cond-mode-avoid"
            className={`px-2 py-1 ${mode === "avoid_shift" ? "bg-indigo-600 text-white" : "bg-white text-slate-700"}`}
          >
            {L.condAvoid}
          </button>
        </div>
        {mode === "avoid_shift" && (
          <select
            value={shift}
            onChange={(e) => setShift(e.target.value)}
            data-testid="cond-shift"
            className="rounded-md border border-slate-300 px-2 py-1 text-sm"
          >
            <option value="M">M</option>
            <option value="E">E</option>
            <option value="N">N</option>
          </select>
        )}
        <button
          onClick={sendPicked}
          disabled={!day || respond.isPending}
          data-testid="cond-send"
          className="rounded-md bg-emerald-700 px-3 py-1 text-sm font-semibold text-white disabled:opacity-60"
        >
          {L.send}
        </button>
        <button onClick={onBack} className="ml-auto text-xs font-semibold text-slate-500">
          {L.condBack}
        </button>
      </div>
    </div>
  );
}

function statusLine(m: NurseMessageOut, L: ReturnType<typeof t>) {
  if (m.status === "accepted") return { text: L.accepted, cls: "text-emerald-700" };
  if (m.status === "conditional") return { text: L.conditionalSent, cls: "text-indigo-700" };
  if (m.status === "expired") return { text: L.expired, cls: "text-slate-500" };
  if (m.status === "withdrawn") return { text: L.withdrawn, cls: "text-slate-500" };
  return { text: L.declined, cls: "text-slate-500" };
}

export default function Inbox({ nurseId, lang }: { nurseId: string; lang: Lang }) {
  const L = t(lang);
  const qc = useQueryClient();
  const [condOpen, setCondOpen] = useState<string | null>(null);
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
            condOpen === m.id ? (
              <ConditionForm m={m} lang={lang} onBack={() => setCondOpen(null)} />
            ) : (
            <div className="mt-2 flex gap-2">
              <button
                onClick={() => respond.mutate({ id: m.id, accept: true })}
                disabled={respond.isPending}
                className="flex-1 rounded-md bg-emerald-700 py-1.5 text-sm font-semibold text-white disabled:opacity-60"
              >
                {L.accept}
              </button>
              {m.can_condition && (
                <button
                  onClick={() => setCondOpen(m.id)}
                  disabled={respond.isPending}
                  data-testid="yes-if"
                  className="flex-1 rounded-md border border-indigo-300 bg-indigo-50 py-1.5 text-sm font-semibold text-indigo-700 disabled:opacity-60"
                >
                  {L.yesIf}
                </button>
              )}
              <button
                onClick={() => respond.mutate({ id: m.id, accept: false })}
                disabled={respond.isPending}
                className="flex-1 rounded-md border border-slate-300 py-1.5 text-sm font-semibold text-slate-700 disabled:opacity-60"
              >
                {L.decline}
              </button>
            </div>
            )
          ) : (
            <p className={`mt-2 text-xs font-semibold ${statusLine(m, L).cls}`}>
              {statusLine(m, L).text}
            </p>
          )}
        </li>
      ))}
          </ul>
        ))}
    </QueryState>
  );
}
