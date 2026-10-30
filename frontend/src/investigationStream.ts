import { useEffect, useRef, useState } from "react";
import { API } from "./api";

export interface Ev { type: string; seq: number; at: string; [k: string]: any }
export type StreamState = "connecting" | "live" | "reconnecting" | "ended" | "failed";

const TYPES = ["queued", "scope", "phase", "skills", "tool_call", "tool_result", "llm_usage", "note", "report", "error", "cancelled", "done"];
const REFRESH = new Set(["tool_result", "report", "done"]);

export function parseEvent(data: string): Ev | null {
  try {
    const ev = JSON.parse(data);
    return ev && typeof ev === "object" && typeof ev.seq === "number" && typeof ev.type === "string" ? ev : null;
  } catch {
    return null;
  }
}

/** Events of one investigation. Changing id drops the previous timeline, and a closed
 *  subscription's late callbacks are ignored so they cannot land in the next investigation. */
export function useInvestigationEvents(id: string | undefined, onRefresh: () => void) {
  const [state, setState] = useState<{ id?: string; events: Ev[]; stream: StreamState }>({ events: [], stream: "connecting" });
  const refresh = useRef(onRefresh);
  refresh.current = onRefresh;

  useEffect(() => {
    if (!id) return;
    let active = true;
    const seen = new Set<number>();
    setState({ id, events: [], stream: "connecting" });
    const es = new EventSource(`${API}/investigations/${id}/events`);
    const setStream = (stream: StreamState) => { if (active) setState((s) => ({ ...s, stream })); };
    const onMsg = (m: MessageEvent) => {
      if (!active) return;
      const ev = parseEvent(m.data);
      if (!ev || seen.has(ev.seq)) return; // the server replays from the start on reconnect
      seen.add(ev.seq);
      setState((s) => ({ ...s, stream: "live", events: [...s.events, ev].sort((a, b) => a.seq - b.seq) }));
      if (REFRESH.has(ev.type)) refresh.current();
    };
    for (const t of TYPES) es.addEventListener(t, onMsg);
    const finish = () => { es.close(); setStream("ended"); };
    es.addEventListener("end", finish);
    es.addEventListener("timeout", finish);
    es.addEventListener("revoked", () => { es.close(); setStream("failed"); }); // access changed: the server stopped sending
    es.onopen = () => setStream("live");
    es.onerror = () => setStream(es.readyState === EventSource.CLOSED ? "failed" : "reconnecting");
    return () => { active = false; es.close(); };
  }, [id]);

  return state.id === id ? state : { id, events: [] as Ev[], stream: "connecting" as StreamState };
}
