import { useEffect, useState } from "react";

type Health = { ok: boolean; llm_mode: string; provider: string; model: string | null; db: string };

export default function App() {
  const [health, setHealth] = useState<Health | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetch("/api/health")
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then(setHealth)
      .catch((e: Error) => setError(e.message));
  }, []);

  return (
    <main className="min-h-screen bg-slate-50 px-4 py-12 text-slate-800">
      <div className="mx-auto max-w-xl rounded-xl border border-slate-200 bg-white p-6 shadow-sm">
        <h1 className="text-2xl font-semibold">ShiftSense</h1>
        <p className="mt-2 text-slate-600">
          Nurse rostering with Meta Muse Spark and CP-SAT. Scaffold page (M0).
        </p>
        <div className="mt-6 text-sm">
          <h2 className="font-medium">API health</h2>
          {error && <p className="mt-1 text-red-600">Unreachable: {error}</p>}
          {!error && !health && <p className="mt-1 text-slate-500">Checking…</p>}
          {health && (
            <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1">
              <dt className="text-slate-500">ok</dt>
              <dd className={health.ok ? "text-green-700" : "text-red-600"}>{String(health.ok)}</dd>
              <dt className="text-slate-500">LLM mode</dt>
              <dd>
                {health.llm_mode}
                {health.llm_mode === "mock" && (
                  <span className="ml-2 rounded bg-yellow-200 px-1.5 py-0.5 text-xs font-semibold">
                    MOCK MODE
                  </span>
                )}
              </dd>
              <dt className="text-slate-500">Provider</dt>
              <dd>{health.provider}</dd>
              <dt className="text-slate-500">Model</dt>
              <dd className="font-mono text-xs">{health.model ?? "—"}</dd>
              <dt className="text-slate-500">Database</dt>
              <dd>{health.db}</dd>
            </dl>
          )}
        </div>
      </div>
    </main>
  );
}
