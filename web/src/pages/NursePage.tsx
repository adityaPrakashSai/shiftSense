// Nurse view (design doc 10.3): mobile first (360-430 px). Name and a demo identity selector on
// top, then Chat, Inbox and My shifts. Live through the ward socket like the charge view.
// `NurseView` is the same view inside the /demo phone frame, where the page owns the identity.
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api } from "../api/client";
import { useWardSocket, type WardMessage } from "../api/ws";
import ModeBadge from "../components/common/ModeBadge";
import Chat, { type Suggestion } from "../components/nurse/Chat";
import Inbox from "../components/nurse/Inbox";
import { t, type Lang } from "../components/nurse/i18n";
import MyShifts from "../components/nurse/MyShifts";

export default function NursePage() {
  const { nurseId = "" } = useParams();
  const navigate = useNavigate();
  return <NurseView nurseId={nurseId} onNurse={(id) => navigate(`/nurse/${id}`)} />;
}

export function NurseView({
  nurseId,
  onNurse,
  embedded = false,
  suggestion = null,
  onSuggestionSent,
}: {
  nurseId: string;
  onNurse: (id: string) => void;
  embedded?: boolean;
  suggestion?: Suggestion | null;
  onSuggestionSent?: () => void;
}) {
  const qc = useQueryClient();
  const ward = useQuery({ queryKey: ["ward"], queryFn: api.ward });
  const health = useQuery({ queryKey: ["health"], queryFn: api.health });
  const roster = useQuery({ queryKey: ["roster", null], queryFn: () => api.roster(null) });
  const nurse = ward.data?.nurses.find((n) => n.id === nurseId);
  const [langOverride, setLang] = useState<Lang | null>(null);
  const lang: Lang = langOverride ?? (nurse?.language === "en" ? "en" : "hi");
  const [tab, setTab] = useState<"chat" | "shifts" | "inbox">("chat");
  const inbox = useQuery({ queryKey: ["inbox", nurseId], queryFn: () => api.inbox(nurseId) });
  const asks = (inbox.data ?? []).filter((m) => m.status === "asked").length;
  const L = t(lang);

  // a suggested message from the demo bar is sent from the chat
  useEffect(() => {
    if (suggestion) setTab("chat");
  }, [suggestion]);

  const onMessage = useCallback(
    (m: WardMessage) => {
      if (m.type === "request_updated" || m.type === "run_finished")
        void qc.invalidateQueries({ queryKey: ["messages", nurseId] });
      if (m.type === "roster_published") void qc.invalidateQueries({ queryKey: ["roster"] });
      if (m.type === "message_for_nurse") void qc.invalidateQueries({ queryKey: ["inbox"] });
      if (m.type === "demo_reset") {
        setTab("chat");
        setLang(null);
      }
    },
    [qc, nurseId],
  );
  useWardSocket(ward.data?.id, onMessage, () => undefined);

  const shiftNames = Object.fromEntries((ward.data?.shifts ?? []).map((s) => [s.code, `${s.name} ${s.start}`]));

  return (
    <div className={`mx-auto flex max-w-[430px] flex-col bg-slate-50 text-slate-800 ${embedded ? "h-full" : "h-screen"}`}>
      <header className="border-b border-slate-200 bg-white px-3 py-2">
        <div className="flex items-center gap-2">
          <div className="min-w-0 flex-1">
            <h1 className="truncate text-base font-semibold">{nurse?.name ?? nurseId}</h1>
            <p className="text-xs text-slate-500">{ward.data?.name}</p>
          </div>
          {health.data && !embedded && <ModeBadge mode={health.data.llm_mode} />}
          <button
            onClick={() => setLang(lang === "en" ? "hi" : "en")}
            className="rounded border border-slate-300 px-2 py-0.5 text-xs text-slate-600"
            title="Language"
          >
            {lang === "en" ? "हिं" : "EN"}
          </button>
        </div>
        {/* on /demo the identity selector sits in the demo bar */}
        <div className={`mt-2 flex items-center gap-2 text-xs ${embedded ? "hidden" : ""}`}>
          <label className="text-slate-500" htmlFor="who">
            Demo identity
          </label>
          <select
            id="who"
            value={nurseId}
            onChange={(e) => onNurse(e.target.value)}
            className="min-w-0 flex-1 rounded border border-slate-300 bg-white px-1 py-0.5"
          >
            {(ward.data?.nurses ?? []).map((n) => (
              <option key={n.id} value={n.id}>
                {n.name} ({n.id})
              </option>
            ))}
          </select>
          {!embedded && (
            <Link to="/charge" className="text-indigo-600 hover:underline">
              Charge view
            </Link>
          )}
        </div>
        <nav className="mt-2 flex gap-1">
          {(["chat", "inbox", "shifts"] as const).map((k) => (
            <button
              key={k}
              onClick={() => setTab(k)}
              className={`flex-1 rounded-md py-1 text-sm font-medium ${tab === k ? "bg-indigo-600 text-white" : "bg-slate-100 text-slate-600"}`}
            >
              {k === "chat" ? L.chat : k === "inbox" ? L.inbox : L.myShifts}
              {k === "inbox" && asks > 0 && (
                <span className="ml-1 rounded-full bg-red-500 px-1.5 text-[10px] text-white" data-testid="inbox-badge">
                  {asks}
                </span>
              )}
            </button>
          ))}
        </nav>
      </header>
      <main className="min-h-0 flex-1">
        {tab === "chat" ? (
          <Chat key={nurseId} nurseId={nurseId} lang={lang} suggestion={suggestion} onSuggestionSent={onSuggestionSent} />
        ) : tab === "inbox" ? (
          <div className="h-full overflow-y-auto">
            <Inbox nurseId={nurseId} lang={lang} />
          </div>
        ) : (
          <div className="h-full overflow-y-auto p-3">
            {roster.data && <MyShifts roster={roster.data} nurseId={nurseId} lang={lang} shiftNames={shiftNames} />}
          </div>
        )}
      </main>
    </div>
  );
}
