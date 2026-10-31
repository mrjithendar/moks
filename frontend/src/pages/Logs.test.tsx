import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const state = vi.hoisted(() => ({ generate: null as any, query: null as any, calls: [] as { path: string; body: any }[], restricted: false, llm: true, logs: null as any }));
vi.mock("../api", async (orig) => ({ ...(await orig<any>()), api: vi.fn(async (path: string, opts?: any) => {
  state.calls.push({ path, body: opts?.body });
  if (path === "/logs/sources") return { generation_available: state.llm, environments: [{ environment: "mk4", display_name: "Minikube", restricted: state.restricted, max_minutes: 360, data_sources: [{ id: "c1", name: "loki", last_test_ok: true }] }] };
  if (path === "/logs/generate") return state.generate(opts);
  if (path === "/logs/query") return state.query(opts);
  if (path === "/multi/logs") return state.logs ? state.logs(opts) : { items: [], errors: [], environments: ["mk4"], queries: {}, collected_at: null };
  if (path === "/multi/scopes") return { scopes: [{ environment: "mk4", display_name: "Minikube", namespaces: ["shop", "web"] }] };
  return {};
}) }));

import { ApiError } from "../api";
import { Logs } from "./Logs";

const draft = (extra: any = {}) => ({ query: '{namespace="shop"} |= "timeout"', explanation: "Timeouts in shop.", query_type: "log", required_labels: ["namespace"], assumptions: ["pods log the word timeout"], suggested_minutes: 30,
  warnings: [], syntax_checked: true, model: "openai/m", scope: { environment: "mk4", connector: "loki", namespace: "shop" }, ...extra });
const result = (extra: any = {}) => ({ summary: "", kind: "log", environment: "mk4", connector: "loki", limit: 200, limit_reached: false, truncated: false, collected_at: "2026-10-31T00:00:00Z", duration_ms: 5, source: {},
  data: { start: "2026-10-31T00:00:00Z", end: "2026-10-31T01:00:00Z", lines: [{ ts: "2026-10-31T00:00:00Z", line: "payment timeout", labels: { namespace: "shop" } }] }, ...extra });

const open = async (url = "/logs?view=logql&namespace=shop") => {
  render(<MemoryRouter initialEntries={[url]}><Logs /></MemoryRouter>);
  await screen.findByRole("option", { name: "loki" });
};
const ask = (text = "find checkout timeouts") => fireEvent.change(screen.getByLabelText("Describe what you want"), { target: { value: text } });
const editor = () => screen.getByLabelText("LogQL") as HTMLTextAreaElement;
const sent = (path: string) => state.calls.filter((c) => c.path === path);

beforeEach(() => { sessionStorage.clear(); state.calls = []; state.restricted = false; state.llm = true; state.logs = null; state.generate = async () => draft(); state.query = async () => result(); });
afterEach(cleanup);

