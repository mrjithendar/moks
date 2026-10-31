import { act, cleanup, fireEvent, render, renderHook, screen, waitFor } from "@testing-library/react";
import { useRef } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes, useNavigate } from "react-router-dom";
import { useFollow } from "../follow";
import { FeedbackProvider } from "../ui";

const state = vi.hoisted(() => ({ conversations: {} as Record<string, any>, post: null as any }));
vi.mock("../auth", () => ({ useAuth: () => ({ can: () => true, me: { email: "s@x" } }) }));
vi.mock("../api", async (orig) => ({ ...(await orig<any>()), api: vi.fn(async (path: string, opts?: any) => {
  if (opts?.method === "POST" && path.endsWith("/messages")) return state.post(opts);
  if (path === "/conversations" || path.startsWith("/conversations?")) return Object.values(state.conversations);
  if (path === "/environments") return [{ name: "mk4", is_production: true }];
  if (path.startsWith("/conversations/")) return state.conversations[path.split("/")[2]];
  if (path.startsWith("/investigations/")) return { id: path.split("/")[2], evidence: [{ ref: "E1", tool: "prom_query", summary: "cpu", ok: true, collected_at: "2026-10-30T00:00:00Z", duration_ms: 1, args: {}, scope: { environment: "mk4", namespace: "shop", from: "2026-10-30T00:00:00Z", to: "2026-10-30T00:00:00Z" }, source: { query: "up" }, data: null }],
    report: { summary: "s", observed_facts: [{ statement: "pod crashed", evidence: ["E1"] }], suspected_causes: [], missing_evidence: ["no traces"], next_steps: [], proposed_remediation: [] }, actions: [], tool_calls: 1, namespace: "shop", environment: "mk4" };
  return {};
}) }));

import { Composer, Workspace, draftKey, messageKind, progressLabel } from "./Conversations";

const conv = (id: string, title: string, extra: any = {}) => ({ id, title, environment: "mk4", namespace: "shop", owner: "s@x", created_at: "2026-10-30T00:00:00Z", updated_at: "2026-10-30T00:00:00Z",
  messages: [{ id: `${id}-m`, role: "assistant", author: "MOKS", content: `answer ${id}`, investigation_id: `${id}-i`, created_at: "2026-10-30T00:00:00Z", can_delete: true }],
  participants: [{ email: "s@x", owner: true }], statuses: { [`${id}-i`]: "completed" }, running: false, can_ask: true, can_manage: true, is_participant: true, ...extra });

let go: (p: string) => void;
function Nav() { const n = useNavigate(); go = n; return null; }
function app(path: string) {
  return render(<MemoryRouter initialEntries={[path]}><FeedbackProvider><Nav /><Routes><Route path="/investigations" element={<Workspace />} /><Route path="/conversations/:id" element={<Workspace />} /></Routes></FeedbackProvider></MemoryRouter>);
}

class FakeES { static all: FakeES[] = []; closed = false; handlers: Record<string, any[]> = {}; onopen?: any; onerror?: any; readyState = 1; constructor(public url: string) { FakeES.all.push(this); }
  addEventListener(t: string, f: any) { (this.handlers[t] ??= []).push(f); } close() { this.closed = true; } emit(t: string, d: any) { (this.handlers[t] ?? []).forEach((f) => f({ data: JSON.stringify(d) })); } }

beforeEach(() => { sessionStorage.clear(); FakeES.all = []; vi.stubGlobal("EventSource", FakeES); state.conversations = { a: conv("a", "First"), b: conv("b", "Second") }; });
afterEach(cleanup);

describe("message presentation", () => {
  it("distinguishes people, MOKS and notes", () => {
    expect(messageKind({ role: "assistant" })).toBe("moks");
    expect(messageKind({ role: "user", investigation_id: "i" })).toBe("engineer");
    expect(messageKind({ role: "user" })).toBe("note");
  });
  it("describes progress without tool internals", () => {
    const e = (o: any) => ({ seq: 1, at: "", ...o });
    expect(progressLabel([e({ type: "tool_call", tool: "k8s_list_pods" })])).toBe("Checking pods");
    expect(progressLabel([e({ type: "tool_call", tool: "prom_query" })])).toBe("Querying Prometheus");
    expect(progressLabel([])).toBe("Starting");
  });
});

