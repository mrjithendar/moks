import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { FeedbackProvider } from "../ui";
import { KnowledgeExplorer, RetrievalWorkspace, safeHref } from "./Knowledge";

const perms = { list: ["knowledge.read", "knowledge.test"] };
vi.mock("../auth", () => ({ useAuth: () => ({ can: (p: string) => perms.list.includes(p) }) }));

const doc = { id: "d1", title: "Redis failover", path: "runbooks/redis-failover-and-a-very-long-path-name.md", kind: "markdown", status: "active", source: { id: "s1", name: "runbooks", type: "github", lifecycle: "active" }, environment: "prod", namespace: "team-a", service: "redis", resource_types: [], owner: "sre", team: "platform", classification: "internal", lifecycle: "active", trust: "reviewed", revision: "abcdef1234567890", checked_at: "2026-11-01T00:00:00Z", chunks: 4, freshness: { state: "current", reason: null }, indexing: "indexed", embedding: { state: "partial", vectors: 2, expected: 4, coverage: 0.5 }, note: null };
const list = { total: 1, page: 1, size: 25, documents: [doc], capped: false, index: { model: "gemini/x/3072", dimensions: 3072, mode: "hybrid" }, facets: { source: { runbooks: 1 }, environment: { prod: 1 }, namespace: { "team-a": 1 }, freshness: { current: 1 }, service: {}, resource_type: {}, kind: {}, owner: {}, team: {}, lifecycle: {}, classification: {}, indexing: {}, embedding: {} },
  progress: [{ id: "s1", name: "runbooks", type: "github", lifecycle: "active", documents: 1, sync: { status: "ok", listing: "complete", last_success_at: null }, indexing: { indexed: 1 }, embedding: { partial: 1 } }] };
const detail = { ...doc, preview: "# Redis failover\nPromote the replica.", preview_cut: false, url: "javascript:alert(1)", citation: { source: "runbooks", path: doc.path, revision: "abc", content_changed_in: "abc", pipeline_version: 3, current_pipeline: 3, content_hash: "git:1", content_revision: 2, indexed_revision: 2, size_bytes: 40 },
  review: { doc_state: "current" }, generations: [], superseded_by: null, replaces: [], can_review: false, can_operate: false };

function respond(routes: Record<string, unknown>) {
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    const key = Object.keys(routes).find((k) => url.includes(k));
    return new Response(JSON.stringify(key ? routes[key] : {}), { status: key ? 200 : 404 });
  }));
}
const page = (node: React.ReactNode) => render(<MemoryRouter><FeedbackProvider>{node}</FeedbackProvider></MemoryRouter>);

beforeEach(() => { perms.list = ["knowledge.read", "knowledge.test"]; });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("safeHref", () => {
  it("makes links only from web addresses", () => {
    expect(safeHref("https://github.com/a")).toBe("https://github.com/a");
    expect(safeHref("javascript:alert(1)")).toBeUndefined();
    expect(safeHref("data:text/html,x")).toBeUndefined();
    expect(safeHref(null)).toBeUndefined();
  });
});

