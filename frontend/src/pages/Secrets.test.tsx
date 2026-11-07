import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api";
import { FeedbackProvider } from "../ui";
import { SecretPlan, Secrets } from "./Secrets";

const calls: { path: string; method?: string; body?: any }[] = [];
let responses: Record<string, unknown> = {};

vi.mock("../api", async (real) => ({
  ...(await real<object>()),
  api: vi.fn(async (path: string, opts: any = {}) => {
    calls.push({ path, method: opts.method, body: opts.body });
    const r = responses[`${opts.method ?? "GET"} ${path.split("?")[0]}`] ?? responses[path.split("?")[0]];
    if (r instanceof Error) throw r;
    return typeof r === "function" ? (r as any)(opts.body) : r;
  }),
}));
vi.mock("../auth", () => ({ useAuth: () => ({ can: () => true }) }));

const scope = { kubernetes: [{ id: "k1", name: "east", writable: true, cluster_uid: "u", namespaces: ["shop"], error: null }],
  aws: [{ id: "a1", name: "aws", account_id: "123456789012", region: "eu-west-1", allowed_prefixes: ["moks/"], kubernetes_namespaces: ["shop"] }],
  cluster_wide: true, can: { discover: true, compare: true, map: true, propose: true, approve: true, execute: true } };
const config = { enabled: true, environments: [{ name: "mk4", display_name: "mk4", is_production: false, aws_connectors: 1 }] };
const mapping = { id: "m1", version: 1, cluster_name: "east", namespace: "shop", secret_name: "db", aws_account: "123456789012", aws_region: "eu-west-1", aws_secret_name: "moks/mk4/east/shop/db" };
const side = (version: string, keys: Record<string, any>) => ({ exists: true, resource_version: version, version_id: version, type: "Opaque", owner: { manager: null }, keys, last_modified: "2026-11-07T00:00:00Z" });
const item = (over: Record<string, any> = {}) => ({
  id: "i1", mapping, included: true, classification: "changed_both", first_sync: true, proposal: null, resolution: null, key_choices: {}, targets: [], actions: {},
  kubernetes: side("101", { password: { fp: "aaaa1111", empty: false, binary: false } }), aws: side("v1", { password: { fp: "bbbb2222", empty: false, binary: false }, extra: { fp: "cccc3333", empty: true, binary: false } }),
  diff: {}, warnings: [], impact: [], blocked: "choose Kubernetes, AWS or an edited resolution", status: "conflict", write_state: {}, attempts: 0, error: null, ...over });
const plan = (over: Record<string, any> = {}) => ({ id: "p1", kind: "sync", status: "draft", reason: "rotate", version: 3, plan_hash: "f".repeat(64), requested_by: "op@example.com", created_at: "2026-11-07T00:00:00Z",
  approvals: [], items: [item()], summary: {}, expired: false, expires_at: null, allowed: { edit: true, submit: true, approve: false, execute: false, retry: false, cancel: true, refresh: true }, ...over });

function page(path = "/secrets?env=mk4") {
  render(<MemoryRouter initialEntries={[path]}><FeedbackProvider><Routes>
    <Route path="/secrets" element={<Secrets />} /><Route path="/secrets/plans/:id" element={<SecretPlan />} />
  </Routes></FeedbackProvider></MemoryRouter>);
}

