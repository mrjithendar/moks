import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { FeedbackProvider } from "../ui";
import { KnowledgeUsed, Report } from "./Investigate";
import { Readiness } from "./Insights";
import { MLInsights, observationLabel } from "./ML";
import { SimilarIncidents } from "./MLParts";

const perms = new Set(["ml.read", "ml.feedback", "investigation.read"]);
vi.mock("../auth", () => ({ useAuth: () => ({ can: (p: string) => perms.has(p) }) }));

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const similar = (over: any = {}) => ({ status: "ok", mode: "enabled", note: "Historical similarity does not show the current cause.", results: [{
  incident_id: "i-1", title: "payments crash loop: missing DB URL", environment: "prod", namespace: "shop", score: 0.71, age_days: 12,
  matching: { symptoms: ["crashloop"], signature: ["payments_db_url"], services: ["payments"], change_context: null },
  differences: { only_now: ["oomkilled"], only_then: [], services_then: [], change_context: "a rollout preceded this one only" },
  resolution: { status: "resolved", cause: "PAYMENTS_DB_URL removed", resolution: "restored payments-config", cause_verified: true } }], ...over });

describe("Similar incidents", () => {
  it("shows what matched, what differs, age and how it ended, and records a judgment", async () => {
    const fetch = vi.fn(async () => new Response(JSON.stringify({ id: "j", verdict: "not_similar", state: "open" }), { status: 201 }));
    vi.stubGlobal("fetch", fetch);
    render(<MemoryRouter><SimilarIncidents s={similar()} investigationId="inv-1" /></MemoryRouter>);
    expect(screen.getByText("Similar incidents")).toBeTruthy();
    expect(screen.getByText(/does not show the current cause/)).toBeTruthy();
    expect(screen.getByRole("link", { name: /payments crash loop/ }).getAttribute("href")).toBe("/incidents/i-1");
    expect(screen.getByText("cause verified")).toBeTruthy();
    expect(screen.getByText(/only now: oomkilled/)).toBeTruthy();
    expect(screen.getByText("12 days ago")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Not similar" }));
    await waitFor(() => expect(screen.getByText("Recorded for review.")).toBeTruthy());
    const [url, init] = fetch.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toContain("/ml/learning/judgments");
    expect(JSON.parse(String(init.body))).toEqual({ target_kind: "similar_incident", target_id: "i-1", verdict: "not_similar", investigation_id: "inv-1" });
  });

  it("explains an empty result, and stays hidden when disabled or in shadow mode", () => {
    render(<MemoryRouter><SimilarIncidents s={similar({ status: "empty", results: [] })} /></MemoryRouter>);
    expect(screen.getByText(/Only incidents with an approved postmortem are compared/i)).toBeTruthy();
    cleanup();
    const { container } = render(<MemoryRouter><SimilarIncidents s={{ status: "disabled", results: [] }} /><SimilarIncidents s={similar({ mode: "shadow" })} /></MemoryRouter>);
    expect(container.textContent).toBe("");
  });

  it("appears in a troubleshooting report", () => {
    const base = { summary: "s", observed_facts: [], suspected_causes: [], missing_evidence: [], next_steps: [], proposed_remediation: [], generated_by: "rules" };
    render(<MemoryRouter><Report report={{ ...base, similar_incidents: similar() }} onCite={vi.fn()} /></MemoryRouter>);
    expect(screen.getByText("Similar incidents")).toBeTruthy();
  });
});

describe("Passage states", () => {
  const p = (over: any) => ({ ref: "E1", rank: 1, chunk_id: Math.random().toString(), query: 0, authority: "target", basis: [], excluded: null, path: "a.md", freshness: {}, ...over });

  it("credits only the cited passage of a search and labels the rest", () => {
    const onCite = vi.fn();
    render(<KnowledgeUsed onCite={onCite} k={{ status: "retrieved", scope: {}, queries: [], reasons: [], hints: [], note: "n", version: 2,
      counts: { retrieved: 3, eligible: 3, supplied: 2, cited: 1, used: 1 },
      passages: [p({ id: "E1.1", state: "supplied", provided: true, excluded: "not cited in the answer" }), p({ id: "E1.2", state: "cited", cited: true, used: true, provided: true }),
        p({ id: "E1.3", state: "eligible", excluded: "eligible, but not given to the model within the prompt budget" })] }} />);
    expect(screen.getByText("1 of 3")).toBeTruthy();
    expect(screen.getByText(/Retrieved 3 · eligible 3 · given to the model 2 · cited 1/)).toBeTruthy();
    expect(screen.getByText("cited")).toBeTruthy();
    expect(screen.getByText("given to the model, not cited")).toBeTruthy();
    expect(screen.getByText(/not given to the model within the prompt budget/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "E1.2" }));
    expect(onCite).toHaveBeenCalledWith("E1");  // the passage opens its search's evidence
  });

  it("renders a report from before passage ids", () => {
    render(<KnowledgeUsed onCite={vi.fn()} k={{ status: "retrieved", scope: {}, queries: [], reasons: [], hints: [], note: "n", counts: { retrieved: 2, usable: 2, used: 1, excluded: 0 },
      passages: [p({ used: true, provided: true }), p({ used: false, provided: true, excluded: "not cited in the answer" })] }} />);
    expect(screen.getByText("1 of 2")).toBeTruthy();
    expect(screen.getByText("Cited in this answer")).toBeTruthy();
    expect(screen.getAllByRole("button", { name: "E1" }).length).toBe(2);
  });
});

