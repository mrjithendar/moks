import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { attachUrl, TerminalSocket } from "../terminalSocket";
import { FeedbackProvider } from "../ui";
import { Terminals, TerminalView } from "./Terminal";

const terminal = vi.hoisted(() => ({ pasted: [] as string[], instance: null as any }));

vi.mock("@xterm/xterm", () => ({ Terminal: class {
  cols = 80; rows = 24;
  constructor() { terminal.instance = this; }
  loadAddon() {} open() {} focus() {} write() {} dispose() {} attachCustomKeyEventHandler() {}
  onData() { return { dispose() {} }; } onBinary() { return { dispose() {} }; } onResize() { return { dispose() {} }; }
  getSelection() { return ""; } paste(text: string) { terminal.pasted.push(text); }
} }));
vi.mock("@xterm/addon-fit", () => ({ FitAddon: class { fit() {} } }));
vi.mock("@xterm/xterm/css/xterm.css", () => ({}));

const options = { idle_minutes: 15, max_minutes: 60, min_minutes: 15, kube_namespaces: ["shop"], recording: { text: "MOKS does not record what is typed or shown in a terminal." },
  profiles: [{ name: "ro", label: "read-only", environment: "mk4", account: "111122223333", role_arn: "arn:aws:iam::111122223333:role/moks-terminal-readonly", regions: ["us-east-1"], clusters: ["in-cluster"] }] };
const ready = { id: "s1", mine: true, owner: "me@example.com", status: "ready", purpose: "Checking checkout", environment: "mk4", account: "111122223333", region: "us-east-1", cluster: "in-cluster", namespaces: ["shop"],
  aws_identity: "arn:aws:sts::111122223333:assumed-role/moks-terminal-readonly/moks-term-s1", kube_identity: "system:serviceaccount:moks-terminals:moks-terminal-session",
  expires_at: new Date(Date.now() + 3 * 60_000).toISOString(), credential_expires_at: new Date(Date.now() + 3 * 60_000).toISOString() };
const responses: Record<string, unknown> = {};
const calls = vi.hoisted(() => [] as { path: string; body: unknown }[]);

vi.mock("../api", async (real) => ({ ...(await real<object>()), api: vi.fn(async (path: string, opts?: { body?: unknown }) => {
  calls.push({ path, body: opts?.body });
  const r = responses[path];
  if (r instanceof Error) throw r;
  return r;
}) }));
vi.mock("../auth", () => ({ useAuth: () => ({ can: (p: string) => p !== "terminal.revoke" }) }));

class FakeSocket {
  static OPEN = 1;
  static all: FakeSocket[] = [];
  readyState = 0; binaryType = ""; sent: unknown[] = [];
  onopen?: () => void; onclose?: (e: { code: number; reason: string }) => void; onmessage?: (e: { data: unknown }) => void;
  constructor(public url: string) { FakeSocket.all.push(this); }
  send(data: unknown) { this.sent.push(data); }
  close() {}
  open() { this.readyState = 1; this.onopen?.(); }
  drop(code: number, reason = "") { this.readyState = 3; this.onclose?.({ code, reason }); }
}

beforeEach(() => {
  FakeSocket.all = [];
  calls.length = 0;
  terminal.pasted = [];
  vi.stubGlobal("WebSocket", FakeSocket);
  vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
  Object.assign(responses, { "/terminal/options": options, "/terminal/sessions": [], "/terminal/sessions/s1": ready });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.useRealTimers(); });

function list() { render(<MemoryRouter><FeedbackProvider><Terminals /></FeedbackProvider></MemoryRouter>); }
function view() { render(<MemoryRouter initialEntries={["/terminal/s1"]}><FeedbackProvider><Routes><Route path="/terminal/:id" element={<TerminalView />} /></Routes></FeedbackProvider></MemoryRouter>); }

describe("opening a terminal", () => {
  it("makes every part of the scope a deliberate choice", async () => {
    list();
    const open = (await screen.findByRole("button", { name: "Open terminal" })) as HTMLButtonElement;
    expect(open.disabled).toBe(true);
    expect((screen.getByLabelText("Environment") as HTMLSelectElement).value).toBe("");
    fireEvent.change(screen.getByLabelText("Environment"), { target: { value: "mk4" } });
    fireEvent.change(screen.getByLabelText("AWS account and role"), { target: { value: "ro" } });
    fireEvent.change(screen.getByLabelText("Region"), { target: { value: "us-east-1" } });
    expect(open.disabled).toBe(true);
    fireEvent.change(screen.getByLabelText("Purpose"), { target: { value: "Checking the rollout" } });
    expect(open.disabled).toBe(false);
    expect(screen.getByText(/does not record what is typed/)).toBeTruthy();
  });
  it("says so when terminals are not enabled", async () => {
    const { ApiError } = await import("../api");
    responses["/terminal/options"] = new ApiError(404, "Not found");
    list();
    expect(await screen.findByText("Terminals are not enabled here")).toBeTruthy();
  });
});

