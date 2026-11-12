import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { FeedbackProvider } from "../ui";
import { api } from "../api";
import { Backups } from "./Backups";

const database = { id: "1", namespace: "shop", workload: "orders-db", kind: "StatefulSet", engine: "postgresql", status: "success", trigger: "manual", started_at: "2026-10-31T00:00:00Z", finished_at: "2026-10-31T00:01:00Z", bytes: 2048, dump_bytes: 4096, sha256: "ab", image: "postgres:17", pod: "orders-db-0", object_key: "clusters/database-backups/x.dump.enc", error: null,
  inventory: { version: "17.11", table_count: 1, row_estimate: 12, not_included: ["legacy"], databases: [{ name: "shop", bytes: 9000, tables: [{ schema: "public", name: "orders", rows: 12, bytes: 8000 }] }] } };
const responses: Record<string, unknown> = {
  "/environments": [{ name: "mk4", display_name: "mk4" }],
  "/backups/mk4": { installed: true, volumes_supported: false, findings: [], locations: [], restores: [], schedules: [], store: null,
    backups: [{ name: "nightly", phase: "Completed", started: null, expires: null, items: 3, items_total: 3, volumes: false, errors: 0, warnings: 0 }] },
  "/backups/mk4/databases": { targets: [{ namespace: "shop", workload: "orders-db", kind: "StatefulSet", engine: "postgresql", enabled: true }], not_covered: [{ namespace: "shop", workload: "cache", service: "redis" }], backups: [database], schedules: [], findings: [{ severity: "warn", code: "x", text: "orders-db has no active backup schedule." }] },
  "/backups/mk4/postgres": { installed: true, truncated: false, environment: "mk4",
    clusters: [{ namespace: "postgres-db", name: "postgres-cluster", phase: "Cluster in healthy state", healthy: true, instances: 3, ready: 3, primary: "postgres-cluster-1", image: "postgresql:17.6", storage: "20Gi",
      backup_method: "barmanObjectStore", destination: "s3://backups/postgres", retention: "30d", archiving: false, archiving_message: "exit status 1", first_recoverability_point: "2026-11-01T00:00:00Z", last_successful_backup: "2026-11-12T02:01:30Z", last_failed_backup: null }],
    backups: [{ namespace: "postgres-db", name: "daily-20261112", cluster: "postgres-cluster", phase: "completed", method: "barmanObjectStore", started: "2026-11-12T02:00:00Z", stopped: "2026-11-12T02:01:30Z", duration_seconds: 90, schedule: "daily", error: null }],
    schedules: [{ namespace: "postgres-db", name: "daily", cluster: "postgres-cluster", cron: "0 0 2 * * *", suspended: false, last_run: "2026-11-12T02:00:00Z", next_run: "2026-11-13T02:00:00Z" }],
    findings: [{ severity: "bad", code: "cnpg_archiving:postgres-db/postgres-cluster", text: "WAL archiving is failing for postgres-db/postgres-cluster." }] },
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
  it("takes a database backup at once, without asking for a reason", async () => {
    open();
    await screen.findByRole("button", { name: "shop/orders-db" });
    const database = screen.getAllByRole("button", { name: "Back up now" }).find((b) => b.classList.contains("ghost"))!;  // the cluster's is not ghost
    fireEvent.click(database);
    await waitFor(() => expect(api).toHaveBeenCalledWith("/backups/mk4/databases/backup", { method: "POST", body: { namespace: "shop", workload: "orders-db", kind: "StatefulSet" } }));
    expect(screen.queryByLabelText("Reason")).toBeNull();
  });
  it("reports PostgreSQL clusters run by CloudNativePG, read-only", async () => {
    open();
    expect(await screen.findByText(/WAL archiving is failing for postgres-db\/postgres-cluster/)).toBeTruthy();
    expect(screen.getByRole("group", { name: "Protection summary" }).textContent).toContain("0 of 1");
    fireEvent.click(screen.getByRole("button", { name: "postgres-db/postgres-cluster" }));
    expect(await screen.findByText("s3://backups/postgres")).toBeTruthy();
    expect(screen.getByText("exit status 1")).toBeTruthy();
    expect(screen.getByText(/does not start, change or restore them/)).toBeTruthy();
  });
  it("still asks why before a restore", async () => {
    open();
    fireEvent.click(await screen.findByRole("button", { name: "Restore" }));
    expect(await screen.findByLabelText("Reason")).toBeTruthy();
  });
});