describe("Readiness counts", () => {
  it("shows ingested, indexed, eligible and why the rest is excluded", async () => {
    const health = { ready: true, source: { scope: { environment: "prod", namespace: "*", shared_globally: false }, profiles: [] },
      upstream: { state: "complete", files: { listed: 6, included: 6, excluded_by_policy: 0, excluded_by_content: 0, failed: 0, pending: 0 } },
      documents: { active: 5, draft: 0, quarantined: 1, archived: 0 }, indexing: { state: "complete", backend: "postgres", outbox_pending: 0, outbox_dead: 0, documents_behind: 0 },
      searchable: { documents: 3, chunks: 3 }, reasons: [],
      retrieval: { ingested: 6, indexed: 6, eligible: 3, excluded: { review: 1, freshness: 2, lifecycle: 0 }, flagged: { superseded: 1, deprecated: 1, stale: 0 }, operations: { pending: 0, failed: 0 } } };
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(health), { status: 200 })));
    render(<MemoryRouter><FeedbackProvider><Readiness id="s1" /></FeedbackProvider></MemoryRouter>);
    await waitFor(() => expect(screen.getByText("3 eligible for retrieval")).toBeTruthy());
    expect(screen.getByLabelText("Retrieval eligibility").textContent).toMatch(/1 awaiting review, 2 past freshness or review limits, 0 by lifecycle/);
  });
});

describe("ML insights", () => {
  const obs = (over: any) => ({ id: Math.random().toString(), cohort: "prod/shop/payments", algorithm: "seasonal_baseline", status: "scored", unusual: false, mode: "shadow", score: 1.2, threshold: 4,
    window_from: "2026-11-04T11:00:00Z", window_to: "2026-11-04T12:00:00Z", deviations: [], data_quality: { absent: [], coverage: {} }, context: { role: "champion", model_version: 2, note: "Unusual for this workload. Not a fault." }, feedback: {}, ...over });

  it("labels cold start and missing telemetry instead of calling them healthy", () => {
    expect(observationLabel(obs({ status: "insufficient_history" }))).toEqual(["neutral", "insufficient history"]);
    expect(observationLabel(obs({ status: "no_telemetry" }))).toEqual(["warn", "no telemetry"]);
    expect(observationLabel(obs({ unusual: true }))[1]).toBe("unusual");
  });

  it("lists observations with their mode and records feedback from the drawer", async () => {
    const unusual = obs({ unusual: true, score: 6.1, deviations: [{ feature: "latency_p95", z: 6.1, observed: 0.7, typical: 0.12, direction: "higher", points: 12 }] });
    const fetch = vi.fn(async (url: string, init?: RequestInit) => {
      if (url.includes("/ml/status")) return new Response(JSON.stringify({ capabilities: { anomaly_detection: { mode: "shadow", allowed: ["disabled", "shadow"], describe: "Shadow only." } } }), { status: 200 });
      if (url.includes("/multi/scopes")) return new Response(JSON.stringify({ scopes: [] }), { status: 200 });
      if (url.includes("/feedback")) return new Response(JSON.stringify({ ...unusual, feedback: { verdict: JSON.parse(String(init?.body)).verdict } }), { status: 200 });
      return new Response(JSON.stringify({ observations: [unusual, obs({ status: "insufficient_history", score: null })], note: "n" }), { status: 200 });
    });
    vi.stubGlobal("fetch", fetch);
    render(<MemoryRouter><FeedbackProvider><MLInsights /></FeedbackProvider></MemoryRouter>);
    await waitFor(() => expect(screen.getAllByText("prod/shop/payments").length).toBe(2));
    expect(screen.getByText("shadow")).toBeTruthy();
    expect(screen.getByText("insufficient history")).toBeTruthy();
    fireEvent.click(screen.getAllByRole("button", { name: "prod/shop/payments" })[0]);
    expect(await screen.findByText(/they do not say why/i)).toBeTruthy();
    expect(screen.getByText("p95 latency")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "False alarm" }));
    await waitFor(() => expect(fetch.mock.calls.some(([u]) => String(u).includes("/feedback"))).toBe(true));
  });
});
