// Demo view (design doc 10.3, 2.2): the charge nurse view at 65% on the left and the nurse view in
// a phone frame at 35% on the right. The top bar has Reset demo, the phone's identity selector,
// the model banner and three suggested messages; a chip switches the phone to its nurse and sends
// the message from her chat. Below the lg breakpoint (a phone or tablet) one pane shows at a time.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useState } from "react";
import { api } from "../api/client";
import ModeBadge from "../components/common/ModeBadge";
import { NAMES } from "../components/common/ProviderBadge";
import type { Suggestion } from "../components/nurse/Chat";
import PhoneFrame from "../components/nurse/PhoneFrame";
import { ChargeView } from "./ChargePage";
import { NurseView } from "./NursePage";

const START_NURSE = "N03"; // Meena, who sends the sick call (2.2 step 2)

const CHIPS: { label: string; nurseId: string; text: string }[] = [
  { label: "Sick call (Hindi)", nurseId: "N03", text: "Sunita di, aaj subah ki duty nahi kar paungi, bukhar hai" },
  { label: "Day off (English)", nurseId: "N04", text: "Can I get a day off on Saturday 7 Nov? It's my sister's engagement." },
  { label: "Why every weekend?", nurseId: "N02", text: "Why do I get every weekend?" },
];

/** Tailwind's lg breakpoint, as state: one nurse view is mounted, framed or full screen. */
function useWide(): boolean {
  const query = "(min-width: 1024px)";
  const [wide, setWide] = useState(() => window.matchMedia(query).matches);
  useEffect(() => {
    const mq = window.matchMedia(query);
    const on = () => setWide(mq.matches);
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, []);
  return wide;
}

export default function DemoPage() {
  const wide = useWide();
  const qc = useQueryClient();
  const health = useQuery({ queryKey: ["health"], queryFn: api.health });
  const ward = useQuery({ queryKey: ["ward"], queryFn: api.ward });
  const [nurseId, setNurseId] = useState(START_NURSE);
  const [suggestion, setSuggestion] = useState<Suggestion | null>(null);
  const [pane, setPane] = useState<"charge" | "nurse">("charge");
  const clearSuggestion = useCallback(() => setSuggestion(null), []);
  const names = Object.fromEntries((ward.data?.nurses ?? []).map((n) => [n.id, n.name.split(" ")[0]]));

  const reset = useMutation({
    mutationFn: api.reset,
    onSuccess: () => {
      setNurseId(START_NURSE);
      setSuggestion(null);
      void qc.invalidateQueries();
    },
  });

  const provider = health.data?.provider ?? "baseten";
  const nurse = (
    <NurseView
      nurseId={nurseId}
      onNurse={setNurseId}
      embedded
      suggestion={suggestion}
      onSuggestionSent={clearSuggestion}
    />
  );
  return (
    <div className="flex h-screen flex-col bg-slate-100 text-slate-800">
      <header className="flex flex-wrap items-center gap-x-4 gap-y-2 border-b border-slate-300 bg-white px-4 py-2">
        <div className="flex items-baseline gap-2">
          <span className="text-base font-semibold">ShiftSense</span>
          <span className="text-xs text-slate-500">demo</span>
        </div>
        <span className="flex items-center gap-1.5 rounded-md bg-indigo-50 px-2 py-1 text-xs font-medium text-indigo-900 ring-1 ring-indigo-200">
          Model: {NAMES[provider] ?? provider}
          {health.data && <ModeBadge mode={health.data.llm_mode} />}
        </span>
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="text-xs text-slate-500">Try:</span>
          {CHIPS.map((c) => (
            <button
              key={c.label}
              onClick={() => {
                setNurseId(c.nurseId);
                setSuggestion({ nurseId: c.nurseId, text: c.text });
                setPane("nurse");
              }}
              title={`${names[c.nurseId] ?? c.nurseId}: “${c.text}”`}
              className="rounded-full border border-indigo-200 bg-white px-2.5 py-1 text-xs text-indigo-800 hover:bg-indigo-50"
            >
              {c.label} · {names[c.nurseId] ?? c.nurseId}
            </button>
          ))}
        </div>
        <div className="ml-auto flex items-center gap-2 text-xs">
          <label htmlFor="phone-who" className="text-slate-500">
            Phone
          </label>
          <select
            id="phone-who"
            value={nurseId}
            onChange={(e) => setNurseId(e.target.value)}
            className="rounded border border-slate-300 bg-white px-1 py-1"
          >
            {(ward.data?.nurses ?? []).map((n) => (
              <option key={n.id} value={n.id}>
                {n.name} ({n.id})
              </option>
            ))}
          </select>
          <button
            onClick={() => reset.mutate()}
            disabled={reset.isPending}
            className="rounded-md border border-slate-300 bg-white px-3 py-1.5 font-semibold text-slate-700 hover:bg-slate-50 disabled:cursor-wait disabled:opacity-60"
          >
            {reset.isPending ? "Resetting…" : "Reset demo"}
          </button>
          {reset.data && !reset.isPending && (
            <span className="text-slate-500" data-testid="reset-done">
              reset in {reset.data.secs.toFixed(1)} s
            </span>
          )}
          {reset.error && <span className="text-red-600">{reset.error.message}</span>}
        </div>
      </header>

      <nav className="flex gap-1 border-b border-slate-300 bg-white p-1 lg:hidden">
        {(
          [
            ["charge", "Charge nurse"],
            ["nurse", `Nurse phone · ${names[nurseId] ?? nurseId}`],
          ] as const
        ).map(([k, label]) => (
          <button
            key={k}
            onClick={() => setPane(k)}
            className={`flex-1 rounded-md py-1.5 text-sm font-medium ${pane === k ? "bg-slate-800 text-white" : "bg-slate-100 text-slate-600"}`}
          >
            {label}
          </button>
        ))}
      </nav>

      <div className="grid min-h-0 flex-1 lg:grid-cols-[13fr_7fr]">
        <section
          className={`min-h-0 overflow-auto border-slate-300 bg-slate-50 lg:block lg:border-r ${pane === "charge" ? "" : "hidden"}`}
          aria-label="Charge nurse view"
        >
          <ChargeView embedded />
        </section>
        <section
          className={`min-h-0 items-center justify-center overflow-hidden lg:flex lg:p-4 ${pane === "nurse" ? "flex" : "hidden"}`}
          aria-label="Nurse phone"
        >
          {wide ? <PhoneFrame>{nurse}</PhoneFrame> : <div className="h-full w-full">{nurse}</div>}
        </section>
      </div>
    </div>
  );
}