describe("a running session", () => {
  it("keeps the verified identity and scope on screen and warns before credentials expire", async () => {
    view();
    expect(await screen.findByText(/assumed-role\/moks-terminal-readonly/)).toBeTruthy();
    expect(screen.getByText("111122223333")).toBeTruthy();
    expect(screen.getByText("expire in 3 min")).toBeTruthy();
    expect(FakeSocket.all[0].url).toContain("/moks/api/terminal/sessions/s1/attach");
  });
  it("sends its size once connected", async () => {
    view();
    await screen.findByText("Copy selection");
    FakeSocket.all[0].open();
    expect(FakeSocket.all[0].sent).toContain(JSON.stringify({ type: "resize", cols: 80, rows: 24 }));
  });
  it("places a suggestion without running it", async () => {
    responses["/assist/terminal/s1"] = { suggestions: [{ command: "aws s3 ls", explanation: "Lists buckets." }], note: "" };
    view();
    await screen.findByText("Copy selection");
    FakeSocket.all[0].open();
    fireEvent.change(screen.getByLabelText("What do you want to find out?"), { target: { value: "list buckets" } });
    fireEvent.click(screen.getByRole("button", { name: "Suggest" }));
    fireEvent.click(await screen.findByRole("button", { name: "Put at my prompt" }));
    expect(terminal.pasted).toEqual(["aws s3 ls"]);
    await screen.findByText(/press Enter to run it/);
    expect(FakeSocket.all[0].sent.filter((m) => m instanceof Uint8Array)).toEqual([]);
  });
  it("shows why a session that ended is over, and opens no socket", async () => {
    responses["/terminal/sessions/s1"] = { ...ready, status: "ended", end_text: "The session ended after a period with no input." };
    view();
    expect(await screen.findByText(/no input/)).toBeTruthy();
    expect(FakeSocket.all).toEqual([]);
  });
  it("never attaches to someone else's session", async () => {
    responses["/terminal/sessions/s1"] = { ...ready, mine: false };
    view();
    expect(await screen.findByText(/Nobody else can attach/)).toBeTruthy();
    expect(FakeSocket.all).toEqual([]);
  });
  it("explains a failed start", async () => {
    responses["/terminal/sessions/s1"] = { ...ready, status: "failed", aws_identity: null, error: "could not assume role: AccessDenied" };
    view();
    expect(await screen.findByText(/AccessDenied/)).toBeTruthy();
  });
});

describe("the connection", () => {
  function connect() {
    const states: string[] = [];
    const made: FakeSocket[] = [];
    const s = new TerminalSocket("s1", { data: () => {}, opened: () => {}, state: (st) => states.push(st) }, (u) => { const f = new FakeSocket(u); made.push(f); return f as unknown as WebSocket; });
    s.connect();
    return { s, states, made };
  }
  it("reconnects after a drop", () => {
    vi.useFakeTimers();
    const { states, made } = connect();
    made[0].open();
    made[0].drop(1006);
    expect(states.at(-1)).toBe("reconnecting");
    vi.advanceTimersByTime(20_000);
    expect(made.length).toBe(2);
    made[1].open();
    expect(states.at(-1)).toBe("connected");
  });
  it.each([[4401, "signed_out"], [4403, "forbidden"], [4404, "gone"], [4409, "taken_over"], [4410, "ended"]])("does not retry after close code %i", (code, state) => {
    vi.useFakeTimers();
    const { states, made } = connect();
    made[0].open();
    made[0].drop(code);
    vi.advanceTimersByTime(60_000);
    expect(made.length).toBe(1);
    expect(states.at(-1)).toBe(state);
  });
  it("gives up after a while, and says so", () => {
    vi.useFakeTimers();
    const { states, made } = connect();
    for (let i = 0; i < 12; i++) { made.at(-1)!.drop(1006); vi.advanceTimersByTime(20_000); }
    expect(states.at(-1)).toBe("gone");
    expect(made.length).toBe(9);
  });
  it("uses wss on an https page, under the base path", () => {
    expect(attachUrl("s1", { protocol: "https:", host: "jithendar.com" } as Location)).toBe("wss://jithendar.com/moks/api/terminal/sessions/s1/attach");
  });
});
