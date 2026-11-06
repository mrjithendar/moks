/** Rightsizing, read from the backend's per-resource recommendations (finops.rightsize). The table, the CSV and a
 *  proposal all come from these functions, so they show the same fields and the same values. A recommendation's
 *  `changes` are the only fields that change; its `patch` is exactly those fields. */

export type Field = "cpu_request" | "cpu_limit" | "mem_request" | "mem_limit";
export const FIELDS: { key: Field; label: string; path: string }[] = [
  { key: "cpu_request", label: "CPU request", path: "resources.requests.cpu" },
  { key: "cpu_limit", label: "CPU limit", path: "resources.limits.cpu" },
  { key: "mem_request", label: "Memory request", path: "resources.requests.memory" },
  { key: "mem_limit", label: "Memory limit", path: "resources.limits.memory" },
];

export interface Change { field: Field; current: string | null; recommended: string; direction: "increase" | "decrease" | "set"; reason: string }
export interface Rec {
  resource: "cpu" | "memory"; action: "increase" | "reduce" | "set_requests"; changes: Change[]; monthly_saving: number;
  capacity: { cpu_cores?: number; memory_bytes?: number }; patch: { requests: Record<string, string>; limits: Record<string, string> };
}
export interface Row {
  environment: string; namespace: string; workload: string; container: string; replicas: number; confidence: "high" | "low"; coverage: number;
  current: Record<Field, string | null>; recommended: Record<Field, string | null>; observed: { cpu_p95: number; mem_max: string };
  findings: string[]; recommendations?: Rec[]; action: string; monthly_saving: number;
}
export interface Pricing { kind: "fixed" | "per_unit"; description: string; note?: string }

export const ACTION_LABEL: Record<Rec["action"], string> = { increase: "Increase", reduce: "Reduce", set_requests: "Set request" };
export const RESOURCE_LABEL: Record<Rec["resource"], string> = { cpu: "CPU", memory: "Memory" };

/** Rows from an older backend have no per-resource entries: show them as one, labelled with what they say. */
export function recsOf(r: Row): Rec[] {
  if (r.recommendations) return r.recommendations;
  const changes = FIELDS.filter((f) => r.recommended[f.key] && r.recommended[f.key] !== r.current[f.key])
    .map((f) => ({ field: f.key, current: r.current[f.key] ?? null, recommended: r.recommended[f.key] as string, direction: "set" as const, reason: r.findings.join("; ") }));
  return changes.length ? [{ resource: "memory", action: r.action as Rec["action"], changes, monthly_saving: r.monthly_saving, capacity: {}, patch: { requests: {}, limits: {} } }] : [];
}

export function changed(rec: Rec, field: Field): Change | undefined {
  return rec.changes.find((c) => c.field === field);
}

export function fmtCores(c: number): string {
  const m = Math.round(Math.abs(c) * 1000);
  return m >= 1000 ? `${(m / 1000).toFixed(2).replace(/\.?0+$/, "")} cores` : `${m}m`;
}

export function fmtBytesMi(b: number): string {
  const mi = Math.abs(b) / 1024 ** 2;
  return mi >= 1024 ? `${(mi / 1024).toFixed(2).replace(/\.?0+$/, "")} GiB` : `${Math.round(mi)} MiB`;
}

/** What a change does to reserved capacity across all replicas: "frees 450m CPU" or "reserves 70 MiB memory". */
export function capacityText(rec: Rec): string {
  const v = rec.resource === "cpu" ? rec.capacity.cpu_cores ?? 0 : rec.capacity.memory_bytes ?? 0;
  if (!v) return "no change in reserved capacity";
  const amount = rec.resource === "cpu" ? `${fmtCores(v)} CPU` : `${fmtBytesMi(v)} memory`;
  return `${v > 0 ? "frees" : "reserves"} ${amount}`;
}

/** Money only where prices are per unit, and then always an estimate. Under a fixed price, null: capacity, not money. */
export function savingText(rec: Rec, pricing?: Pricing | null): string | null {
  if (pricing?.kind !== "per_unit") return null;
  const v = rec.monthly_saving;
  return `${v >= 0 ? "saves" : "costs"} ${Math.abs(v).toFixed(2)} per month`;
}

/** Why Propose cannot be used for this recommendation, or null. The backend checks all of this again. */
export function proposeBlocker(r: Row, can: (p: string) => boolean): string | null {
  if (!can("action.request")) return "You need a role with action.request in this environment to propose changes.";
  const kind = r.workload.split("/")[0];
  if (kind !== "Deployment") return `MOKS can change resources on Deployments only. ${kind} ${r.workload.split("/").slice(1).join("/")} needs a change to its manifest instead.`;
  if (r.confidence === "low") return `Only ${Math.round(r.coverage * 100)}% of the window has usage data. Wait for more before changing requests.`;
  return null;
}

/** The plan Propose creates: patch_resources with exactly the changed fields, reviewed in Approvals before anything runs. */
export function resizePlan(r: Row, rec: Rec, days: number) {
  return {
    action: "patch_resources", target_kind: "Deployment", target_name: r.workload.split("/").slice(1).join("/"),
    params: { container: r.container, requests: rec.patch.requests, limits: rec.patch.limits },
    reason: `Rightsizing ${RESOURCE_LABEL[rec.resource].toLowerCase()} from ${days}-day usage (p95 CPU ${r.observed.cpu_p95}, max memory ${r.observed.mem_max}). `
      + rec.changes.map((c) => `${FIELDS.find((f) => f.key === c.field)!.label} ${c.current ?? "unset"} → ${c.recommended}: ${c.reason}`).join(". "),
  };
}

/** The exact diff of the container spec, one line per changed field. */
export function diffLines(r: Row, rec: Rec): { path: string; from: string; to: string }[] {
  return rec.changes.map((c) => ({ path: `containers[${r.container}].${FIELDS.find((f) => f.key === c.field)!.path}`, from: c.current ?? "(unset)", to: c.recommended }));
}

/** One CSV row per recommendation, with all four values: unchanged ones repeat the current value. */
export const CSV_COLUMNS: [string, (x: { r: Row; rec: Rec; pricing?: Pricing | null }) => unknown][] = [
  ["Environment", ({ r }) => r.environment], ["Namespace", ({ r }) => r.namespace], ["Workload", ({ r }) => r.workload], ["Container", ({ r }) => r.container],
  ["Resource", ({ rec }) => RESOURCE_LABEL[rec.resource]], ["Action", ({ rec }) => ACTION_LABEL[rec.action]],
  ...FIELDS.flatMap((f) => [
    [`${f.label} now`, ({ r }: { r: Row }) => r.current[f.key] ?? ""],
    [`${f.label} recommended`, ({ r, rec }: { r: Row; rec: Rec }) => changed(rec, f.key)?.recommended ?? r.current[f.key] ?? ""],
  ] as [string, (x: { r: Row; rec: Rec }) => unknown][]),
  ["Changed fields", ({ rec }) => rec.changes.map((c) => FIELDS.find((f) => f.key === c.field)!.label).join("; ")],
  ["Reasons", ({ rec }) => rec.changes.map((c) => c.reason).join("; ")],
  ["Capacity", ({ rec }) => capacityText(rec)],
  ["Estimated saving per month", ({ rec, pricing }) => (pricing?.kind === "per_unit" ? rec.monthly_saving : "")],
  ["Pricing", ({ pricing }) => (pricing?.kind === "per_unit" ? "Estimate" : "Fixed price: capacity only")],
  ["Confidence", ({ r }) => r.confidence], ["Data coverage", ({ r }) => r.coverage],
];
