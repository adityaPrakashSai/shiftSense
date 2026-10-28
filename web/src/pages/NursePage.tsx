// Nurse view (design doc 10.3): mobile first (360-430 px). Name and a demo identity selector on
// top, then My shifts and Chat. Live through the ward socket like the charge view.
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api } from "../api/client";
import { useWardSocket, type WardMessage } from "../api/ws";
import ModeBadge from "../components/common/ModeBadge";
import Chat from "../components/nurse/Chat";
import { t, type Lang } from "../components/nurse/i18n";
import MyShifts from "../components/nurse/MyShifts";

export default function NursePage() {
  const { nurseId = "" } = useParams();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const ward = useQuery({ queryKey: ["ward"], queryFn: api.ward });
  const health = useQuery({ queryKey: ["health"], queryFn: api.health });
  const roster = useQuery({ queryKey: ["roster", null], queryFn: () => api.roster(null) });
  const nurse = ward.data?.nurses.find((n) => n.id === nurseId);
  const [langOverride, setLang] = useState<Lang | null>(null);
  const lang: Lang = langOverride ?? (nurse?.language === "en" ? "en" : "hi");
  const [tab, setTab] = useState<"chat" | "shifts">("chat");
  const L = t(lang);

  const onMessage = useCallback(
    (m: WardMessage) => {
      if (m.type === "request_updated" || m.type === "run_finished")
        void qc.invalidateQueries({ queryKey: ["messages", nurseId] });
      if (m.type === "roster_published") void qc.invalidateQueries({ queryKey: ["roster"] });
    },
    [qc, nurseId],
  );
  useWardSocket(ward.data?.id, onMessage, () => undefined);

  const shiftNames = Object.fromEntries((ward.data?.shifts ?? []).map((s) => [s.code, `${s.name} ${s.start}`]));

  return (
    <div className="mx-auto flex h-screen max-w-[430px] flex-col bg-slate-50 text-slate-800">
      <header className="border-b border-slate-200 bg-white px-3 py-2">
        <div className="flex items-center gap-2">
          <div className="min-w-0 flex-1">
            <h1 className="truncate text-base font-semibold">{nurse?.name ?? nurseId}</h1>
            <p className="text-xs text-slate-500">{ward.data?.name}</p>
          </div>
          {health.data && <ModeBadge mode={health.data.llm_mode} />}
          <button
            onClick={() => setLang(lang === "en" ? "hi" : "en")}
            className="rounded border border-slate-300 px-2 py-0.5 text-xs text-slate-600"
            title="Language"
          >
            {lang === "en" ? "हिं" : "EN"}
          </button>
        </div>
        <div className="mt-2 flex items-center gap-2 text-xs">
          <label className="text-slate-500" htmlFor="who">
            Demo identity
          </label>
          <select
            id="who"
            value={nurseId}
            onChange={(e) => navigate(`/nurse/${e.target.value}`)}
            className="min-w-0 flex-1 rounded border border-slate-300 bg-white px-1 py-0.5"
          >
            {(ward.data?.nurses ?? []).map((n) => (
              <option key={n.id} value={n.id}>
                {n.name} ({n.id})
              </option>
            ))}
          </select>
          <Link to="/charge" className="text-indigo-600 hover:underline">
            Charge view
          </Link>
        </div>
        <nav className="mt-2 flex gap-1">
          {(["chat", "shifts"] as const).map((k) => (
            <button
              key={k}
              onClick={() => setTab(k)}
              className={`flex-1 rounded-md py-1 text-sm font-medium ${tab === k ? "bg-indigo-600 text-white" : "bg-slate-100 text-slate-600"}`}
            >
              {k === "chat" ? L.chat : L.myShifts}
            </button>
          ))}
        </nav>
      </header>
      <main className="min-h-0 flex-1">
        {tab === "chat" ? (
          <Chat key={nurseId} nurseId={nurseId} lang={lang} />
        ) : (
          <div className="h-full overflow-y-auto p-3">
            {roster.data && <MyShifts roster={roster.data} nurseId={nurseId} lang={lang} shiftNames={shiftNames} />}
          </div>
        )}
      </main>
    </div>
  );
}
