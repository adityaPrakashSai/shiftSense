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
export type RequestOut = S["RequestOut"];
export type MessageReply = S["MessageReply"];
export type MessageRecord = S["MessageRecord"];
export type RequestAction = S["RequestAction"];

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
  // multipart bodies (FormData) set their own Content-Type with the boundary
  const json = !(init?.body instanceof FormData);
  const res = await fetch(path, {
    ...init,
    headers: { ...(json ? { "Content-Type": "application/json" } : {}), ...(init?.headers ?? {}) },
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
  sendMessage: (senderId: string, text: string) => {
    const form = new FormData();
    form.set("sender_id", senderId);
    form.set("text", text);
    return request<MessageReply>("/api/messages", { method: "POST", body: form });
  },
  messages: (nurseId: string) => request<MessageRecord[]>(`/api/messages${qs({ nurse_id: nurseId })}`),
  requests: (params: { nurse_id?: string; status?: string }) =>
    request<RequestOut[]>(`/api/requests${qs(params)}`),
  confirm: (id: string) => request<RequestAction>(`/api/requests/${id}/confirm`, { method: "POST" }),
  reject: (id: string) => request<RequestAction>(`/api/requests/${id}/reject`, { method: "POST" }),
  approve: (id: string) => request<RequestAction>(`/api/requests/${id}/approve`, { method: "POST" }),
};
