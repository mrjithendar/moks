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
  cluster_wide: true, can: { discover: true, compare: true, map: true, propose: true, approve: true, execute: true, reveal: true }, discovery: { interval_minutes: 60, last_at: "2026-11-07T00:00:00Z" } };
const row = (over: Record<string, any> = {}) => ({ id: "r1", side: "kubernetes", connector_id: "k1", namespace: "shop", name: "db", type: "Opaque", keys: ["password"], key_count: 1,
  owner: { manager: null, detail: "" }, syncable: true, excluded: null, last_modified: null, version: "1", sync_status: "unmapped", extra: {}, mapping: null, ...over });
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

  it("maps every selected secret in one go and opens the comparison", async () => {
    responses = { "/secrets/config": config, "/secrets/mk4/scope": scope,
      "/secrets/mk4/inventory": { items: [row(), row({ id: "r2", name: "api" })], truncated: false, hidden: 0, scopes: [], types: ["Opaque"] },
      "POST /secrets/mk4/mappings/bulk": { created: [{ ...mapping, id: "m1" }, { ...mapping, id: "m2", secret_name: "api" }], skipped: [{ secret: "shop/x", why: "cannot be synced" }], plan_id: "p9" },
      "POST /secrets/mk4/schedules": { id: "s1" } };
    page();
    fireEvent.click(await screen.findByRole("button", { name: "Select all 2 unmapped" }));
    fireEvent.click(screen.getByRole("button", { name: "Map 2 selected" }));
    fireEvent.change(screen.getByLabelText("Keep them in sync"), { target: { value: "60" } });
    fireEvent.click(screen.getByRole("button", { name: "Map 2" }));
    expect(await screen.findByText(/waits for approval on the Schedules tab/)).toBeTruthy();
    expect(calls.find((c) => c.path.endsWith("/mappings/bulk"))?.body).toMatchObject({ inventory_ids: ["r1", "r2"], aws_connector_id: "a1", direction: "export", compare: true });
    expect(calls.find((c) => c.path.endsWith("/schedules") && c.method === "POST")?.body).toMatchObject({ mapping_ids: ["m1", "m2"], interval_minutes: 60 });
    expect(screen.getByText("cannot be synced", { exact: false })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Review the comparison" })).toBeTruthy();
  });

  it("filters by several secret types picked from the types found", async () => {
    responses = { "/secrets/config": config, "/secrets/mk4/scope": scope,
      "/secrets/mk4/inventory": { items: [row()], truncated: false, hidden: 0, scopes: [], types: ["Opaque", "kubernetes.io/tls"] } };
    page("/secrets?env=mk4&type=Opaque");
    await screen.findByText("Type: Opaque");
    await waitFor(() => expect(calls.some((c) => c.path.includes("&type=Opaque"))).toBe(true));
    fireEvent.change(screen.getByLabelText("Type"), { target: { value: "kubernetes.io/tls" } });
    await waitFor(() => expect(calls.some((c) => c.path.includes("&type=Opaque&type=kubernetes.io%2Ftls"))).toBe(true));
  });

  it("shows an approval for a pending schedule and what its last run left for a person", async () => {
    const sched = { id: "s1", name: "shop db", status: "pending", paused_reason: null, interval_minutes: 60, version: 2, schedule_hash: "e".repeat(64), requested_by: "op@example.com",
      namespaces: ["shop"], stale_mappings: [], mappings: [{ id: "m1", secret: "shop/db", aws_secret_name: "moks/x", direction: "both", last_status: "changed_both" }],
      next_run_at: null, last_run_at: "2026-11-07T00:00:00Z", last_plan_id: "p1",
      last_outcome: { status: "succeeded", written: 0, needs_review: [{ mapping_id: "m1", secret: "shop/db", aws: "moks/x", classification: "changed_both", why: "both sides changed" }] },
      approvals: [], allowed: { edit: false, approve: true, pause: false, resume: false, run: false, retire: true } };
    responses = { "/secrets/config": config, "/secrets/mk4/scope": scope, "/secrets/mk4/schedules": { items: [sched] }, "POST /secrets/mk4/schedules/s1/decision": { ...sched, status: "active" } };
    page("/secrets?env=mk4&tab=schedules");
    fireEvent.click(await screen.findByText("shop db"));
    expect(await screen.findByText("both sides changed", { exact: false })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(calls.find((c) => c.path.endsWith("/schedules/s1/decision"))?.body).toMatchObject({ approve: true, schedule_hash: "e".repeat(64) }));
  });

  it("opens values sealed to this tab and masks them until asked", async () => {
    const subtle = globalThis.crypto.subtle;
    let sealed: any;
    responses = { "/secrets/config": config, "/secrets/mk4/scope": scope, "/secrets/mk4/inventory": { items: [row()], truncated: false, hidden: 0, scopes: [], types: [] },
      "POST /secrets/mk4/reveal": async (body: any) => {  // what the executor does (seal.py), with WebCrypto
        const jobId = "00000000-0000-4000-8000-000000000001";
        const mine = await subtle.generateKey({ name: "ECDH", namedCurve: "P-256" }, true, ["deriveBits"]);
        const peer = await subtle.importKey("jwk", body.public_key, { name: "ECDH", namedCurve: "P-256" }, false, []);
        const bits = await subtle.deriveBits({ name: "ECDH", public: peer }, mine.privateKey, 256);
        const hk = await subtle.importKey("raw", bits, "HKDF", false, ["deriveKey"]);
        const salt = Uint8Array.from(jobId.replace(/-/g, "").match(/../g)!.map((h) => parseInt(h, 16)));
        const key = await subtle.deriveKey({ name: "HKDF", hash: "SHA-256", salt, info: new TextEncoder().encode("moks-secret-reveal-v1") }, hk, { name: "AES-GCM", length: 256 }, false, ["encrypt"]);
        const iv = new Uint8Array(12);
        const ct = new Uint8Array(await subtle.encrypt({ name: "AES-GCM", iv, additionalData: new TextEncoder().encode(jobId) }, key,
          new TextEncoder().encode(JSON.stringify({ side: "kubernetes", type: "Opaque", version: "7", keys: { password: { text: "hunter2" } } }))));
        const b64 = (a: Uint8Array) => btoa(String.fromCharCode(...a)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
        const epk = await subtle.exportKey("jwk", mine.publicKey);
        sealed = { alg: "x", epk: { kty: epk.kty, crv: epk.crv, x: epk.x, y: epk.y }, iv: b64(iv), ct: b64(ct) };
        return { job_id: jobId };
      },
      "GET /secrets/mk4/reveal/00000000-0000-4000-8000-000000000001": () => ({ status: "succeeded", sealed, key_count: 1 }) };
    page();
    fireEvent.click(await screen.findByText("shop/db"));
    fireEvent.click(await screen.findByRole("button", { name: "Show values" }));
    expect(await screen.findByText("••••••••", {}, { timeout: 4000 })).toBeTruthy();
    expect(screen.queryByText("hunter2")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Show" }));
    expect(screen.getByText("hunter2")).toBeTruthy();
    expect(calls.find((c) => c.path.endsWith("/reveal"))?.body.public_key).not.toHaveProperty("d");
  });

  it("proposes deleting a mapped secret from its details, never directly", async () => {
    responses = { "/secrets/config": config, "/secrets/mk4/scope": scope,
      "/secrets/mk4/inventory": { items: [row({ mapping: { id: "m1", other: "moks/x" }, sync_status: "in_sync" })], truncated: false, hidden: 0, scopes: [], types: [] },
      "POST /secrets/mk4/plans": { id: "p5" }, "/secrets/mk4/plans/p5": plan({ id: "p5", kind: "delete" }) };
    page();
    fireEvent.click(await screen.findByText("shop/db"));
    fireEvent.click(await screen.findByRole("button", { name: "Propose deletion" }));
    expect((screen.getByLabelText("Kubernetes") as HTMLInputElement).checked).toBe(true);
    expect((screen.getByLabelText("AWS Secrets Manager") as HTMLInputElement).checked).toBe(false);
    fireEvent.change(screen.getByLabelText("Reason"), { target: { value: "no longer used" } });
    fireEvent.click(screen.getByRole("button", { name: "Start comparison" }));
    await waitFor(() => expect(calls.find((c) => c.path.endsWith("/plans") && c.method === "POST")?.body).toMatchObject({ kind: "delete", mapping_ids: ["m1"], targets: ["kubernetes"] }));
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
