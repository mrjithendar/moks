import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

const state = vi.hoisted(() => ({ calls: [] as string[] }));
vi.mock("../api", async (orig) => ({ ...(await orig<any>()), api: vi.fn(async (path: string) => {
  state.calls.push(path);
  if (path === "/environments") return [{ name: "mk4", display_name: "mk4", kind: "minikube", is_production: false }];
  if (path === "/datastores/mk4") return { data: [
    { name: "shop-db", kind: "postgres", namespace: "shop", host: "shop-db-rw.shop.svc", port: 5432, reachable: true,
      findings: [{ severity: "bad", code: "pg_blocked:shop-db", text: "shop-db: 6 session(s) are waiting on locks held by other sessions." }] },
    { name: "cache", kind: "redis", mode: "sentinel", namespace: "cache", reachable: false, error: "unavailable: cannot reach redis" },
  ] };
  if (path === "/datastores/mk4/postgres/shop-db/overview") return { summary: "shop-db: primary", collected_at: "2026-11-12T10:00:00Z", data: {
    server: { role: "primary", version: "PostgreSQL 17.2 on x86_64" }, connections: { total: 40, max: 100, used_pct: 41.2, by_source: [{ application: "checkout", user: "app", database: "app", n: 30, idle_in_transaction: 4 }] },
    blocked_sessions: 6, cache_hit_ratio: 0.99, oldest_xid_age: 1200, idle_in_transaction: 4, findings: [] } };
  if (path === "/datastores/mk4/postgres/shop-db/locks") return { summary: "6 blocked", collected_at: "2026-11-12T10:00:00Z", data: {
    blocked: [], root_blockers: [{ blocker: { pid: 4242, application: "billing", state: "idle in transaction", xact_age_s: 900, query: "UPDATE accounts SET balance = ? WHERE id = ?" }, waiting: 6, waiting_pids: [1, 2, 3] }] } };
  return {};
}) }));
vi.mock("../auth", () => ({ useAuth: () => ({ can: () => true }) }));

import { FeedbackProvider } from "../ui";
import { DataStores, health, VIEWS } from "./DataStores";

afterEach(() => { cleanup(); state.calls.length = 0; });

describe("data-store health", () => {
  it("ranks unreachable and bad findings above warnings", () => {
    expect(health({ reachable: false })).toBe("bad");
    expect(health({ reachable: true, findings: [{ severity: "warn" }, { severity: "info" }] })).toBe("warn");
    expect(health({ reachable: true, findings: [] })).toBe("ok");
    expect(health({})).toBe("neutral");
    expect(VIEWS.postgres.map(([k]) => k)).toContain("locks");
    expect(VIEWS.redis.map(([k]) => k)).toEqual(["overview", "slowlog", "clients", "keyspace"]);
  });

  it("lists clusters with their findings and opens one view at a time", async () => {
    render(<MemoryRouter><FeedbackProvider><DataStores /></FeedbackProvider></MemoryRouter>);
    expect(await screen.findAllByText("shop-db")).not.toHaveLength(0);
    expect(screen.getAllByText(/waiting on locks/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/cannot reach redis/).length).toBeGreaterThan(0);
    fireEvent.click(screen.getAllByText("shop-db")[0]);
    await waitFor(() => expect(state.calls).toContain("/datastores/mk4/postgres/shop-db/overview"));
    expect(await screen.findByText(/40 of 100/)).toBeTruthy();
    fireEvent.click(screen.getByRole("tab", { name: "Locks" }));
    expect(await screen.findByText(/pid 4242 holds up 6/)).toBeTruthy();
  });
});
