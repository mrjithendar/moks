import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { FeedbackProvider } from "../ui";
import { Skills } from "./Skills";

const perms = { list: [] as string[] };
vi.mock("../auth", () => ({ useAuth: () => ({ can: (p: string) => perms.list.includes(p) }) }));

const row = { name: "crashloop-triage", title: "Crashloop triage", description: "Finds why a pod restarts.", category: "kubernetes", source: "builtin", enabled: true, owner: "sre", version: "1", digest: "a".repeat(64), state: "published", triggers: { keywords: ["crash"], findings: ["crashloop"] }, tools: [], remediation: [], resource_types: ["Pod"], needs: ["kubernetes"], detection_rules: [], environments: ["*"],
  policy_environments: [], availability: { prod: { state: "partial", available: true, reasons: ["needs the loki connector"], actions: ["Add a loki connector to this environment under Connectors."], checks: [] } } };
const full = { ...row, steps: [{ tool: "k8s_pod_logs", required: true, purpose: "crashed container" }], body: "Read the logs.", telemetry: [], permissions: [], detection: [], history: [{ version: "1", digest: row.digest, state: "published" }, { version: "0", digest: "b".repeat(64), state: "deprecated" }],
  enforced: [{ field: "evidence_required", item: "k8s_pod_logs", enforced: true, how: "at least 1 successful calls" }, { field: "recovery_validation", item: "watch the graph", enforced: false, how: "guidance only" }], evaluation_results: { created_at: "2026-11-01T00:00:00Z", generation: 3, cases: [{ case: "crashloop-config", kind: "execution", passed: true, failures: [] }] } };
const generation = { active: { generation: 3, digest: "c".repeat(64), skills: 50 }, processes: [{ instance: "a", component: "api", generation: 2, current: false, error: "this build cannot read it" }], consistent: false };
const waiting = { skill: "demo", version: "2", digest: "d".repeat(64), state: "validated", source: "proposal", owner: "sre", reviewed_at: null, validation: [] };

function respond(extra: Record<string, unknown> = {}) {
  const routes: Record<string, unknown> = { "/skills/generation": generation, "/skills/registry": { versions: [waiting], generations: [] }, "/skills/crashloop-triage/runs": [], "/skills/crashloop-triage": full, "/skills": { skills: [row], errors: [] }, ...extra };
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    const key = Object.keys(routes).find((k) => url.includes(k));
    return new Response(JSON.stringify(key ? routes[key] : {}), { status: key ? 200 : 404 });
  }));
}
const page = () => render(<MemoryRouter><FeedbackProvider><Skills /></FeedbackProvider></MemoryRouter>);

beforeEach(() => { perms.list = []; });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("Skills", () => {
  it("shows a process that could not move, and that a registry change is not a publication", async () => {
    respond();
    page();
    expect(await screen.findByText(/kept its last valid generation: this build cannot read it/)).toBeTruthy();
    expect(screen.getByText("processes differ")).toBeTruthy();
    expect(screen.queryByText("Register from files")).toBeNull();
  });

  it("never presents guidance as an enforced check", async () => {
    respond();
    page();
    fireEvent.click(await screen.findByRole("button", { name: /Crashloop triage/ }));
    const line = (await screen.findByText("watch the graph")).parentElement!;
    expect(within(line).getByText("guidance")).toBeTruthy();
    expect(within(line).queryByText("enforced")).toBeNull();
    expect(within((await screen.findByText("k8s_pod_logs", { selector: ".trunc" })).parentElement!).getByText("enforced")).toBeTruthy();
  });

  it("says what to do about readiness, and shows evaluation results", async () => {
    respond();
    page();
    fireEvent.click(await screen.findByRole("button", { name: /Crashloop triage/ }));
    expect(await screen.findByText("Add a loki connector to this environment under Connectors.")).toBeTruthy();
    expect(screen.getByText(/crashloop-config/)).toBeTruthy();
  });

  it("hides rollback and publication from people who cannot use them", async () => {
    respond();
    page();
    fireEvent.click(await screen.findByRole("button", { name: /Crashloop triage/ }));
    await screen.findByText("Compare");
    expect(screen.queryByText("Roll back")).toBeNull();
    expect(screen.queryByText("Turn off")).toBeNull();
    expect(screen.queryByText(/Waiting for review/)).toBeNull();
  });

  it("makes review come before publication", async () => {
    perms.list = ["skills.review", "skills.publish"];
    respond();
    page();
    expect(await screen.findByText(/Waiting for review/)).toBeTruthy();
    expect((screen.getByRole("button", { name: "Publish", hidden: true }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole("button", { name: "Mark reviewed", hidden: true }) as HTMLButtonElement).disabled).toBe(true);
  });
});
