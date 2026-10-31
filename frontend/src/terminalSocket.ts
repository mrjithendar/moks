/** The browser end of a terminal. Reconnecting is safe because the shell lives on in its pod, so a
 *  dropped connection only needs a new socket. Close codes the gateway sends mean "do not retry". */
import { API } from "./api";

export type TerminalState = "connecting" | "connected" | "reconnecting" | "taken_over" | "ended" | "signed_out" | "forbidden" | "gone";

const FINAL: Record<number, TerminalState> = { 4401: "signed_out", 4403: "forbidden", 4404: "gone", 4409: "taken_over", 4410: "ended" };
const PING_MS = 25_000;
const MAX_TRIES = 8;

export function attachUrl(id: string, where: Location = window.location): string {
  return `${where.protocol === "https:" ? "wss" : "ws"}://${where.host}${API}/terminal/sessions/${id}/attach`;
}

export interface Events {
  data: (bytes: Uint8Array) => void;
  state: (state: TerminalState, detail?: string) => void;
  opened: () => void;
}

export class TerminalSocket {
  private ws: WebSocket | null = null;
  private tries = 0;
  private closed = false;
  private retry?: ReturnType<typeof setTimeout>;
  private ping?: ReturnType<typeof setInterval>;
  private encoder = new TextEncoder();

  constructor(private id: string, private on: Events, private make: (url: string) => WebSocket = (u) => new WebSocket(u)) {}

  connect() {
    if (this.closed) return;
    clearTimeout(this.retry);
    this.on.state(this.tries ? "reconnecting" : "connecting");
    const ws = this.make(attachUrl(this.id));
    ws.binaryType = "arraybuffer";
    this.ws = ws;
    ws.onopen = () => {
      this.tries = 0;
      this.on.state("connected");
      this.on.opened();
      this.ping = setInterval(() => this.control({ type: "ping" }), PING_MS);
    };
    ws.onmessage = (e) => { if (e.data instanceof ArrayBuffer) this.on.data(new Uint8Array(e.data)); };
    ws.onclose = (e) => {
      clearInterval(this.ping);
      if (this.closed || this.ws !== ws) return;
      const final = FINAL[e.code];
      if (final) { this.closed = true; this.on.state(final, e.reason); return; }
      if (++this.tries > MAX_TRIES) { this.on.state("gone", "The connection could not be restored."); return; }
      this.on.state("reconnecting", `Attempt ${this.tries} of ${MAX_TRIES}`);
      this.retry = setTimeout(() => this.connect(), Math.min(500 * 2 ** this.tries, 15_000));
    };
  }

  /** After taking over or running out of tries, the person asks for it again. */
  again() {
    this.closed = false;
    this.tries = 0;
    this.connect();
  }

  send(text: string) {
    if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(this.encoder.encode(text));
  }

  resize(cols: number, rows: number) {
    this.control({ type: "resize", cols, rows });
  }

  private control(event: Record<string, unknown>) {
    if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(event));
  }

  close() {
    this.closed = true;
    clearTimeout(this.retry);
    clearInterval(this.ping);
    this.ws?.close();
  }
}