describe("Secrets sync", () => {
  afterEach(() => { cleanup(); calls.length = 0; });

  it("says when the feature is off", async () => {
    responses = { "/secrets/config": { enabled: false, environments: [] } };
    page();
    expect(await screen.findByText("Secret sync is off")).toBeTruthy();
  });

  it("lists the inventory with owners, partial discovery, and masks nothing it was not given", async () => {
    responses = { "/secrets/config": config, "/secrets/mk4/scope": scope, "/secrets/mk4/inventory": {
      items: [{ id: "r1", side: "kubernetes", connector_id: "k1", namespace: "shop", name: "db", type: "Opaque", keys: ["password"], key_count: 1, owner: { manager: "external-secrets", detail: "" },
        syncable: true, excluded: null, last_modified: null, version: "1", sync_status: "unmapped", extra: {}, mapping: null },
        { id: "r2", side: "kubernetes", connector_id: "k1", namespace: "shop", name: "token", type: "kubernetes.io/service-account-token", keys: null, key_count: 1, owner: { manager: "kubernetes", detail: "" },
          syncable: false, excluded: "service-account token issued by Kubernetes", last_modified: null, version: "2", sync_status: "excluded", extra: {}, mapping: null }],
      truncated: false, hidden: 2, scopes: [{ scope: "shop", complete: false, count: 2, error: "RBAC denied list", discovered_at: null }] } };
    page();
    expect(await screen.findByText("service-account token issued by Kubernetes")).toBeTruthy();  // rows loaded
    expect(screen.getAllByText("external-secrets").some((e) => e.classList.contains("badge"))).toBe(true);
    expect(screen.getAllByText(/password/).length).toBeGreaterThan(0);
    expect(screen.getByText(/RBAC denied list/)).toBeTruthy();
    expect(screen.getByText(/2 secrets are outside your namespaces/)).toBeTruthy();
    expect((screen.getByLabelText("Select token") as HTMLInputElement).disabled).toBe(true);
  });

  it("explains a denied inventory", async () => {
    responses = { "/secrets/config": config, "/secrets/mk4/scope": scope, "/secrets/mk4/inventory": new ApiError(403, "You need secrets.discover in mk4") };
    page();
    expect(await screen.findByText("You need secrets.discover in mk4")).toBeTruthy();
  });

  it("reviews a first-sync conflict with masked fingerprints and per-key resolution", async () => {
    responses = { "/secrets/plans/p1": plan(), "/secrets/mk4/plans/p1": plan(), "PATCH /secrets/mk4/plans/p1/items/i1": plan() };
    page("/secrets/plans/p1?env=mk4");
    expect(await screen.findByText(/First sync: there is no verified baseline/)).toBeTruthy();
    expect(screen.getAllByText(/not used to pick a winner/).length).toBe(2);
    fireEvent.click(screen.getByLabelText("Choose per key"));
    await waitFor(() => expect(calls.some((c) => c.method === "PATCH" && c.body.resolution === "edited" && c.body.version === 3)).toBe(true));
  });

  it("shows masked diffs, workload impact and approval controls", async () => {
    const p = plan({ status: "pending", allowed: { edit: false, submit: false, approve: true, execute: false, retry: false, cancel: false, refresh: true },
      items: [item({ classification: "changed_aws", first_sync: false, status: "pending", blocked: null, targets: ["kubernetes"], actions: { kubernetes: "update" }, resolution: null, proposal: "aws",
        diff: { kubernetes: { added: [{ key: "extra", to: { fp: "cccc3333", empty: true, binary: false } }], removed: [], changed: [{ key: "password", from: { fp: "aaaa1111", empty: false, binary: false }, to: { fp: "bbbb2222", empty: false, binary: false } }], unchanged: [], type_change: null } },
        impact: [{ kind: "Deployment", name: "api", how: ["env"], keys: ["password"], restart_required: true, restartable: true, note: "environment variables are read at container start: restart to pick up the change" }] })] });
    responses = { "/secrets/mk4/plans/p1": p, "POST /secrets/mk4/plans/p1/decision": { ...p, status: "approved" } };
    page("/secrets/plans/p1?env=mk4");
    expect(await screen.findByText("••••aaaa1111")).toBeTruthy();
    expect(screen.getByText("••••bbbb2222")).toBeTruthy();
    expect(screen.getByText("empty")).toBeTruthy();
    expect(screen.getByText(/Update in Kubernetes \(east\/shop\)/)).toBeTruthy();
    expect(screen.getByText(/A restart is a separate action/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Propose restart" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(calls.find((c) => c.path.endsWith("/decision"))?.body).toMatchObject({ approve: true, plan_hash: "f".repeat(64) }));
  });

  it("offers a refresh for stale items instead of a retry", async () => {
    responses = { "/secrets/mk4/plans/p1": plan({ status: "failed", allowed: { edit: false, submit: false, approve: false, execute: false, retry: true, cancel: false, refresh: true },
      items: [item({ status: "stale", error: "kubernetes changed after the comparison", blocked: null })] }) };
    page("/secrets/plans/p1?env=mk4");
    expect(await screen.findByText("Some items changed after review")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Retry failed items" })).toBeNull();
    expect(screen.getByRole("button", { name: "Refresh plan" })).toBeTruthy();
  });
});
