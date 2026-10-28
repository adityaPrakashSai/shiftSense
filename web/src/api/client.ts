// Typed fetch wrappers over the FastAPI routes. Types come from src/api/schema.ts, which
// openapi-typescript generates from the server's OpenAPI schema (`make api`).
import type { components } from "./schema";

type S = components["schemas"];
export type Ward = S["Ward"];
export type Nurse = S["Nurse"];
export type Health = S["Health"];
export type RosterOut = S["RosterOut"];
export type Cell = S["Cell"];
export type DayTotal = S["DayTotal"];
export type StageOut = S["StageOut"];
export type RunSummary = S["RunSummary"];
export type RunDetail = S["RunDetail"];
export type EventOut = S["EventOut"];
export type ResetOut = S["ResetOut"];
export type LLMUsage = S["LLMUsage"];

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    const err = body?.error ?? { code: `HTTP_${res.status}`, message: res.statusText };
    throw new ApiError(res.status, err.code, err.message);
  }
  return (await res.json()) as T;
}

function qs(params: Record<string, string | number | undefined | null>): string {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null) q.set(k, String(v));
  const s = q.toString();
  return s ? `?${s}` : "";
}

export const api = {
  health: () => request<Health>("/api/health"),
  ward: () => request<Ward>("/api/ward"),
  roster: (runId?: string | null) => request<RosterOut>(`/api/roster${qs({ run_id: runId })}`),
  run: (runId: string) => request<RunDetail>(`/api/runs/${runId}`),
  lineage: (runId: string) => request<RunSummary[]>(`/api/runs/${runId}/lineage`),
  generate: () =>
    request<S["RunAccepted"]>("/api/runs", { method: "POST", body: JSON.stringify({ kind: "full" }) }),
  publish: (runId: string) =>
    request<RunSummary>(`/api/runs/${runId}/publish`, {
      method: "POST",
      body: JSON.stringify({ by: "charge" }),
    }),
  events: (afterSeq: number, runId?: string) =>
    request<EventOut[]>(`/api/events${qs({ after_seq: afterSeq, run_id: runId })}`),
  lastEvents: (n: number) => request<EventOut[]>(`/api/events${qs({ last: n })}`),
  reset: () => request<ResetOut>("/api/demo/reset", { method: "POST" }),
  llmUsage: () => request<LLMUsage>("/api/llm/usage"),
};
