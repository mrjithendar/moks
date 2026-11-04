import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { FeedbackProvider } from "../ui";
import { KnowledgeUsed, Report } from "./Investigate";
import { Readiness, TestRetrieval } from "./Insights";

vi.mock("../auth", () => ({ useAuth: () => ({ can: () => true }) }));

const passage = (over: any = {}) => ({ ref: "E1", chunk_id: "c1", query: 0, rank: 1, authority: "target", basis: ["source maps it to service payments"], excluded: null, used: true, provided: true,
  source: "payments-docs", source_type: "github", repository: "acme/payments", path: "docs/runbook.md", revision: "0000000000000000000000000000000000000007", lines: [3, 9],
  url: "https://github.com/acme/payments/blob/7/docs/runbook.md?plain=1#L3-L9", title: "Payments runbook", heading: "Payments runbook > CrashLoopBackOff",
  freshness: { state: "current", trust: "unreviewed" }, excerpt: "When payments pods crash-loop, read the previous container's logs first", ...over });
const knowledge = (over: any = {}) => ({ status: "retrieved", scope: { environment: "prod", namespace: "shop", target: null, repository: null, history_used: false },
  queries: [{ stage: "initial", query: "why is payments crashing", status: "ok", results: 2, mode: "keyword", reused: false }], hints: [], reasons: [],
  passages: [passage(), passage({ ref: "E1", chunk_id: "c2", authority: "unrelated", used: false, excluded: "written for billing, not payments", repository: "acme/billing", path: "docs/billing.md", url: "javascript:alert(1)" })],
  counts: { retrieved: 2, usable: 1, used: 1, excluded: 1 }, note: "Documented knowledge says how things are meant to work. It does not show the current state of any system.", ...over });

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("Knowledge used", () => {
  it("shows queries, the passage used with its repository, path, lines and commit, and why the rest was left out", () => {
    const onCite = vi.fn();
    render(<KnowledgeUsed k={knowledge()} onCite={onCite} />);
    expect(screen.getByText("1 of 2")).toBeTruthy();
    expect(screen.getByText("why is payments crashing")).toBeTruthy();
    const link = screen.getByRole("link", { name: /acme\/payments:docs\/runbook.md L3-9/ });
    expect(link.getAttribute("href")).toContain("#L3-L9");
    expect(screen.getAllByText("@ 0000000000").length).toBe(2);
    expect(screen.getByText("about the target")).toBeTruthy();
    expect(screen.getByText(/Not used: written for billing, not payments/)).toBeTruthy();
    expect(screen.queryByRole("link", { name: /billing/ })).toBeNull();  // never a link from a non-web address
    expect(screen.getByText(/does not show the current state/)).toBeTruthy();
    fireEvent.click(screen.getAllByRole("button", { name: "E1" })[0]);
    expect(onCite).toHaveBeenCalledWith("E1");
  });

  it("says what happened instead of a generic empty message", () => {
    render(<KnowledgeUsed k={knowledge({ status: "failed", passages: [], counts: { retrieved: 0, usable: 0, used: 0, excluded: 0 }, reasons: ["Knowledge search failed; documented procedures may exist but could not be read."],
      queries: [{ stage: "initial", query: "q", status: "failed", reason: "unavailable: index down" }] })} onCite={vi.fn()} />);
    expect(screen.getByText(/could not be checked/)).toBeTruthy();
    expect(screen.getByText("search failed")).toBeTruthy();
    cleanup();
    render(<KnowledgeUsed k={knowledge({ status: "empty", passages: [], counts: { retrieved: 0, usable: 0, used: 0, excluded: 0 }, hints: ["1 of the 2 sources you can read here have not finished syncing."] })} onCite={vi.fn()} />);
    expect(screen.getByText(/found no passage you may read/)).toBeTruthy();
    expect(screen.getByText(/have not finished syncing/)).toBeTruthy();
  });

  it("appears in troubleshooting and guidance reports, and not for reports from before it existed", () => {
    const base = { summary: "s", observed_facts: [], suspected_causes: [], missing_evidence: [], next_steps: [], proposed_remediation: [], generated_by: "rules" };
    render(<MemoryRouter><Report report={{ ...base, knowledge: knowledge() }} onCite={vi.fn()} /></MemoryRouter>);
    expect(screen.getByText("Knowledge used")).toBeTruthy();
    cleanup();
    render(<MemoryRouter><Report report={{ ...base, response_type: "deployment_guidance", answer: { steps: [{ text: "Run `helm upgrade`", command: "helm upgrade", sources: ["E1"], general: true }] }, knowledge: knowledge() }} onCite={vi.fn()} /></MemoryRouter>);
    expect(screen.getByText("Knowledge used")).toBeTruthy();
    expect(screen.getAllByText("general guidance").length).toBeGreaterThan(0);
    cleanup();
    render(<MemoryRouter><Report report={base} onCite={vi.fn()} /></MemoryRouter>);
    expect(screen.queryByText("Knowledge used")).toBeNull();
  });
});

describe("source readiness and test retrieval", () => {
  const health = { ready: false, source: { scope: { environment: "prod", namespace: "shop", shared_globally: false }, profiles: ["documentation"] },
    upstream: { state: "complete", last_success_at: null, revision: "abc", files: { listed: 6, included: 2, excluded_by_policy: 4, excluded_by_content: 0, failed: 0, pending: 0 }, policy_sample: [{ path: "src/main.go", rule: "no include rule" }] },
    documents: { active: 0, draft: 0, quarantined: 2, archived: 0 }, indexing: { state: "complete", backend: "postgres", mode: "keyword-only", outbox_pending: 0, outbox_dead: 0, documents_behind: 0 },
    searchable: { documents: 0, chunks: 0 }, reasons: [{ code: "awaiting_review", message: "Documents are waiting for review (draft or quarantined) and are not retrieved until approved.", detail: "2 quarantined, 0 draft" }] };

  it("separates sync from indexing and names why nothing is searchable", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(health), { status: 200 })));
    render(<MemoryRouter><FeedbackProvider><Readiness id="s1" /></FeedbackProvider></MemoryRouter>);
    await waitFor(() => expect(screen.getByText("not ready")).toBeTruthy());
    expect(screen.getByText(/Sync: complete · Index: complete/)).toBeTruthy();
    expect(screen.getByText(/4 left out by policy/)).toBeTruthy();
    expect(screen.getByText(/waiting for review/)).toBeTruthy();
  });

  it("runs the investigation's search on one source and shows the reasons it found nothing", async () => {
    const fetch = vi.fn(async () => new Response(JSON.stringify({ results: [], mode: "keyword", reasons: [{ code: "outside_scope", message: "The source is outside the requested environment or namespace.", detail: "source scope prod/shop; requested dev/*" }] }), { status: 200 }));
    vi.stubGlobal("fetch", fetch);
    render(<MemoryRouter><FeedbackProvider><TestRetrieval id="s1" env="dev" ns="" /></FeedbackProvider></MemoryRouter>);
    fireEvent.change(screen.getByLabelText("Question"), { target: { value: "payments crash" } });
    fireEvent.click(screen.getByRole("button", { name: "Test" }));
    await waitFor(() => expect(screen.getByText(/outside the requested environment/)).toBeTruthy());
    const [url, init] = fetch.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toContain("/knowledge/sources/s1/test-retrieval");
    expect(JSON.parse(String(init.body))).toMatchObject({ query: "payments crash", environment: "dev", namespace: null });
  });
});
