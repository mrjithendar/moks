import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { FeedbackProvider } from "../ui";
import { Backups } from "./Backups";

const database = { id: "1", namespace: "shop", workload: "orders-db", kind: "StatefulSet", engine: "postgresql", status: "success", trigger: "manual", started_at: "2026-10-31T00:00:00Z", finished_at: "2026-10-31T00:01:00Z", bytes: 2048, dump_bytes: 4096, sha256: "ab", image: "postgres:17", pod: "orders-db-0", object_key: "clusters/database-backups/x.dump.enc", error: null,
  inventory: { version: "17.11", table_count: 1, row_estimate: 12, not_included: ["legacy"], databases: [{ name: "shop", bytes: 9000, tables: [{ schema: "public", name: "orders", rows: 12, bytes: 8000 }] }] } };
const responses: Record<string, unknown> = {
  "/environments": [{ name: "mk4", display_name: "mk4" }],
  "/backups/mk4": { installed: true, volumes_supported: false, findings: [], locations: [], restores: [], schedules: [], store: null,
    backups: [{ name: "nightly", phase: "Completed", started: null, expires: null, items: 3, items_total: 3, volumes: false, errors: 0, warnings: 0 }] },
  "/backups/mk4/databases": { targets: [{ namespace: "shop", workload: "orders-db", kind: "StatefulSet", engine: "postgresql", enabled: true }], not_covered: [{ namespace: "shop", workload: "cache", service: "redis" }], backups: [database], schedules: [], findings: [{ severity: "warn", code: "x", text: "orders-db has no active backup schedule." }] },
  "/backups/mk4/items/nightly": { name: "nightly", phase: "Completed", errors: 0, warnings: 0, namespaces: ["*"], excluded_namespaces: ["velero"], included_resources: ["*"], excluded_resources: [], duration_seconds: 15, ttl: "87600h", uid: "u1", storage_location: "moks-r2", format_version: "1.1.0", fs_backups: { completed: 0, attempted: 0 },
    contents: { total: 2, kinds: [{ kind: "Pod", count: 2 }], namespaces: [{ namespace: "shop", count: 2, kinds: { Pod: 2 } }], items: [{ kind: "Pod", namespace: "shop", name: "checkout" }], truncated: false, errors: {}, warnings: {} }, contents_note: null },
};

vi.mock("../api", async (real) => ({ ...(await real<object>()), api: vi.fn(async (path: string) => responses[path]) }));
vi.mock("../auth", () => ({ useAuth: () => ({ can: () => true }) }));

function open() { render(<MemoryRouter><FeedbackProvider><Backups /></FeedbackProvider></MemoryRouter>); }

describe("Backups", () => {
  afterEach(cleanup);
  it("shows what is inside a cluster backup", async () => {
    open();
    fireEvent.click(await screen.findByRole("button", { name: "nightly" }));
    expect(await screen.findByText("What is inside (2 objects)")).toBeTruthy();
    expect(screen.getByText("15 s")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Find an object"), { target: { value: "check" } });
    expect(screen.getByText("Pod shop/checkout")).toBeTruthy();
  });
  it("lists databases, gaps and what a dump holds", async () => {
    open();
    fireEvent.click(await screen.findByRole("button", { name: "shop/orders-db" }));
    expect(await screen.findByText("orders")).toBeTruthy();
    expect(screen.getByText(/Not in this backup: legacy/)).toBeTruthy();
    expect(screen.getByText(/shop\/cache \(redis\)/)).toBeTruthy();
    expect(screen.getByText(/no active backup schedule/)).toBeTruthy();
  });
});