describe("KnowledgeExplorer", () => {
  it("shows sync, indexing and embedding separately, and never a vector", async () => {
    respond({ "/chunks": { total: 0, chunks: [] }, "/explorer/documents/d1": detail, "/explorer/documents": list });
    page(<KnowledgeExplorer />);
    expect(await screen.findByText("Redis failover")).toBeTruthy();
    expect(screen.getByText("50%")).toBeTruthy();
    expect(screen.getByText(/Source progress/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /Redis failover/ }));
    const dialog = await screen.findByRole("dialog");
    await within(dialog).findByText(/Promote the replica/);
    expect(within(dialog).getByText(/Vectors themselves are not shown/)).toBeTruthy();
  });

  it("does not make a link from an unsafe stored address", async () => {
    respond({ "/chunks": { total: 0, chunks: [] }, "/explorer/documents/d1": detail, "/explorer/documents": list });
    page(<KnowledgeExplorer />);
    fireEvent.click(await screen.findByRole("button", { name: /Redis failover/ }));
    const dialog = await screen.findByRole("dialog");
    await within(dialog).findByText(/Promote the replica/);
    expect(within(dialog).queryByText("Open the original")).toBeNull();
  });

  it("offers review and retry only to those who may use them", async () => {
    respond({ "/chunks": { total: 0, chunks: [] }, "/explorer/documents/d1": { ...detail, trust: "unreviewed", can_review: true, indexing: "pending", can_operate: true, operations: { events: [], generation_errors: [], vector_state: "missing", vector_attempts: 2 } }, "/explorer/documents": list });
    page(<KnowledgeExplorer />);
    fireEvent.click(await screen.findByRole("button", { name: /Redis failover/ }));
    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByText("Mark reviewed")).toBeTruthy();
    expect(within(dialog).getByText("Retry indexing")).toBeTruthy();
    expect(within(dialog).getByText("Retries and errors")).toBeTruthy();
  });

  it("says when nothing matches rather than showing an empty table", async () => {
    respond({ "/explorer/documents": { ...list, total: 0, documents: [], progress: [] } });
    page(<KnowledgeExplorer />);
    expect(await screen.findByText("No documents match.")).toBeTruthy();
  });
});

describe("RetrievalWorkspace", () => {
  const hit = { chunk_id: "c1", document_id: "d1", score: 0.03, signals: { exact_identifier: 0.03 }, title: "Redis", heading: "Failover", text: "Promote the replica", citation: { source: "runbooks", path: "r.md", lines: [1, 2], revision: "abc", url: "javascript:x" }, scope: { environment: "prod", namespace: "a" }, freshness: { state: "current", trust: "reviewed" } };
  const result = { variants: [
    { name: "keyword", mode: "keyword", reranked: false, abstain: false, evidence: { state: "supported", reason: "The passages contain the terms searched for." }, latency_ms: 4, results: [hit] },
    { name: "vector", mode: "none", reranked: false, abstain: true, evidence: { state: "none", reason: "No readable document matched." }, fallback_reason: "no embedding model is active", results: [] },
  ], usage: { embedding_calls: 0, tokens: 0 }, reranker: { configured: false, note: "Reranking is off." }, note: "Documented guidance, not current facts." };

  it("says what actually ran and treats a score as a rank", async () => {
    respond({ "/retrieval/test": result, "/explorer/documents": list });
    page(<RetrievalWorkspace />);
    fireEvent.change(screen.getByLabelText("Question"), { target: { value: "redis failover" } });
    fireEvent.click(screen.getByRole("button", { name: "Compare" }));
    expect(await screen.findByText("ran: none")).toBeTruthy();
    expect(screen.getByText("no embedding model is active")).toBeTruthy();
    expect(screen.getByText(/not a probability/)).toBeTruthy();
    expect(screen.getByText("Reranking is off.")).toBeTruthy();
  });

  it("sends feedback for review, naming the passage", async () => {
    respond({ "/retrieval/test": result, "/knowledge/feedback": { id: "f", state: "open" }, "/explorer/documents": list });
    page(<RetrievalWorkspace />);
    fireEvent.change(screen.getByLabelText("Question"), { target: { value: "redis failover" } });
    fireEvent.click(screen.getByRole("button", { name: "Compare" }));
    fireEvent.click((await screen.findAllByRole("button", { name: "Outdated" }))[0]);
    await waitFor(() => expect((fetch as any).mock.calls.some(([u, o]: any[]) => u.includes("/feedback") && JSON.parse(o.body).chunk_id === "c1" && JSON.parse(o.body).verdict === "outdated")).toBe(true));
    expect(await screen.findByText(/changes nothing by itself/)).toBeTruthy();
  });
});
