import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { EvidenceKind, Report } from "./Investigate";

const base = { summary: "s", observed_facts: [], suspected_causes: [], missing_evidence: [], next_steps: [], proposed_remediation: [], generated_by: "llm" };
const show = (over: any) => render(<MemoryRouter><Report report={{ ...base, ...over }} onCite={vi.fn()} /></MemoryRouter>);

describe("how a report says what it knows", () => {
  afterEach(cleanup);

  it("names each kind of evidence, so a failed call never reads as an observation", () => {
    render(<EvidenceKind kind="tool_error" ok={false} />);
    expect(screen.getByText("collection failed")).toBeTruthy();
    render(<EvidenceKind kind="historical" ok />);
    expect(screen.getByText("historical")).toBeTruthy();
  });

  it("falls back for evidence stored before kinds existed", () => {
    render(<EvidenceKind ok={false} />);
    expect(screen.getByText("collection failed")).toBeTruthy();
  });

  it("keeps an observed failure apart from a verified defect", () => {
    show({ verified_findings: [
      { category: "observed_failure", cause: "api was OOMKilled", evidence: ["E1"], check: "deterministic detector: oomkilled", target: "pod/api", at: "2026-11-02 12:00 UTC" },
      { category: "configuration_defect", cause: "selector matches no pods", evidence: ["E2"], check: "deterministic detector: service_selector_mismatch" },
    ] });
    expect(screen.getByText("Observed failures")).toBeTruthy();
    expect(screen.getByText("Verified configuration defects")).toBeTruthy();
    expect(screen.getByText(/target pod\/api/)).toBeTruthy();
  });

  it("still shows reports written before the categories", () => {
    show({ verified_causes: [{ cause: "old cause", evidence: ["E1"], check: "deterministic detector: x" }] });
    expect(screen.getByText("old cause")).toBeTruthy();
  });

  it("shows qualifiers, disagreement and demoted claims", () => {
    show({ observed_facts: [{ statement: "3 pods are running", evidence: ["E1"], kind: "observation", qualifiers: ["another source disagrees about firing alerts"] }],
      conflicts: [{ subject: "firing alerts", detail: "Prometheus has firing alerts", evidence: ["E1", "E2"] }],
      suspected_causes: [{ cause: "All is healthy", confidence: "low", evidence: [], origin: "demoted_fact", reasoning: "" }] });
    expect(screen.getByText("Sources disagree")).toBeTruthy();
    expect(screen.getByText(/another source disagrees/)).toBeTruthy();
    expect(screen.getByText(/Treat it as a hypothesis/)).toBeTruthy();
  });

  it("says a claim check is not proof, and when a run resumed", () => {
    show({ validation: { method: "rules", summary_sentences_removed: [{ sentence: "x" }] }, execution: { attempt: 2, resumed_after: "logs" } });
    expect(screen.getByText(/does not prove a passage supports a claim/)).toBeTruthy();
    expect(screen.getByText(/resumed after logs/)).toBeTruthy();
    expect(screen.getByText(/went beyond the evidence/)).toBeTruthy();
  });

  it("shows when a skill did not finish", () => {
    show({ skill_runs: [{ skill: "s", version: "1", digest: "abc", reasons: [], steps: [], outcome: "incomplete", gaps: ["step k8s_nodes skipped"] }] });
    expect(screen.getByText("incomplete")).toBeTruthy();
  });
});