describe("Generate LogQL with AI", () => {
  it("fills the editor and runs nothing", async () => {
    await open();
    ask();
    fireEvent.click(screen.getByRole("button", { name: "Generate" }));
    await waitFor(() => expect(editor().value).toContain("timeout"));
    expect(sent("/logs/query")).toHaveLength(0);
    expect(screen.getByRole("status").textContent).toContain("Nothing has run");
    expect(screen.getByText("Timeouts in shop.")).toBeTruthy();
    expect(sent("/logs/generate")[0].body).toMatchObject({ environment: "mk4", connector: "c1", namespace: "shop", prompt: "find checkout timeouts" });
    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    await screen.findByText("Log result");
    expect(sent("/logs/query")[0].body).toMatchObject({ logql: draft().query, minutes: 30, connector: "c1" });
  });

  it("keeps what the engineer had written and can bring it back", async () => {
    await open();
    fireEvent.change(editor(), { target: { value: '{namespace="shop"} |= "mine"' } });
    ask();
    fireEvent.click(screen.getByRole("button", { name: "Generate" }));
    await waitFor(() => expect(editor().value).toContain("timeout"));
    fireEvent.click(screen.getByRole("button", { name: "Restore previous query" }));
    expect(editor().value).toContain("mine");
  });

  it("does not put the query or the request in the URL", async () => {
    await open();
    ask("find request abc-123");
    fireEvent.click(screen.getByRole("button", { name: "Generate" }));
    await waitFor(() => expect(editor().value).toContain("timeout"));
    expect(state.calls.every((c) => !c.path.includes("abc-123") && !c.path.includes("timeout"))).toBe(true);
  });

  it("sends one request while one is running, and can cancel it", async () => {
    state.generate = (opts: any) => new Promise((_, reject) => opts.signal.addEventListener("abort", () => reject(Object.assign(new Error("x"), { name: "AbortError" }))));
    await open();
    ask();
    const form = screen.getByRole("button", { name: "Generate" }).closest("form") as HTMLFormElement;
    fireEvent.submit(form);
    fireEvent.submit(form);
    await screen.findByRole("button", { name: "Generating…" });
    expect(sent("/logs/generate")).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    await screen.findByRole("button", { name: "Generate" });
    expect(screen.queryByRole("alert")).toBeNull();
    expect(editor().value).toBe("");
  });

  it("says plainly when the model's answer was unusable, and keeps the editor", async () => {
    state.generate = async () => { throw new ApiError(502, "the model did not return a usable answer; try again or write the query yourself"); };
    await open();
    fireEvent.change(editor(), { target: { value: '{namespace="shop"}' } });
    ask();
    fireEvent.click(screen.getByRole("button", { name: "Generate" }));
    expect((await screen.findByRole("alert")).textContent).toContain("usable answer");
    expect(editor().value).toBe('{namespace="shop"}');
  });

  it("shows why a draft was discarded instead of offering it", async () => {
    state.generate = async () => draft({ query: "", warnings: ["The model's draft was discarded: it did not stay inside namespace shop."] });
    await open();
    ask();
    fireEvent.click(screen.getByRole("button", { name: "Generate" }));
    expect(await screen.findByText(/did not stay inside/)).toBeTruthy();
    expect(editor().value).toBe("");
  });

  it("shows a refusal from the backend as a permission problem", async () => {
    state.generate = async () => { throw new ApiError(403, "namespace 'secure' is outside your scope"); };
    await open();
    ask();
    fireEvent.click(screen.getByRole("button", { name: "Generate" }));
    expect((await screen.findByRole("alert")).textContent).toContain("You don't have access");
  });

  it("asks a scoped engineer for a namespace before generating", async () => {
    state.restricted = true;
    await open("/logs?view=logql");
    ask();
    expect((screen.getByRole("button", { name: "Generate" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText("Choose a namespace first.")).toBeTruthy();
  });

  it("explains when no model is configured", async () => {
    state.llm = false;
    await open();
    ask();
    expect((screen.getByRole("button", { name: "Generate" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText(/No active chat model/)).toBeTruthy();
  });
});

describe("running LogQL", () => {
  it("reports a refusal at run time, whoever wrote the query", async () => {
    state.query = async () => { throw new ApiError(403, "namespace 'secure' is outside your scope"); };
    await open();
    fireEvent.change(editor(), { target: { value: '{namespace="secure"}' } });
    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    expect((await screen.findByRole("alert")).textContent).toContain("outside your scope");
  });

  it("labels metric results differently from log results, with their limits", async () => {
    state.query = async () => result({ kind: "metric", data: { start: "2026-10-31T00:00:00Z", end: "2026-10-31T01:00:00Z", total_series: 40, series: [{ metric: { app: "a" }, values: [[1, 2], [2, 3]] }] } });
    await open();
    fireEvent.change(editor(), { target: { value: 'rate({namespace="shop"}[5m])' } });
    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    expect(await screen.findByText("Metric result")).toBeTruthy();
    expect(screen.getByRole("region", { name: "Metric series" })).toBeTruthy();
    expect(screen.getByText("Showing 1 of 40 series.")).toBeTruthy();
  });

  it("shows source, scope, truncation and an empty result honestly", async () => {
    state.query = async () => result({ limit_reached: true, limit: 200 });
    await open();
    fireEvent.change(editor(), { target: { value: '{namespace="shop"}' } });
    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    expect(await screen.findByText(/Stopped at the limit of 200/)).toBeTruthy();
    expect(screen.getByText(/loki · mk4/)).toBeTruthy();
  });
});

describe("log search", () => {
  it("sends search text in the body and keeps it out of the URL", async () => {
    state.logs = async () => ({ items: [{ ts: "2026-10-31T00:00:00Z", line: "boom", labels: {}, environment: "mk4" }], errors: [{ environment: "eks", namespace: null, error: "loki down", denied: false }], environments: ["mk4"], queries: {}, collected_at: "2026-10-31T00:00:00Z" });
    render(<MemoryRouter initialEntries={["/logs"]}><Logs /></MemoryRouter>);
    await screen.findByText("boom");
    expect(screen.getByText(/returned partial data/)).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Contains"), { target: { value: "secret-request" } });
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Search" })); });
    await waitFor(() => expect(sent("/multi/logs").at(-1)?.body.contains).toBe("secret-request"));
    expect(state.calls.every((c) => !c.path.includes("secret-request"))).toBe(true);
  });
});