describe("workspace", () => {
  it("lists and filters conversations", async () => {
    app("/investigations");
    await screen.findByText("First");
    fireEvent.change(screen.getByLabelText("Search conversations"), { target: { value: "sec" } });
    expect(screen.queryByText("First")).toBeNull();
    expect(screen.getByText("Second")).toBeTruthy();
  });

  it("switching conversations shows no stale content or events", async () => {
    state.conversations.a = conv("a", "First", { running: true, statuses: { "a-i": "running" }, messages: [{ id: "q", role: "user", author: "s@x", content: "why", investigation_id: "a-i", created_at: "2026-10-30T00:00:00Z" }] });
    app("/conversations/a");
    await screen.findByText("why");
    await waitFor(() => expect(FakeES.all.length).toBe(1));
    act(() => go("/conversations/b"));
    await screen.findByText("answer b");
    expect(screen.queryByText("why")).toBeNull();
    expect(FakeES.all[0].closed).toBe(true);
    act(() => FakeES.all[0].emit("phase", { seq: 1, type: "phase", at: "", label: "Late event" }));
    expect(screen.queryByText(/Late event/)).toBeNull();
  });

  it("opens evidence from a citation and keeps uncertainty visible", async () => {
    app("/conversations/a");
    await screen.findByText("no traces");
    fireEvent.click(screen.getAllByRole("button", { name: "E1" })[0]);
    expect(await screen.findByLabelText("Evidence")).toBeTruthy();
    expect(await screen.findByRole("button", { name: "Copy query" })).toBeTruthy();
  });

  it("makes evidence a modal sheet on a phone, closed by Escape", async () => {
    const mm = vi.fn((q: string) => ({ matches: true, media: q, addEventListener: vi.fn(), removeEventListener: vi.fn() }));
    vi.stubGlobal("matchMedia", mm);
    window.matchMedia = mm as any;
    app("/conversations/a");
    await screen.findByText("no traces");
    fireEvent.click(screen.getByRole("button", { name: "Evidence" }));
    const sheet = await screen.findByRole("dialog", { name: "Evidence" });
    expect(sheet.getAttribute("aria-modal")).toBe("true");
    fireEvent.keyDown(document, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    delete (window as any).matchMedia;
  });
});

describe("composer", () => {
  const box = () => screen.getByLabelText("Message") as HTMLTextAreaElement;

  it("Enter sends, Shift+Enter does not, and composing text never sends", async () => {
    const send = vi.fn(async () => {});
    render(<Composer id="c" canAsk running={false} onSend={send} />);
    fireEvent.change(box(), { target: { value: "hello" } });
    fireEvent.keyDown(box(), { key: "Enter", shiftKey: true });
    fireEvent.keyDown(box(), { key: "Enter", isComposing: true });
    expect(send).not.toHaveBeenCalled();
    fireEvent.keyDown(box(), { key: "Enter" });
    await waitFor(() => expect(send).toHaveBeenCalledWith("hello", true));
    await waitFor(() => expect(box().value).toBe(""));
  });

  it("keeps the text after a failed request and shows the error", async () => {
    render(<Composer id="c" canAsk running={false} onSend={async () => { throw new Error("worker down"); }} />);
    fireEvent.change(box(), { target: { value: "keep me" } });
    fireEvent.keyDown(box(), { key: "Enter" });
    expect(await screen.findByText("worker down")).toBeTruthy();
    expect(box().value).toBe("keep me");
    expect(sessionStorage.getItem(draftKey("c"))).toBe("keep me");
  });

  it("prevents a double submission", async () => {
    let finish!: () => void;
    const send = vi.fn(() => new Promise<void>((r) => { finish = r; }));
    render(<Composer id="c" canAsk running={false} onSend={send} />);
    fireEvent.change(box(), { target: { value: "once" } });
    fireEvent.keyDown(box(), { key: "Enter" });
    fireEvent.keyDown(box(), { key: "Enter" });
    expect(send).toHaveBeenCalledTimes(1);
    await act(async () => finish());
  });

  it("keeps a separate draft per conversation", () => {
    sessionStorage.setItem(draftKey("x"), "draft x");
    const { unmount } = render(<Composer id="x" canAsk running={false} onSend={async () => {}} />);
    expect(box().value).toBe("draft x");
    unmount();
    render(<Composer id="y" canAsk running={false} onSend={async () => {}} />);
    expect(box().value).toBe("");
  });
});

describe("scroll following", () => {
  function setup() {
    const el = document.createElement("div");
    let top = 0;
    Object.defineProperty(el, "scrollHeight", { configurable: true, value: 2000 });
    Object.defineProperty(el, "clientHeight", { configurable: true, value: 500 });
    Object.defineProperty(el, "scrollTop", { configurable: true, get: () => top, set: (v) => { top = v; } });
    const { result } = renderHook(() => { const s = useRef(el); const c = useRef<HTMLElement>(null); return useFollow(s, c); });
    return { el, result, top: () => top, scrollTo: (v: number) => { top = v; act(() => { el.dispatchEvent(new Event("scroll")); }); } };
  }

  it("follows while the reader is at the bottom", () => {
    const t = setup();
    t.scrollTo(1500);
    act(() => t.result.current.notify());
    expect(t.top()).toBe(2000);
    expect(t.result.current.fresh).toBe(false);
  });

  it("never moves someone reading earlier messages, and offers New activity", () => {
    const t = setup();
    t.scrollTo(300);
    act(() => t.result.current.notify());
    expect(t.top()).toBe(300);
    expect(t.result.current.fresh).toBe(true);
    act(() => t.result.current.jump());
    expect(t.top()).toBe(2000);
    expect(t.result.current.fresh).toBe(false);
  });
});

describe("inbox", () => {
  const row = (id: string, title: string, extra: any = {}) => conv(id, title, { preview: `preview of ${title}`, status: "completed", unread: false, ...extra });

  it("shows subject, preview and unread state, and only says unread when the API does", async () => {
    state.conversations = { a: row("a", "Checkout down", { unread: true }), b: row("b", "Second") };
    app("/investigations");
    await screen.findByText("Checkout down");
    expect(screen.getByText("preview of Checkout down")).toBeTruthy();
    expect(screen.getByText("Unread:")).toBeTruthy();
    expect(screen.getAllByText("Unread:")).toHaveLength(1);
  });

  it("keeps filters and search when opening a conversation and going back", async () => {
    state.conversations = { a: row("a", "Checkout down"), b: row("b", "Payments slow", { environment: "eks" }) };
    app("/investigations?environment=mk4");
    await screen.findByText("Checkout down");
    expect(screen.queryByText("Payments slow")).toBeNull();
    fireEvent.change(screen.getByRole("searchbox", { name: "Search conversations" }), { target: { value: "check" } });
    fireEvent.click(screen.getByRole("link", { name: /Checkout down/ }));
    await screen.findByText("answer a");
    expect((screen.getByRole("link", { name: "Back to inbox" }) as HTMLAnchorElement).getAttribute("href")).toBe("/investigations?environment=mk4");
    fireEvent.click(screen.getByRole("link", { name: "Back to inbox" }));
    await screen.findByText("Choose a conversation");
    expect((screen.getByRole("searchbox", { name: "Search conversations" }) as HTMLInputElement).value).toBe("check");
    expect(screen.queryByText("Payments slow")).toBeNull();
  });

  it("marks a conversation read once it is open", async () => {
    const { api } = await import("../api");
    app("/conversations/a");
    await screen.findByText("answer a");
    await waitFor(() => expect((api as any).mock.calls.some(([p]: [string]) => p === "/conversations/a/read")).toBe(true));
  });

  it("offers row actions in a named menu", async () => {
    state.conversations = { a: row("a", "Checkout down") };
    app("/investigations");
    await screen.findByText("Checkout down");
    fireEvent.click(screen.getByRole("button", { name: "Actions for Checkout down" }));
    expect(screen.getByRole("menuitem", { name: "Mark as unread" })).toBeTruthy();
    expect(screen.getByRole("menuitem", { name: "Delete" })).toBeTruthy();
  });
});

describe("drafts", () => {
  it("clearLocalWork removes drafts and leaves other tabs' keys alone", async () => {
    const { clearLocalWork } = await import("../api");
    sessionStorage.setItem(draftKey("a"), "secret plan");
    sessionStorage.setItem("moks.logql", '{namespace="shop"}');
    sessionStorage.setItem("other.app", "keep");
    clearLocalWork();
    expect(sessionStorage.getItem(draftKey("a"))).toBeNull();
    expect(sessionStorage.getItem("moks.logql")).toBeNull();
    expect(sessionStorage.getItem("other.app")).toBe("keep");
  });
});

describe("composer controls", () => {
  it("offers Stop while MOKS works and keeps the text when sending fails", async () => {
    const stop = vi.fn();
    const send = vi.fn(async () => { throw new Error("boom"); });
    render(<Composer id="z" canAsk running stopping={false} onSend={send} onStop={stop} />);
    fireEvent.click(screen.getByRole("button", { name: "Stop" }));
    expect(stop).toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText("Message"), { target: { value: "try this" } });
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Send" })); });
    expect((screen.getByLabelText("Message") as HTMLTextAreaElement).value).toBe("try this");
    expect(screen.getByRole("alert").textContent).toContain("boom");
  });
});
