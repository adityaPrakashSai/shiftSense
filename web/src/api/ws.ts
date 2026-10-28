// useWardSocket (design doc 10.2): one WebSocket per ward that only carries "something changed"
// nudges. Reconnects with backoff (1, 2, 4, max 8 s) and invalidates every query on (re)connect.
// If it cannot connect for 10 s it polls /api/events every 3 s and turns new events into the same
// nudges, so a view never depends on the socket being up.
import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { api, type EventOut } from "./client";

export type WardMessage =
  | { type: "run_started"; run_id: string }
  | { type: "run_finished"; run_id: string; status: string }
  | { type: "roster_published"; run_id: string | null }
  | { type: "event"; seq: number };

export type SocketMode = "connecting" | "live" | "polling";

const BACKOFF = [1000, 2000, 4000, 8000];
const POLL_AFTER_MS = 10_000;
const POLL_EVERY_MS = 3000;

/** The nudge a decision-log event stands for, when the socket is not there to send it. */
function nudgeFor(e: EventOut): WardMessage | null {
  switch (e.kind) {
    case "run.started":
      return e.run_id ? { type: "run_started", run_id: e.run_id } : null;
    case "run.proposed":
      return e.run_id ? { type: "run_finished", run_id: e.run_id, status: "proposed" } : null;
    case "run.infeasible":
    case "run.failed":
      return e.run_id ? { type: "run_finished", run_id: e.run_id, status: "infeasible" } : null;
    case "run.published":
    case "demo.reset":
      return { type: "roster_published", run_id: e.run_id ?? null };
    default:
      return null;
  }
}

export function useWardSocket(
  wardId: string | undefined,
  onMessage: (m: WardMessage) => void,
  onEvents: (events: EventOut[]) => void,
): SocketMode {
  const qc = useQueryClient();
  const [mode, setMode] = useState<SocketMode>("connecting");
  const handlers = useRef({ onMessage, onEvents });
  handlers.current = { onMessage, onEvents };

  useEffect(() => {
    if (!wardId) return;
    let closed = false;
    let ws: WebSocket | null = null;
    let attempt = 0;
    let retry: number | undefined;
    let poll: number | undefined;
    let pollTimer: number | undefined;
    let ping: number | undefined;
    let lastSeq = 0;
    let fetching = false;
    let dirty = false;

    // fetch events newer than lastSeq (coalesced: one request in flight at a time)
    const pull = async (polling: boolean) => {
      if (fetching) {
        dirty = true;
        return;
      }
      fetching = true;
      try {
        do {
          dirty = false;
          const events = await api.events(lastSeq);
          if (events.length === 0) continue;
          lastSeq = events[events.length - 1].seq;
          handlers.current.onEvents(events);
          if (polling) {
            for (const e of events) {
              const m = nudgeFor(e);
              if (m) handlers.current.onMessage(m);
            }
          }
        } while (dirty);
      } catch {
        /* the next nudge or poll retries */
      } finally {
        fetching = false;
      }
    };

    const startPolling = () => {
      if (poll !== undefined || closed) return;
      setMode("polling");
      poll = window.setInterval(() => void pull(true), POLL_EVERY_MS);
    };
    const stopPolling = () => {
      if (poll !== undefined) window.clearInterval(poll);
      poll = undefined;
    };

    const connect = () => {
      if (closed) return;
      const proto = location.protocol === "https:" ? "wss" : "ws";
      ws = new WebSocket(`${proto}://${location.host}/ws/ward/${wardId}`);
      ws.onopen = () => {
        attempt = 0;
        if (pollTimer !== undefined) window.clearTimeout(pollTimer);
        pollTimer = undefined;
        stopPolling();
        setMode("live");
        void qc.invalidateQueries();
        // nudges sent while the socket was down are lost: replay them from the event log
        void pull(true);
        ping = window.setInterval(() => ws?.readyState === 1 && ws.send("ping"), 20_000);
      };
      ws.onmessage = (ev) => {
        const m = JSON.parse(ev.data) as WardMessage;
        if (m.type === "event") void pull(false);
        else handlers.current.onMessage(m);
      };
      ws.onclose = () => {
        if (ping !== undefined) window.clearInterval(ping);
        if (closed) return;
        if (attempt === 0 && poll === undefined) {
          setMode("connecting");
          pollTimer = window.setTimeout(startPolling, POLL_AFTER_MS);
        }
        const wait = BACKOFF[Math.min(attempt, BACKOFF.length - 1)];
        attempt += 1;
        retry = window.setTimeout(connect, wait);
      };
    };

    // start the event cursor at the newest event, so old runs are not replayed as nudges
    api
      .lastEvents(1)
      .then((evs) => {
        lastSeq = evs.length ? evs[evs.length - 1].seq : 0;
      })
      .catch(() => undefined)
      .finally(connect);

    return () => {
      closed = true;
      stopPolling();
      if (pollTimer !== undefined) window.clearTimeout(pollTimer);
      if (retry !== undefined) window.clearTimeout(retry);
      if (ping !== undefined) window.clearInterval(ping);
      ws?.close();
    };
  }, [wardId, qc]);

  return mode;
}
