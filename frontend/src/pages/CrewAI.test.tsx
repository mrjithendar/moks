import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

const state = vi.hoisted(() => ({ crewai: null as any, posts: [] as any[] }));
vi.mock("../auth", () => ({ useAuth: () => ({ can: () => true, me: { email: "s@x" } }) }));
vi.mock("../api", async (orig) => ({ ...(await orig<any>()), api: vi.fn(async (path: string, opts?: any) => {
  if (path === "/investigate/config") return { llm: { provider: "openai", model: "m" }, skills: [], environments: ["mk4"], crewai: state.crewai };
  if (opts?.method === "POST") { state.posts.push({ path, body: opts.body }); return { id: "i1", conversation_id: "c1" }; }
  return {};
}) }));

import { NewInvestigation, Report } from "./Investigate";

const off = { enabled: false, available: false, reason: "disabled: MOKS_CREWAI_ENABLED is not true", version: null, limits: { timeout_seconds: 120, max_tool_calls: 20, max_iterations_per_agent: 5 } };
const on = { ...off, enabled: true, available: true, reason: null, version: "1.15.36", llm: "openai/m" };
const page = () => render(<MemoryRouter><NewInvestigation /></MemoryRouter>);

describe("the CrewAI investigation option", () => {
  afterEach(() => { cleanup(); state.posts = []; });

  it("is not offered when the backend reports it unavailable", async () => {
    state.crewai = off;
    page();
    await screen.findByText(/SRE skills/);
    expect(screen.queryByText("CrewAI investigation")).toBeNull();
    expect(screen.getByRole("textbox", { name: "Question" })).toBeTruthy();
  });

  it("is not offered by a backend that predates it", async () => {
    state.crewai = undefined;
    page();
    await screen.findByText(/SRE skills/);
    expect(screen.queryByText("CrewAI investigation")).toBeNull();
  });

  it("starts a scoped workload investigation when available", async () => {
    state.crewai = on;
    page();
    fireEvent.click(await screen.findByLabelText("CrewAI investigation"));
    fireEvent.change(screen.getByLabelText("Namespace"), { target: { value: "crewai-demo" } });
    fireEvent.change(screen.getByLabelText("Workload"), { target: { value: "web" } });
    fireEvent.click(screen.getByText("Run CrewAI investigation"));
    await waitFor(() => expect(state.posts).toHaveLength(1));
    expect(state.posts[0]).toEqual({ path: "/investigations/crewai", body: { environment: "mk4", namespace: "crewai-demo", workload: "web", kind: "Deployment", minutes: undefined } });
  });
});

describe("the CrewAI part of a report", () => {
  afterEach(cleanup);
  const base = { summary: "s", observed_facts: [], suspected_causes: [], missing_evidence: [], next_steps: [], proposed_remediation: [], generated_by: "llm" };

  it("shows agents, gaps, rejected citations and registry excerpts", () => {
    render(<MemoryRouter><Report onCite={vi.fn()} report={{ ...base, crewai: {
      version: "1.15.36", status: "partial", stopped: "the crew's time limit of 120s was reached", tool_calls: 3,
      agents: [{ agent: "workload", label: "Workload investigator", status: "done" }, { agent: "change", label: "Change investigator", status: "running" }],
      gaps: [{ source: "git", reason: "no repository configured" }], validation: { citations_rejected: ["E99"], claims_removed: [{ statement: "x" }], confidence_lowered: [] },
      evidence: [{ id: "E1", source: "k8s_triage", excerpt: "ImagePullBackOff", truncated: false }] } }} /></MemoryRouter>);
    expect(screen.getByText(/Workload investigator: done/)).toBeTruthy();
    expect(screen.getByText(/Stopped early/)).toBeTruthy();
    expect(screen.getByText(/no repository configured/)).toBeTruthy();
    expect(screen.getByText(/1 invented citation\(s\) rejected \(E99\)/)).toBeTruthy();
    expect(screen.getByText("ImagePullBackOff")).toBeTruthy();
  });

  it("is absent from ordinary reports", () => {
    render(<MemoryRouter><Report onCite={vi.fn()} report={base} /></MemoryRouter>);
    expect(screen.queryByText(/CrewAI run/)).toBeNull();
  });
});
