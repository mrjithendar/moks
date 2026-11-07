import { Fragment, useEffect, useMemo, useRef, useState, type FormEvent, type ReactNode } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { api, ApiError, qs } from "../api";
import { Badge, DataTable, ErrorBox, Loading, PageHead, Time, useInterval, useLoad, type Col } from "../components";
import { FilterPanel, useUrlFilters } from "../filters";
import { StateView } from "../states";
import { newKeyPair, openSealed, type Revealed } from "../secretReveal";
import { Dialog, Drawer, Empty, Identifier, useFeedback } from "../ui";

/** Kubernetes <-> AWS Secrets Manager: inventories, mappings, reviewed plans, sync schedules and history. The API
 *  returns names, versions, key names (with secrets.compare) and masked fingerprints. Values appear only in the
 *  details drawer, for people with secrets.reveal: sealed by the executor to a key made in this tab (secretReveal.ts). */

type Tone = "ok" | "warn" | "bad" | "info" | "neutral";
const STATUS_TONE: Record<string, Tone> = {
  in_sync: "ok", changed_kubernetes: "warn", changed_aws: "warn", changed_both: "bad", missing_kubernetes: "warn", missing_aws: "warn",
  unsupported: "neutral", failed: "bad", unmapped: "neutral", not_compared: "info", excluded: "neutral",
};
const STATUS_LABEL: Record<string, string> = {
  in_sync: "In sync", changed_kubernetes: "Changed in Kubernetes", changed_aws: "Changed in AWS", changed_both: "Changed on both sides",
  missing_kubernetes: "Missing in Kubernetes", missing_aws: "Missing in AWS", unsupported: "Unmanaged or unsupported", failed: "Denied or failed",
  unmapped: "Not mapped", not_compared: "Not compared yet", excluded: "Excluded",
};
const PLAN_TONE: Record<string, Tone> = { succeeded: "ok", partial: "warn", failed: "bad", stale: "bad", compare_failed: "bad", rejected: "bad", cancelled: "neutral",
  expired: "neutral", comparing: "info", draft: "info", pending: "warn", approved: "info", queued: "info", executing: "info" };
const ITEM_TONE: Record<string, Tone> = { succeeded: "ok", pending: "info", running: "info", failed: "bad", stale: "bad", conflict: "warn", skipped: "neutral" };
const BUSY = ["comparing", "queued", "executing"];

interface Config { enabled: boolean; environments: { name: string; display_name: string; is_production: boolean; aws_connectors: number }[] }
interface ScopeInfo {
  kubernetes: { id: string; name: string; writable: boolean; cluster_uid: string | null; namespaces: string[]; error: string | null }[];
  aws: { id: string; name: string; account_id?: string; region?: string; allowed_prefixes?: string[]; kubernetes_namespaces?: string[]; allow_delete?: boolean; error?: string }[];
  cluster_wide: boolean; can: Record<string, boolean>; discovery?: { interval_minutes: number; last_at: string | null };
  policy?: { mode: "manual" | "scheduled"; schedules_run: boolean; explanation: string };
}

/** Under the manual-only policy (the default) every write is a plan someone reviews; schedules never run. */
function canSchedule(scope: ScopeInfo): boolean {
  return scope.policy?.schedules_run ?? false;
}

function every(minutes: number): string {
  return minutes % 1440 === 0 ? `${minutes / 1440} d` : minutes % 60 === 0 ? `${minutes / 60} h` : `${minutes} min`;
}

function Status({ s }: { s: string }) {
  return <Badge kind={STATUS_TONE[s] ?? "neutral"}>{STATUS_LABEL[s] ?? s}</Badge>;
}

function denied(error: unknown) {
  return error instanceof ApiError && error.status === 403;
}

export function Secrets() {
  const [f, setF] = useUrlFilters({ env: "", tab: "inventory" });
  const config = useLoad(() => api<Config>("/secrets/config"), []);
  const envs = config.data?.environments ?? [];
  const env = f.env || envs[0]?.name || "";
  const scope = useLoad(() => (env ? api<ScopeInfo>(`/secrets/${env}/scope`) : Promise.resolve(undefined)), [env]);
  if (config.data && !config.data.enabled) {
    return <><PageHead title="Secrets sync" /><StateView kind="unavailable" title="Secret sync is off" why="This MOKS has MOKS_SECRETS_SYNC_ENABLED unset." next="An operator enables secretsSync in the Helm values (docs/SECRETS_SYNC.md)." /></>;
  }
  const tabs: [string, string][] = [["inventory", "Inventory"], ["mappings", "Mappings"], ["schedules", "Schedules"], ["plans", "Plans"], ["history", "History"]];
  return (
    <>
      <PageHead title="Secrets sync" sub="Discover, compare and sync Kubernetes Secrets with AWS Secrets Manager. Every write is approved: once per plan, or once per schedule, which then syncs one-sided changes on its own.">
        {envs.length > 0 && <label className="inline">Environment
          <select value={env} onChange={(e) => setF({ env: e.target.value })}>{envs.map((e) => <option key={e.name} value={e.name}>{e.display_name}{e.is_production ? " (production)" : ""}</option>)}</select>
        </label>}
      </PageHead>
      <Loading load={config} />
      {config.data && !envs.length && <StateView kind="denied" why="No environment grants you secrets.discover." next="Ask an administrator for the secrets_viewer, secrets_operator or secrets_approver role." />}
      {env && <>
        <div className="tabs" role="tablist" aria-label="Secrets views">
          {tabs.map(([k, label]) => <button key={k} type="button" role="tab" aria-selected={f.tab === k} className={f.tab === k ? "active" : undefined} onClick={() => setF({ tab: k })}>{label}</button>)}
        </div>
        {scope.error ? <ErrorBox error={scope.error} onRetry={scope.reload} /> : null}
        {scope.data?.discovery && <p className="small muted">Last discovered <Time iso={scope.data.discovery.last_at} />
          {scope.data.discovery.interval_minutes > 0 ? ` · the executor lists every synced namespace every ${every(scope.data.discovery.interval_minutes)} on its own` : " · discovery runs only when someone asks"}</p>}
        {scope.data && !scope.data.aws.length && <StateView kind="empty" title="No AWS Secrets Manager connector" why={`${env} has no aws_secrets connector.`} next={<>An administrator adds one on <Link to="/admin/connectors">Connectors</Link>.</>} />}
        {scope.data && f.tab === "inventory" && <Inventory env={env} scope={scope.data} />}
        {scope.data && f.tab === "mappings" && <Mappings env={env} scope={scope.data} onScheduled={() => setF({ tab: "schedules" })} />}
        {scope.data && f.tab === "schedules" && <Schedules env={env} scope={scope.data} />}
        {f.tab === "plans" && <Plans env={env} />}
        {f.tab === "history" && <History env={env} />}
      </>}
    </>
  );
}

// ---- inventory ---------------------------------------------------------------------------------------------

interface Row { id: string; side: string; connector_id: string; namespace: string | null; name: string; arn: string | null; type: string | null; keys: string[] | null; key_count: number | null;
  owner: { manager: string | null; detail: string }; syncable: boolean; excluded: string | null; last_modified: string | null; version: string | null; sync_status: string;
  extra: Record<string, any>; mapping: { id: string; other: string } | null }

function Inventory({ env, scope }: { env: string; scope: ScopeInfo }) {
  const [f, setF, clear] = useUrlFilters({ side: "kubernetes", q: "", ns: "", type: "", owner: "", status: "", prefix: "" });
  const types = f.type ? f.type.split(",") : [];
  const typeQuery = types.map((t) => `&type=${encodeURIComponent(t)}`).join("");
  const inv = useLoad(() => api<{ items: Row[]; truncated: boolean; hidden: number; types?: string[]; scopes: { scope: string; complete: boolean; count: number; error: string | null; discovered_at: string | null }[] }>(
    `/secrets/${env}/inventory${qs({ side: f.side, q: f.q, namespace: f.ns, owner: f.owner, status: f.status, prefix: f.prefix }) || "?"}${typeQuery}`), [env, f.side, f.q, f.ns, f.type, f.owner, f.status, f.prefix]);
  const setTypes = (next: string[]) => setF({ type: next.join(",") });
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [discovering, setDiscovering] = useState(false);
  const [proposing, setProposing] = useState(false);
  const [reviewing, setReviewing] = useState(false);
  const [open, setOpen] = useState<Row>();
  useEffect(() => setSelected(new Set()), [env, f.side]);
  const rows = inv.data?.items ?? [];
  const namespaces = [...new Set(scope.kubernetes.flatMap((k) => k.namespaces))].sort();
  const toggle = (id: string) => setSelected((s) => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n; });
  const selectable = rows.filter((r) => !r.excluded && !r.mapping);
  const allPicked = selectable.length > 0 && selectable.every((r) => selected.has(r.id));
  const counts = rows.reduce<Record<string, number>>((c, r) => ({ ...c, [r.sync_status]: (c[r.sync_status] ?? 0) + 1 }), {});
  const cols: Col<Row>[] = [
    { key: "pick", head: "", cell: (r) => <input type="checkbox" aria-label={`Select ${r.name}`} disabled={!!r.excluded || !!r.mapping} checked={selected.has(r.id)} onClick={(e) => e.stopPropagation()} onChange={() => toggle(r.id)} /> },
    { key: "name", head: "Name", primary: true, cell: (r) => <Identifier name={f.side === "kubernetes" ? `${r.namespace}/${r.name}` : r.name} what="secret name" /> },
    { key: "status", head: "Sync status", status: true, cell: (r) => <Status s={r.sync_status} /> },
    { key: "type", head: "Type", cell: (r) => r.type ?? "—" },
    { key: "keys", head: "Keys", cell: (r) => r.keys ? <span className="small mono" title={r.keys.join(", ")}>{r.keys.length ? r.keys.join(", ") : "none"}</span> : r.key_count != null ? `${r.key_count} keys` : <span className="muted small">after compare</span> },
    { key: "owner", head: "Owner", cell: (r) => r.excluded ? <span className="small muted">{r.excluded}</span> : r.owner?.manager ? <Badge kind={r.owner.manager === "moks" ? "info" : "warn"}>{r.owner.manager}</Badge> : <span className="muted">—</span> },
    { key: "modified", head: "Last modified", cell: (r) => <Time iso={r.last_modified} /> },
    { key: "mapping", head: "Mapped to", wide: true, cell: (r) => r.mapping ? <span className="small mono">{r.mapping.other}</span> : "—" },
  ];
  const chips = [f.q && { key: "q", label: `Name: ${f.q}`, onRemove: () => setF({ q: "" }) }, f.ns && { key: "ns", label: `Namespace: ${f.ns}`, onRemove: () => setF({ ns: "" }) },
    ...types.map((t) => ({ key: `type-${t}`, label: `Type: ${t}`, onRemove: () => setTypes(types.filter((x) => x !== t)) })), f.owner && { key: "owner", label: `Owner: ${f.owner}`, onRemove: () => setF({ owner: "" }) },
    f.status && { key: "status", label: `Status: ${STATUS_LABEL[f.status] ?? f.status}`, onRemove: () => setF({ status: "" }) },
    f.prefix && { key: "prefix", label: `Prefix: ${f.prefix}`, onRemove: () => setF({ prefix: "" }) }].filter(Boolean) as { key: string; label: string; onRemove: () => void }[];
  const partial = (inv.data?.scopes ?? []).filter((s) => !s.complete);
  return (
    <section className="stack">
      <FilterPanel chips={chips} onClear={clear}
        search={<input type="search" aria-label="Search secret names" placeholder="Search names…" defaultValue={f.q} onKeyDown={(e) => { if (e.key === "Enter") setF({ q: (e.target as HTMLInputElement).value }); }} />}
        actions={<>
          {scope.can.discover && <button type="button" className="ghost" onClick={() => setDiscovering(true)}>Discover</button>}
          {scope.can.map && selectable.length > 0 && <button type="button" className="ghost" onClick={() => setSelected(allPicked ? new Set() : new Set(selectable.map((r) => r.id)))}>
            {allPicked ? "Clear selection" : `Select all ${selectable.length} unmapped`}</button>}
          {scope.can.map && selected.size > 0 && <button type="button" onClick={() => setProposing(true)}>Map {selected.size} selected</button>}
        </>}
        advanced={<div className="row wrap">
          <label>Type<select value="" onChange={(e) => e.target.value && setTypes([...types, e.target.value])}>
            <option value="">{types.length ? "Add another type…" : "Any type"}</option>
            {(inv.data?.types ?? []).filter((t) => !types.includes(t)).map((t) => <option key={t} value={t}>{t}</option>)}</select></label>
          <label>Owner<select value={f.owner} onChange={(e) => setF({ owner: e.target.value })}><option value="">Any</option><option value="none">None or MOKS</option>
            {["external-secrets", "secrets-store-csi", "cert-manager", "helm", "argocd", "flux", "sealed-secrets", "aws-rotation"].map((o) => <option key={o}>{o}</option>)}</select></label>
          <label>Status<select value={f.status} onChange={(e) => setF({ status: e.target.value })}><option value="">Any</option>{Object.entries(STATUS_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select></label>
          {f.side === "aws" && <label>Name prefix<input value={f.prefix} onChange={(e) => setF({ prefix: e.target.value })} placeholder="moks/" /></label>}
        </div>}>
        <label>Source<select value={f.side} onChange={(e) => setF({ side: e.target.value })}><option value="kubernetes">Kubernetes</option><option value="aws">AWS Secrets Manager</option></select></label>
        {f.side === "kubernetes" && <label>Namespace<select value={f.ns} onChange={(e) => setF({ ns: e.target.value })}><option value="">All you can see</option>{namespaces.map((n) => <option key={n}>{n}</option>)}</select></label>}
      </FilterPanel>
      {inv.error && denied(inv.error) ? <StateView kind="denied" why={(inv.error as Error).message} /> : <Loading load={inv} />}
      {rows.length > 0 && <div className="row wrap" role="group" aria-label="Sync status summary">
        {Object.entries(counts).sort((a, b) => b[1] - a[1]).map(([k, n]) => (
          <button key={k} type="button" className={`ghost small${f.status === k ? " active" : ""}`} aria-pressed={f.status === k} onClick={() => setF({ status: f.status === k ? "" : k })}>
            <Badge kind={STATUS_TONE[k] ?? "neutral"}>{n}</Badge> {STATUS_LABEL[k] ?? k}</button>))}
      </div>}
      {partial.length > 0 && <StateView kind="partial" why="The last discovery could not read everything; rows it did not reach are from earlier runs."
        next="Fix the permission or retry Discover."><ul className="small state-list">{partial.map((s) => <li key={s.scope}><span className="mono">{s.scope}</span>: {s.error ?? "incomplete"}</li>)}</ul></StateView>}
      {(inv.data?.hidden ?? 0) > 0 && <p className="small muted">{inv.data!.hidden} secrets are outside your namespaces and not listed.</p>}
      {inv.data?.truncated && <p className="small muted">Showing the first {rows.length}. Filter to narrow the list.</p>}
      {inv.data && <DataTable label={f.side === "kubernetes" ? "Kubernetes Secrets" : "AWS secrets"} rows={rows} cols={cols} rowKey={(r) => r.id} onRow={setOpen}
        empty={<Empty title={inv.data.scopes.length ? "No secrets match." : "Nothing discovered yet."}>{!inv.data.scopes.length && "Use Discover to list secret names. Discovery reads metadata and never writes."}</Empty>} />}
      {discovering && <DiscoverDialog env={env} scope={scope} onClose={() => setDiscovering(false)} onDone={() => { setDiscovering(false); inv.reload(); }} />}
      {proposing && <BulkMap env={env} scope={scope} side={f.side} ids={[...selected]} onClose={() => setProposing(false)} onReview={() => { setProposing(false); setReviewing(true); }}
        onDone={() => { setProposing(false); setSelected(new Set()); inv.reload(); }} />}
      {reviewing && <MappingEditor env={env} scope={scope} ids={[...selected]} onClose={() => setReviewing(false)} onDone={() => { setReviewing(false); setSelected(new Set()); inv.reload(); }} />}
      {open && <SecretDetails env={env} scope={scope} row={open} onClose={() => setOpen(undefined)} />}
    </section>
  );
}

function DiscoverDialog({ env, scope, onClose, onDone }: { env: string; scope: ScopeInfo; onClose: () => void; onDone: () => void }) {
  const [kube, setKube] = useState(scope.kubernetes[0]?.id ?? "");
  const cluster = scope.kubernetes.find((k) => k.id === kube);
  // Every secretsSync namespace starts ticked; the executor also lists them on its own (secretsSync.discoveryIntervalMinutes).
  const [ns, setNs] = useState<string[]>(cluster?.namespaces ?? []);
  const [aws, setAws] = useState(scope.cluster_wide ? scope.aws[0]?.id ?? "" : "");
  const [job, setJob] = useState<string>();
  const [status, setStatus] = useState<any>();
  const [error, setError] = useState<unknown>();
  useInterval(async () => {
    if (!job) return;
    try {
      const s = await api<any>(`/secrets/${env}/discover/${job}`);
      setStatus(s);
      if (s.status === "succeeded") onDone();
    } catch (e) { setError(e); }
  }, 1500, !!job && status?.status !== "succeeded" && status?.status !== "failed");
  async function start(e: FormEvent) {
    e.preventDefault();
    setError(undefined);
    try {
      const r = await api<{ job_id: string }>(`/secrets/${env}/discover`, { method: "POST", body: { ...(ns.length ? { kube_connector_id: kube, namespaces: ns } : {}), ...(aws ? { aws_connector_id: aws } : {}) } });
      setJob(r.job_id);
    } catch (err) { setError(err); }
  }
  return (
    <Dialog title="Discover secrets" onClose={onClose}>
      <form className="stack" onSubmit={start}>
        <p className="small muted">Lists names, types, key names and owners. Values pass through the executor's memory and are not kept. Nothing is written.</p>
        {scope.kubernetes.length > 0 && <label>Cluster<select value={kube} onChange={(e) => { setKube(e.target.value); setNs(scope.kubernetes.find((k) => k.id === e.target.value)?.namespaces ?? []); }}>{scope.kubernetes.map((k) => <option key={k.id} value={k.id} disabled={!k.writable}>{k.name}{k.writable ? "" : " (no executor target)"}</option>)}</select></label>}
        {cluster?.error && <div className="alert warn small">{cluster.error}</div>}
        <fieldset><legend>Namespaces</legend>
          {cluster?.namespaces.length ? cluster.namespaces.map((n) => <label key={n} className="check"><input type="checkbox" checked={ns.includes(n)} onChange={() => setNs(ns.includes(n) ? ns.filter((x) => x !== n) : [...ns, n])} /> {n}</label>)
            : <span className="small muted">No namespace is both in your scope and synced by an AWS connector.</span>}
        </fieldset>
        <label>AWS Secrets Manager<select value={aws} onChange={(e) => setAws(e.target.value)} disabled={!scope.cluster_wide}>
          <option value="">Skip</option>{scope.aws.map((a) => <option key={a.id} value={a.id}>{a.name} ({a.account_id} {a.region})</option>)}</select></label>
        {!scope.cluster_wide && <span className="small muted">Listing AWS needs secrets.discover for every namespace.</span>}
        <ErrorBox error={error} />
        {status && <div className="small">Discovery {status.status}{status.result?.scopes && <ul>{status.result.scopes.map((s: any) => <li key={s.side + s.scope}>{s.side} {s.scope}: {s.count} {s.complete ? "" : `(partial: ${s.error})`}</li>)}</ul>}</div>}
        <div className="row end"><button disabled={!!job || (!ns.length && !aws)}>{job ? "Discovering…" : "Discover"}</button></div>
      </form>
    </Dialog>
  );
}

// ---- mappings ----------------------------------------------------------------------------------------------

interface Draft { inventory_id: string; kube_connector_id?: string; namespace: string; secret_name: string; aws_connector_id: string; aws_secret_name: string; payload_format: string;
  k8s_type: string; direction: string; expected_owner: Record<string, string>; management_policy: string; excluded: string | null; owner: { manager: string | null } | null;
  collision: boolean; aws_exists: boolean; allowed: boolean; key_mapping?: Record<string, any>; result?: string; error?: string }

function MappingEditor({ env, scope, ids, onClose, onDone }: { env: string; scope: ScopeInfo; ids: string[]; onClose: () => void; onDone: () => void }) {
  const [aws, setAws] = useState(scope.aws[0]?.id ?? "");
  const [drafts, setDrafts] = useState<Draft[]>();
  const [error, setError] = useState<unknown>();
  const [saving, setSaving] = useState(false);
  useEffect(() => {
    if (!aws) return;
    api<{ items: Draft[] }>(`/secrets/${env}/mappings/propose`, { method: "POST", body: { inventory_ids: ids, aws_connector_id: aws } }).then((r) => setDrafts(r.items), setError);
  }, [env, aws, ids.join(",")]);
  const edit = (i: number, patch: Partial<Draft>) => setDrafts((d) => d!.map((x, j) => (j === i ? { ...x, ...patch } : x)));
  async function save() {
    setSaving(true);
    const out = [...(drafts ?? [])];
    for (let i = 0; i < out.length; i++) {
      const d = out[i];
      if (d.excluded || d.result === "created") continue;
      try {
        await api(`/secrets/${env}/mappings`, { method: "POST", body: { kube_connector_id: d.kube_connector_id ?? scope.kubernetes[0]?.id, namespace: d.namespace, secret_name: d.secret_name,
          aws_connector_id: d.aws_connector_id, aws_secret_name: d.aws_secret_name, payload_format: d.payload_format, key_mapping: d.key_mapping ?? {}, k8s_type: d.k8s_type,
          direction: d.direction, expected_owner: d.expected_owner, management_policy: d.management_policy } });
        out[i] = { ...d, result: "created", error: undefined };
      } catch (e) { out[i] = { ...d, result: "failed", error: e instanceof Error ? e.message : String(e) }; }
      setDrafts([...out]);
    }
    setSaving(false);
    if (out.every((d) => d.result === "created" || d.excluded)) onDone();
  }
  return (
    <Dialog title="Review proposed mappings" onClose={onClose} wide>
      <div className="stack">
        <p className="small muted">Names include the cluster and namespace, so the same Secret name elsewhere never collides. Edit before saving: nothing is created in AWS until a reviewed plan runs.</p>
        <label>AWS connector<select value={aws} onChange={(e) => setAws(e.target.value)}>{scope.aws.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}</select></label>
        <ErrorBox error={error} />
        {!drafts && !error && <p className="small">Preparing…</p>}
        {drafts?.map((d, i) => (
          <fieldset key={d.inventory_id} className="card stack">
            <legend className="mono">{d.namespace ? `${d.namespace}/${d.secret_name}` : d.aws_secret_name}</legend>
            {d.excluded && <div className="alert warn small">Cannot be synced: {d.excluded}</div>}
            {!d.namespace && <div className="row wrap">
              <label>Namespace<input value={d.namespace} onChange={(e) => edit(i, { namespace: e.target.value })} /></label>
              <label>Secret name<input value={d.secret_name} onChange={(e) => edit(i, { secret_name: e.target.value })} /></label>
              <label>Cluster<select value={d.kube_connector_id ?? ""} onChange={(e) => edit(i, { kube_connector_id: e.target.value })}><option value="">Choose</option>{scope.kubernetes.map((k) => <option key={k.id} value={k.id}>{k.name}</option>)}</select></label>
            </div>}
            <label>AWS secret name<input className="mono" value={d.aws_secret_name} onChange={(e) => edit(i, { aws_secret_name: e.target.value, collision: false })} /></label>
            {d.collision && <div className="alert warn small">Another mapping already uses this AWS name.</div>}
            {d.aws_exists && d.namespace && <div className="small muted">An AWS secret with this name exists: the first comparison will ask which side wins.</div>}
            <div className="row wrap">
              <label>Format<select value={d.payload_format} onChange={(e) => edit(i, { payload_format: e.target.value })}>
                <option value="moks-k8s-v1">moks-k8s-v1 (lossless, binary-safe)</option><option value="json">JSON object of text values</option><option value="text">Text (one key)</option><option value="binary">Binary (one key)</option></select></label>
              {(d.payload_format === "text" || d.payload_format === "binary") && <label>Kubernetes key<input value={d.key_mapping?.key ?? ""} onChange={(e) => edit(i, { key_mapping: { key: e.target.value } })} /></label>}
              <label>Direction<select value={d.direction} onChange={(e) => edit(i, { direction: e.target.value })}><option value="export">Kubernetes → AWS</option><option value="import">AWS → Kubernetes</option><option value="both">Both, reviewed each time</option></select></label>
              <label>Type<input value={d.k8s_type} onChange={(e) => edit(i, { k8s_type: e.target.value })} /></label>
            </div>
            {d.owner?.manager && d.owner.manager !== "moks" && <div className="stack">
              <div className="alert warn small">{d.owner.manager} manages this secret. MOKS writes into it only if you override it explicitly.</div>
              <label className="check"><input type="checkbox" checked={d.management_policy === "override"} onChange={(e) => edit(i, { management_policy: e.target.checked ? "override" : "refuse_managed" })} /> Allow MOKS to overwrite what {d.owner.manager} manages</label>
            </div>}
            {d.result === "created" && <Badge kind="ok">Created</Badge>}
            {d.error && <div className="alert error small">{d.error}</div>}
          </fieldset>
        ))}
        <div className="row end"><button type="button" className="ghost" onClick={onClose}>Cancel</button><button type="button" disabled={saving || !drafts?.length} onClick={save}>{saving ? "Saving…" : "Create mappings"}</button></div>
      </div>
    </Dialog>
  );
}

interface Mapping { id: string; version: number; cluster_name: string; namespace: string; secret_name: string; aws_account: string; aws_region: string; aws_secret_name: string; aws_secret_arn: string | null;
  payload_format: string; direction: string; k8s_type: string; management_policy: string; expected_owner: Record<string, string>; last_status: string | null; last_compared_at: string | null; busy: boolean;
  baseline: { at: string; source: string } | null }

const DIRECTION: Record<string, string> = { export: "K8s → AWS", import: "AWS → K8s", both: "Both" };

function Mappings({ env, scope, onScheduled }: { env: string; scope: ScopeInfo; onScheduled: () => void }) {
  const nav = useNavigate();
  const list = useLoad(() => api<{ items: Mapping[] }>(`/secrets/${env}/mappings`), [env]);
  const [q, setQ] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [planKind, setPlanKind] = useState<"sync" | "delete">();
  const [editing, setEditing] = useState<Mapping>();
  const [scheduling, setScheduling] = useState(false);
  const { toast } = useFeedback();
  const rows = (list.data?.items ?? []).filter((m) => !q || `${m.namespace}/${m.secret_name} ${m.aws_secret_name}`.toLowerCase().includes(q.toLowerCase()));
  const toggle = (id: string) => setSelected((s) => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n; });
  async function retire(m: Mapping) {
    if (!window.confirm(`Stop managing ${m.namespace}/${m.secret_name}? Neither secret is changed.`)) return;
    try { await api(`/secrets/${env}/mappings/${m.id}${qs({ version: m.version })}`, { method: "DELETE" }); toast("Mapping retired."); list.reload(); } catch (e) { toast(e instanceof Error ? e.message : String(e)); }
  }
  const cols: Col<Mapping>[] = [
    { key: "pick", head: "", cell: (m) => <input type="checkbox" aria-label={`Select ${m.secret_name}`} checked={selected.has(m.id)} onClick={(e) => e.stopPropagation()} onChange={() => toggle(m.id)} /> },
    { key: "k8s", head: "Kubernetes", primary: true, cell: (m) => <Identifier name={`${m.cluster_name}/${m.namespace}/${m.secret_name}`} what="Secret" /> },
    { key: "status", head: "Status", status: true, cell: (m) => <>{m.last_status ? <Status s={m.last_status} /> : <Status s="not_compared" />}{m.busy && <Badge kind="info">running</Badge>}</> },
    { key: "aws", head: "AWS", cell: (m) => <span className="small mono" title={`${m.aws_account} ${m.aws_region}`}>{m.aws_secret_name}</span> },
    { key: "dir", head: "Direction", cell: (m) => DIRECTION[m.direction] },
    { key: "fmt", head: "Format", wide: true, cell: (m) => m.payload_format },
    { key: "base", head: "Last verified", cell: (m) => <Time iso={m.baseline?.at} /> },
  ];
  return (
    <section className="stack">
      <div className="row wrap">
        <input type="search" aria-label="Search mappings" placeholder="Search…" value={q} onChange={(e) => setQ(e.target.value)} />
        <span className="grow" />
        {scope.can.propose && <>
          <button type="button" className="ghost" onClick={() => setSelected(selected.size === rows.length ? new Set() : new Set(rows.map((m) => m.id)))}>{selected.size === rows.length && rows.length ? "Clear selection" : "Select all"}</button>
          <button type="button" disabled={!selected.size} onClick={() => setPlanKind("sync")}>Compare {selected.size || ""} selected</button>
          {canSchedule(scope) && <button type="button" className="ghost" disabled={!selected.size} onClick={() => setScheduling(true)}>Schedule sync</button>}
          <button type="button" className="ghost danger" disabled={!selected.size} onClick={() => setPlanKind("delete")}>Propose deletion</button>
        </>}
      </div>
      <Loading load={list} />
      {list.data && <DataTable label="Secret mappings" rows={rows} cols={cols} rowKey={(m) => m.id}
        menu={scope.can.map ? (m) => ({ label: `Actions for ${m.secret_name}`, items: [{ label: "Edit", run: () => setEditing(m) }, { label: "Retire", run: () => retire(m), danger: true }] }) : undefined}
        empty={<Empty title="No mappings yet.">Select discovered secrets on the Inventory tab and map them.</Empty>} />}
      {planKind && <PlanDialog env={env} kind={planKind} ids={[...selected]} onClose={() => setPlanKind(undefined)} onCreated={(id) => nav(`/secrets/plans/${id}?env=${env}`)} />}
      {editing && <EditMapping env={env} m={editing} onClose={() => setEditing(undefined)} onDone={() => { setEditing(undefined); list.reload(); }} />}
      {scheduling && <ScheduleDialog env={env} ids={[...selected]} onClose={() => setScheduling(false)} onDone={() => { setScheduling(false); toast("Schedule created. It runs once someone else approves it."); onScheduled(); }} />}
    </section>
  );
}

function EditMapping({ env, m, onClose, onDone }: { env: string; m: Mapping; onClose: () => void; onDone: () => void }) {
  const [v, setV] = useState({ direction: m.direction, payload_format: m.payload_format, k8s_type: m.k8s_type, management_policy: m.management_policy,
    owner_k8s: m.expected_owner?.kubernetes ?? "none", owner_aws: m.expected_owner?.aws ?? "none", aws_secret_name: m.aws_secret_name });
  const [error, setError] = useState<unknown>();
  async function save(e: FormEvent) {
    e.preventDefault();
    try {
      await api(`/secrets/${env}/mappings/${m.id}`, { method: "PATCH", body: { version: m.version, direction: v.direction, payload_format: v.payload_format, k8s_type: v.k8s_type,
        management_policy: v.management_policy, expected_owner: { kubernetes: v.owner_k8s, aws: v.owner_aws }, ...(m.aws_secret_arn ? {} : { aws_secret_name: v.aws_secret_name }) } });
      onDone();
    } catch (err) { setError(err); }
  }
  return (
    <Dialog title={`Edit ${m.namespace}/${m.secret_name}`} onClose={onClose}>
      <form className="stack" onSubmit={save}>
        <label>AWS secret name<input className="mono" value={v.aws_secret_name} disabled={!!m.aws_secret_arn} onChange={(e) => setV({ ...v, aws_secret_name: e.target.value })} /></label>
        <label>Direction<select value={v.direction} onChange={(e) => setV({ ...v, direction: e.target.value })}>{Object.entries(DIRECTION).map(([k, l]) => <option key={k} value={k}>{l}</option>)}</select></label>
        <label>Format<select value={v.payload_format} onChange={(e) => setV({ ...v, payload_format: e.target.value })}>{["moks-k8s-v1", "json", "text", "binary"].map((x) => <option key={x}>{x}</option>)}</select></label>
        <label>Kubernetes type<input value={v.k8s_type} onChange={(e) => setV({ ...v, k8s_type: e.target.value })} /></label>
        <label>Management policy<select value={v.management_policy} onChange={(e) => setV({ ...v, management_policy: e.target.value })}><option value="refuse_managed">Never write into a secret another controller manages</option><option value="override">Override the expected owner below</option></select></label>
        <div className="row wrap"><label>Expected Kubernetes owner<input value={v.owner_k8s} onChange={(e) => setV({ ...v, owner_k8s: e.target.value })} /></label>
          <label>Expected AWS owner<input value={v.owner_aws} onChange={(e) => setV({ ...v, owner_aws: e.target.value })} /></label></div>
        <p className="small muted">Changing the format or type clears the verified baseline: the next comparison asks for a source again.</p>
        <ErrorBox error={error} />
        <div className="row end"><button>Save</button></div>
      </form>
    </Dialog>
  );
}

function PlanDialog({ env, kind, ids, onClose, onCreated, rollback, defaultTargets = ["aws"] }: { env: string; kind: "sync" | "delete" | "rollback"; ids: string[]; onClose: () => void;
  onCreated: (id: string) => void; rollback?: Record<string, any>; defaultTargets?: string[] }) {
  const [reason, setReason] = useState("");
  const [targets, setTargets] = useState<string[]>(defaultTargets);
  const [error, setError] = useState<unknown>();
  async function submit(e: FormEvent) {
    e.preventDefault();
    try {
      const r = await api<{ id: string }>(`/secrets/${env}/plans`, { method: "POST", body: { kind, mapping_ids: ids, reason, ...(kind === "delete" ? { targets } : {}), ...(rollback ? { rollback } : {}) } });
      onCreated(r.id);
    } catch (err) { setError(err); }
  }
  const title = kind === "delete" ? "Propose deletion" : kind === "rollback" ? "Propose rollback" : `Compare ${ids.length} mapping${ids.length === 1 ? "" : "s"}`;
  return (
    <Dialog title={title} onClose={onClose}>
      <form className="stack" onSubmit={submit}>
        <p className="small muted">{kind === "delete" ? "Deletion is a separate plan that always needs another person's approval. AWS keeps the secret for its recovery window." :
          "The executor reads both sides and builds a draft. You review it, someone else approves it, and nothing is written before that."}</p>
        {kind === "delete" && <fieldset><legend>Delete from</legend>{["aws", "kubernetes"].map((t) => <label key={t} className="check"><input type="checkbox" checked={targets.includes(t)}
          onChange={() => setTargets(targets.includes(t) ? targets.filter((x) => x !== t) : [...targets, t])} /> {t === "aws" ? "AWS Secrets Manager" : "Kubernetes"}</label>)}</fieldset>}
        <label>Reason<textarea required minLength={5} rows={2} value={reason} onChange={(e) => setReason(e.target.value)} /></label>
        <ErrorBox error={error} />
        <div className="row end"><button>Start comparison</button></div>
      </form>
    </Dialog>
  );
}

// ---- plans -------------------------------------------------------------------------------------------------

function Plans({ env }: { env: string }) {
  const nav = useNavigate();
  const list = useLoad(() => api<{ items: any[] }>(`/secrets/${env}/plans`), [env]);
  useInterval(list.reload, 5000, !!list.data?.items.some((p) => BUSY.includes(p.status)));
  const cols: Col<any>[] = [
    { key: "what", head: "Plan", primary: true, cell: (p) => <span>{p.kind} · {p.mapping_count} mapping{p.mapping_count === 1 ? "" : "s"}{p.summary?.schedule && <> · <Badge kind="info">schedule {p.summary.schedule}</Badge></>}</span> },
    { key: "status", head: "Status", status: true, cell: (p) => <Badge kind={PLAN_TONE[p.status] ?? "neutral"}>{p.status}{p.expired ? " (expired)" : ""}</Badge> },
    { key: "ns", head: "Namespaces", cell: (p) => p.namespaces.join(", ") },
    { key: "by", head: "Requested by", cell: (p) => p.requested_by ?? "—" },
    { key: "when", head: "Created", cell: (p) => <Time iso={p.created_at} /> },
  ];
  return (
    <section className="stack">
      <Loading load={list} />
      {list.data && <DataTable label="Secret plans" rows={list.data.items} cols={cols} rowKey={(p) => p.id} onRow={(p) => nav(`/secrets/plans/${p.id}?env=${env}`)}
        empty={<Empty title="No plans yet.">Compare mappings on the Mappings tab to start one.</Empty>} />}
    </section>
  );
}

function Side({ title, st, where }: { title: string; st: any; where: ReactNode }) {
  return (
    <div className="card stack small">
      <strong>{title}</strong>
      <div className="muted">{where}</div>
      {st.error ? <div className="alert error">{st.error}</div> : st.exists === false ? <span>Does not exist</span> : <>
        <span>Version <code>{st.resource_version ?? st.version_id ?? "—"}</code>{st.type ? <> · {st.type}</> : null}</span>
        {st.owner?.manager && <span>Owner: <Badge kind={st.owner.manager === "moks" ? "info" : "warn"}>{st.owner.manager}</Badge></span>}
        <span>Last modified <Time iso={st.last_modified} /> <span className="muted">(not used to pick a winner)</span></span>
        {st.unsupported && <div className="alert warn">{st.unsupported}</div>}
      </>}
    </div>
  );
}

function Diff({ d }: { d: any }) {
  if (!d) return null;
  if (d.hidden) return <p className="small muted">Key names are hidden: you need secrets.compare in this namespace.</p>;
  const fp = (x: any) => x ? (x.empty ? "empty" : `••••${x.fp}${x.binary ? " (binary)" : ""}`) : "absent";
  const rows = [...(d.added ?? []).map((x: any) => ["added", x.key, "absent", fp(x.to)]), ...(d.removed ?? []).map((x: any) => ["removed", x.key, fp(x.from), "absent"]),
    ...(d.changed ?? []).map((x: any) => ["changed", x.key, fp(x.from), fp(x.to)])];
  return (
    <div className="stack">
      {d.type_change && <div className="alert warn small">Type changes from {d.type_change.from} to {d.type_change.to}</div>}
      {rows.length ? <div className="table-wrap"><table className="small"><thead><tr><th>Change</th><th>Key</th><th>Now</th><th>After</th></tr></thead>
        <tbody>{rows.map((r) => <tr key={r[0] + r[1]}><td><Badge kind={r[0] === "removed" ? "bad" : r[0] === "added" ? "info" : "warn"}>{r[0]}</Badge></td><td className="mono">{r[1]}</td><td className="mono">{r[2]}</td><td className="mono">{r[3]}</td></tr>)}</tbody></table></div>
        : <p className="small muted">No key changes.</p>}
      {d.unchanged?.length > 0 && <p className="small muted">Unchanged: {d.unchanged.join(", ")}</p>}
      <p className="small muted">Fingerprints are keyed digests: they show that values differ, never what they are.</p>
    </div>
  );
}

function ItemCard({ env, plan, item, onChange }: { env: string; plan: any; item: any; onChange: () => void }) {
  const m = item.mapping;
  const nav = useNavigate();
  const { toast } = useFeedback();
  const [error, setError] = useState<unknown>();
  const editable = plan.allowed.edit;
  const keys = useMemo(() => [...new Set([...Object.keys(item.kubernetes.keys ?? {}), ...Object.keys(item.aws.keys ?? {})])].sort(), [item]);
  async function patch(body: any) {
    setError(undefined);
    try { await api(`/secrets/${env}/plans/${plan.id}/items/${item.id}`, { method: "PATCH", body: { version: plan.version, ...body } }); onChange(); } catch (e) { setError(e); }
  }
  async function restart(name: string) {
    const reason = window.prompt(`Reason for restarting Deployment ${name}`, `pick up the new ${m.secret_name} Secret`);
    if (!reason) return;
    try {
      const r = await api<{ action_plan_id: string }>(`/secrets/${env}/plans/${plan.id}/items/${item.id}/restart`, { method: "POST", body: { kind: "Deployment", name, reason } });
      toast("Restart proposed as a separate action plan.");
      nav(`/actions/${r.action_plan_id}`);
    } catch (e) { setError(e); }
  }
  const conflict = item.classification === "changed_both";
  return (
    <article className="card stack" aria-label={`${m.namespace}/${m.secret_name}`}>
      <div className="row between wrap">
        <label className="check">{editable && <input type="checkbox" checked={item.included} onChange={(e) => patch({ included: e.target.checked })} aria-label="Include in plan" />}
          <strong className="mono">{m.namespace}/{m.secret_name}</strong> ⇄ <span className="mono small">{m.aws_secret_name}</span></label>
        <span className="row">{item.classification && <Status s={item.classification} />}<Badge kind={ITEM_TONE[item.status] ?? "neutral"}>{item.status}</Badge></span>
      </div>
      <div className="grid-2">
        <Side title="Kubernetes" st={item.kubernetes} where={<>{m.cluster_name} · {m.namespace}</>} />
        <Side title="AWS Secrets Manager" st={item.aws} where={<>{m.aws_account} · {m.aws_region}</>} />
      </div>
      {item.first_sync && conflict && <div className="alert warn small">First sync: there is no verified baseline and the sides differ. Choose the source.</div>}
      {editable && plan.kind === "sync" && item.classification && !["failed", "unsupported", "in_sync"].includes(item.classification) && (
        <fieldset className="stack"><legend>Resolution</legend>
          <div className="row wrap">{[["kubernetes", "Use Kubernetes"], ["aws", "Use AWS"], ["edited", "Choose per key"]].map(([v, l]) =>
            <label key={v} className="check"><input type="radio" name={`res-${item.id}`} checked={(item.resolution ?? (conflict ? null : item.proposal)) === v} onChange={() => patch({ resolution: v })} /> {l}</label>)}</div>
          {item.resolution === "edited" && <div className="stack">{keys.map((k) => (
            <label key={k} className="row"><span className="mono grow">{k}</span>
              <select value={item.key_choices?.[k] ?? ""} onChange={(e) => patch({ resolution: "edited", key_choices: { ...item.key_choices, [k]: e.target.value } })}>
                <option value="" disabled>Choose</option>{item.kubernetes.keys?.[k] && <option value="kubernetes">Kubernetes</option>}{item.aws.keys?.[k] && <option value="aws">AWS</option>}<option value="omit">Leave out</option>
              </select></label>))}</div>}
        </fieldset>)}
      {item.blocked && <div className={`alert ${item.status === "conflict" ? "warn" : "info"} small`}>{item.blocked}</div>}
      {item.targets.length > 0 && <div className="stack">
        {item.targets.map((t: string) => (
          <div key={t} className="stack">
            <strong className="small">{item.actions[t] === "create" ? "Create" : item.actions[t] === "delete" ? "Delete" : item.actions[t] === "restage" ? "Make version current" : "Update"} in {t === "aws" ? `AWS (${m.aws_account} ${m.aws_region})` : `Kubernetes (${m.cluster_name}/${m.namespace})`}</strong>
            <Diff d={item.diff[t]} />
          </div>))}
      </div>}
      {item.warnings.length > 0 && <ul className="small warn-list">{item.warnings.map((w: string) => <li key={w}>⚠ {w}</li>)}</ul>}
      {item.impact?.length > 0 && <div className="stack small">
        <strong>Workloads that read this Secret</strong>
        <p className="muted">Changing a Secret does not restart anything. A restart is a separate action with its own review.</p>
        <ul>{item.impact.map((w: any) => <li key={w.kind + w.name}><span className="mono">{w.kind}/{w.name}</span> ({w.how.join(", ")}): {w.note}
          {w.restart_required && w.restartable && <> <button type="button" className="ghost small" onClick={() => restart(w.name)}>Propose restart</button></>}</li>)}</ul>
      </div>}
      {item.error && item.status !== "conflict" && <div className="alert error small">{item.error}</div>}
      {item.attempts > 0 && <p className="small muted">Attempts {item.attempts}{Object.keys(item.write_state ?? {}).length > 0 && <> · write state {Object.entries(item.write_state).map(([k, v]) => `${k}: ${v}`).join(", ")}</>}</p>}
      <ErrorBox error={error} />
    </article>
  );
}

export function SecretPlan() {
  const { id } = useParams();
  const [params] = useSearchParams();
  const env = params.get("env") ?? "";
  const nav = useNavigate();
  const plan = useLoad(() => api<any>(`/secrets/${env}/plans/${id}`), [env, id]);
  const p = plan.data;
  useInterval(plan.reload, 2000, !!p && BUSY.includes(p.status));
  const [error, setError] = useState<unknown>();
  const [comment, setComment] = useState("");
  const [confirm, setConfirm] = useState("");
  async function act(path: string, body: any, then?: (r: any) => void) {
    setError(undefined);
    try { const r = await api(`/secrets/${env}/plans/${id}/${path}`, { method: "POST", body }); then ? then(r) : plan.reload(); } catch (e) { setError(e); }
  }
  if (plan.error) return <><PageHead title="Secret plan" />{denied(plan.error) ? <StateView kind="denied" why={(plan.error as Error).message} /> : <ErrorBox error={plan.error} onRetry={plan.reload} />}</>;
  if (!p) return <Loading load={plan} />;
  const included = p.items.filter((i: any) => i.included && i.targets.length);
  return (
    <>
      <PageHead title={`${p.kind === "sync" ? "Sync" : p.kind === "delete" ? "Deletion" : "Rollback"} plan`} sub={<>{p.reason} · requested by {p.requested_by} · <Time iso={p.created_at} /></>}>
        <Link to={`/secrets?env=${env}&tab=plans`}>All plans</Link>
      </PageHead>
      <div className="row wrap">
        <Badge kind={PLAN_TONE[p.status] ?? "neutral"}>{p.status}</Badge>
        {p.expires_at && <span className="small muted">Approval valid until <Time iso={p.expires_at} /></span>}
        {p.plan_hash && <span className="small muted">Hash <code>{p.plan_hash.slice(0, 12)}</code></span>}
      </div>
      {p.status === "comparing" && <StateView kind="loading" title="The executor is reading both sides" />}
      {p.status === "compare_failed" && <StateView kind="unavailable" title="The comparison failed" why={p.error} next={p.allowed.refresh && <button type="button" onClick={() => act("refresh", {}, (r) => nav(`/secrets/plans/${r.id}?env=${env}`))}>Compare again</button>} />}
      {p.expired && <StateView kind="stale" title="This plan's approval window has passed" why="Both sides may have changed since. A fresh comparison and approval are needed." next={p.allowed.refresh && <button type="button" onClick={() => act("refresh", {}, (r) => nav(`/secrets/plans/${r.id}?env=${env}`))}>Refresh plan</button>} />}
      {p.items.some((i: any) => i.status === "stale") && <StateView kind="stale" title="Some items changed after review" why="MOKS never writes over a change it did not review. Those items need a fresh comparison."
        next={p.allowed.refresh && <button type="button" onClick={() => act("refresh", {}, (r) => nav(`/secrets/plans/${r.id}?env=${env}`))}>Refresh plan</button>} />}
      {p.status === "partial" && <StateView kind="partial" why="Some items succeeded and others did not. Succeeded items are verified and their baselines moved; the rest are unchanged or waiting for a retry." />}
      <div className="stack">{p.items.map((i: any) => <ItemCard key={i.id} env={env} plan={p} item={i} onChange={plan.reload} />)}</div>
      {p.approvals.length > 0 && <section className="card small stack"><strong>Decisions</strong>{p.approvals.map((a: any, n: number) => <span key={n}>{a.decision} by {a.by} <Time iso={a.at} />{a.comment && <>: {a.comment}</>}{!a.current && " (for an earlier version)"}</span>)}</section>}
      <ErrorBox error={error} />
      <div className="row wrap end sticky-actions">
        {p.allowed.cancel && <button type="button" className="ghost" onClick={() => act("cancel", {})}>Cancel plan</button>}
        {p.allowed.submit && <button type="button" disabled={!included.length} onClick={() => act("submit", { version: p.version })}>Submit {included.length} item{included.length === 1 ? "" : "s"} for approval</button>}
        {p.allowed.approve && <>
          <input aria-label="Approval comment" placeholder="Comment" value={comment} onChange={(e) => setComment(e.target.value)} />
          {p.kind === "delete" && <input aria-label="Confirmation" placeholder={`Type: ${p.summary?.confirm}`} value={confirm} onChange={(e) => setConfirm(e.target.value)} />}
          <button type="button" className="ghost danger" onClick={() => act("decision", { approve: false, plan_hash: p.plan_hash, comment })}>Reject</button>
          <button type="button" onClick={() => act("decision", { approve: true, plan_hash: p.plan_hash, comment, confirm: confirm || null })}>Approve</button>
        </>}
        {p.allowed.execute && !p.expired && <button type="button" onClick={() => act("execute", { plan_hash: p.plan_hash })}>Execute</button>}
        {p.allowed.retry && !p.expired && p.items.some((i: any) => i.status === "failed") && <button type="button" onClick={() => act("retry", {})}>Retry failed items</button>}
      </div>
    </>
  );
}

// ---- history -----------------------------------------------------------------------------------------------

function History({ env }: { env: string }) {
  const nav = useNavigate();
  const list = useLoad(() => api<{ items: any[] }>(`/secrets/${env}/history`), [env]);
  const [rollback, setRollback] = useState<any>();
  const cols: Col<any>[] = [
    { key: "secret", head: "Secret", primary: true, cell: (h) => <span className="mono">{h.secret}</span> },
    { key: "status", head: "Outcome", status: true, cell: (h) => <Badge kind={ITEM_TONE[h.status] ?? "neutral"}>{h.status}</Badge> },
    { key: "what", head: "Change", cell: (h) => Object.entries(h.actions ?? {}).map(([t, a]) => `${a} ${t}`).join(", ") },
    { key: "keys", head: "Keys", cell: (h) => h.keys ? h.keys.join(", ") || "—" : <span className="muted small">hidden</span> },
    { key: "versions", head: "AWS version", wide: true, cell: (h) => h.aws_version_after ? <code className="small">{String(h.aws_version_before ?? "new").slice(0, 8)} → {String(h.aws_version_after).slice(0, 8)}</code> : "—" },
    { key: "by", head: "By", cell: (h) => h.by ?? "—" },
    { key: "at", head: "When", cell: (h) => <Time iso={h.at} /> },
  ];
  return (
    <section className="stack">
      <Loading load={list} />
      {list.data && <DataTable label="Sync history" rows={list.data.items} cols={cols} rowKey={(h) => h.item_id} onRow={(h) => nav(`/secrets/plans/${h.plan_id}?env=${env}`)}
        menu={(h) => ({ label: `Actions for ${h.secret}`, items: [{ label: "Propose rollback to the previous AWS version", disabled: !h.rollback_available, run: () => setRollback(h) }] })}
        empty={<Empty title="Nothing has run yet." />} />}
      {rollback && <PlanDialog env={env} kind="rollback" ids={[rollback.mapping_id]} rollback={{ [rollback.mapping_id]: { version: rollback.aws_version_before, targets: ["aws", "kubernetes"] } }}
        onClose={() => setRollback(undefined)} onCreated={(id) => nav(`/secrets/plans/${id}?env=${env}`)} />}
    </section>
  );
}

// ---- bulk mapping ------------------------------------------------------------------------------------------

const INTERVALS: [number, string][] = [[15, "Every 15 minutes"], [60, "Every hour"], [360, "Every 6 hours"], [1440, "Every day"]];

function BulkMap({ env, scope, side, ids, onClose, onReview, onDone }: { env: string; scope: ScopeInfo; side: string; ids: string[]; onClose: () => void; onReview: () => void; onDone: () => void }) {
  const nav = useNavigate();
  const aws0 = side === "aws" ? "import" : "export";
  const [aws, setAws] = useState(scope.aws[0]?.id ?? "");
  const [direction, setDirection] = useState(aws0);
  const writable = scope.kubernetes.filter((k) => k.writable);
  const [kube, setKube] = useState(writable[0]?.id ?? "");
  const [ns, setNs] = useState("");
  const [compareNow, setCompareNow] = useState(!!scope.can.propose);
  const [schedule, setSchedule] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>();
  const [result, setResult] = useState<{ created: Mapping[]; skipped: { secret: string; why: string }[]; plan_id: string | null; schedule?: string }>();
  const namespaces = writable.find((k) => k.id === kube)?.namespaces ?? [];
  async function run(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(undefined);
    try {
      const r = await api<{ created: Mapping[]; skipped: { secret: string; why: string }[]; plan_id: string | null }>(`/secrets/${env}/mappings/bulk`, { method: "POST", body: {
        inventory_ids: ids, aws_connector_id: aws, direction, compare: compareNow, ...(side === "aws" ? { kube_connector_id: kube, namespace: ns } : {}) } });
      let made: string | undefined;
      if (schedule && r.created.length) {
        const stamp = new Date().toISOString().slice(0, 16).replace("T", " ");
        try {
          await api(`/secrets/${env}/schedules`, { method: "POST", body: { name: `Auto-mapped ${stamp}`, mapping_ids: r.created.map((m) => m.id), interval_minutes: schedule } });
          made = `A schedule (${every(schedule)}) waits for approval on the Schedules tab.`;
        } catch (err) { made = `The schedule was not created: ${err instanceof Error ? err.message : String(err)}`; }
      }
      setResult({ ...r, schedule: made });
    } catch (err) { setError(err); }
    setBusy(false);
  }
  if (result) return (
    <Dialog title="Mapped" onClose={onDone}>
      <div className="stack">
        <p><Badge kind="ok">{result.created.length}</Badge> mapping{result.created.length === 1 ? "" : "s"} created{result.skipped.length ? <>, <Badge kind="warn">{result.skipped.length}</Badge> skipped</> : null}.</p>
        {result.skipped.length > 0 && <ul className="small state-list">{result.skipped.map((x) => <li key={x.secret}><span className="mono">{x.secret}</span>: {x.why}</li>)}</ul>}
        {result.schedule && <p className="small">{result.schedule}</p>}
        <div className="row end">
          <button type="button" className="ghost" onClick={onDone}>Close</button>
          {result.plan_id && <button type="button" onClick={() => nav(`/secrets/plans/${result.plan_id}?env=${env}`)}>Review the comparison</button>}
        </div>
      </div>
    </Dialog>
  );
  return (
    <Dialog title={`Map ${ids.length} secret${ids.length === 1 ? "" : "s"}`} onClose={onClose}>
      <form className="stack" onSubmit={run}>
        <p className="small muted">Each one gets the default mapping: names include the cluster and namespace, the lossless moks-k8s-v1 format, and
          {side === "aws" ? " a Kubernetes name from the last part of the AWS name." : " the Secret's own type."} Secrets that are excluded, already mapped or outside the connector are skipped and listed. Nothing is written to either side until a plan or an approved schedule runs.</p>
        <label>AWS connector<select value={aws} onChange={(e) => setAws(e.target.value)}>{scope.aws.map((a) => <option key={a.id} value={a.id}>{a.name} ({a.account_id} {a.region})</option>)}</select></label>
        <label>Direction<select value={direction} onChange={(e) => setDirection(e.target.value)}>
          {side === "aws" ? <><option value="import">Import: AWS → Kubernetes</option><option value="both">Sync both ways</option></>
            : <><option value="export">Sync to AWS: Kubernetes → AWS</option><option value="both">Sync both ways</option><option value="import">Import: AWS → Kubernetes</option></>}</select></label>
        {side === "aws" && <div className="row wrap">
          <label>Cluster<select value={kube} onChange={(e) => { setKube(e.target.value); setNs(""); }}>{writable.map((k) => <option key={k.id} value={k.id}>{k.name}</option>)}</select></label>
          <label>Namespace<select required value={ns} onChange={(e) => setNs(e.target.value)}><option value="" disabled>Choose</option>{namespaces.map((n) => <option key={n}>{n}</option>)}</select></label>
        </div>}
        {scope.can.propose && <label className="check"><input type="checkbox" checked={compareNow} onChange={(e) => setCompareNow(e.target.checked)} /> Compare them right away (a read-only plan to review)</label>}
        {scope.can.propose && canSchedule(scope) && <label>Keep them in sync<select value={schedule} onChange={(e) => setSchedule(Number(e.target.value))}><option value={0}>No schedule</option>
          {INTERVALS.map(([m, l]) => <option key={m} value={m}>{l} (after approval)</option>)}</select></label>}
        <ErrorBox error={error} />
        <div className="row end">
          <button type="button" className="ghost" onClick={onReview}>Review each one instead</button>
          <button disabled={busy || !aws || (side === "aws" && !ns)}>{busy ? "Mapping…" : `Map ${ids.length}`}</button>
        </div>
      </form>
    </Dialog>
  );
}

// ---- details and values ------------------------------------------------------------------------------------

function SecretDetails({ env, scope, row, onClose }: { env: string; scope: ScopeInfo; row: Row; onClose: () => void }) {
  const nav = useNavigate();
  const [deleting, setDeleting] = useState(false);
  const label = row.side === "kubernetes" ? `${row.namespace}/${row.name}` : row.name;
  const facts: [string, ReactNode][] = [["Source", row.side === "kubernetes" ? "Kubernetes" : "AWS Secrets Manager"], ["Sync status", <Status s={row.sync_status} />],
    ["Type", row.type ?? "—"], ["Keys", row.keys ? (row.keys.length ? <span className="mono">{row.keys.join(", ")}</span> : "none") : row.key_count != null ? `${row.key_count}` : "after the first comparison"],
    ["Owner", row.owner?.manager ?? "none"], ["Version", row.version ? <code>{row.version}</code> : "—"], ["Last modified", <Time iso={row.last_modified} />],
    ["Mapped to", row.mapping ? <span className="mono">{row.mapping.other}</span> : "not mapped"], ...(row.arn ? [["ARN", <span className="mono small">{row.arn}</span>] as [string, ReactNode]] : [])];
  return (
    <Drawer title={label} heading={<Identifier name={label} what="secret name" />} onClose={onClose}
      footer={scope.can.propose && <div className="row end">{row.mapping
        ? <button type="button" className="ghost danger" onClick={() => setDeleting(true)}>Propose deletion</button>
        : <span className="small muted">Map this secret to delete it through a reviewed plan.</span>}</div>}>
      <dl className="kv">{facts.map(([k, v]) => <Fragment key={k}><dt>{k}</dt><dd>{v}</dd></Fragment>)}</dl>
      {row.excluded && <div className="alert warn small">Not syncable: {row.excluded}</div>}
      {scope.can.reveal ? <RevealPanel env={env} row={row} /> : <p className="small muted">Values are shown only to people with secrets.reveal (the secrets_approver role) in this namespace.</p>}
      {deleting && row.mapping && <PlanDialog env={env} kind="delete" ids={[row.mapping.id]} defaultTargets={[row.side === "aws" ? "aws" : "kubernetes"]}
        onClose={() => setDeleting(false)} onCreated={(id) => nav(`/secrets/plans/${id}?env=${env}`)} />}
    </Drawer>
  );
}

const HIDE_AFTER_SECONDS = 120;

function RevealPanel({ env, row }: { env: string; row: Row }) {
  const key = useRef<CryptoKey | null>(null);
  const [job, setJob] = useState<string>();
  const [values, setValues] = useState<Revealed>();
  const [shown, setShown] = useState<Set<string>>(new Set());
  const [left, setLeft] = useState(0);
  const [error, setError] = useState<unknown>();
  const { toast } = useFeedback();
  useEffect(() => () => { key.current = null; }, []);
  function hide() { setValues(undefined); setShown(new Set()); setJob(undefined); key.current = null; }
  useInterval(async () => {
    if (!job) return;
    try {
      const r = await api<{ status: string; sealed?: any; error?: string }>(`/secrets/${env}/reveal/${job}`);
      if (r.status === "succeeded" && r.sealed && key.current) {
        setValues(await openSealed(key.current, r.sealed, job));
        key.current = null;  // one use: the next view makes a new key
        setJob(undefined);
        setLeft(HIDE_AFTER_SECONDS);
      } else if (r.status === "failed" || r.status === "cancelled") { setError(new Error(r.error || "the executor could not read the secret")); setJob(undefined); }
    } catch (e) { setError(e); setJob(undefined); }
  }, 1000, !!job);
  useInterval(() => { if (left <= 1) hide(); else setLeft(left - 1); }, 1000, !!values);
  async function start() {
    setError(undefined);
    try {
      const pair = await newKeyPair();
      key.current = pair.privateKey;
      const r = await api<{ job_id: string }>(`/secrets/${env}/reveal`, { method: "POST", body: { inventory_id: row.id, public_key: pair.publicJwk } });
      setJob(r.job_id);
    } catch (e) { setError(e); key.current = null; }
  }
  const text = (v: { text?: string; base64?: string }) => v.text ?? v.base64 ?? "";
  async function copy(k: string, v: { text?: string; base64?: string }) {
    try { await navigator.clipboard.writeText(text(v)); toast(`Copied ${k}.`); } catch { toast("The browser refused clipboard access.", "bad"); }
  }
  return (
    <section className="stack" aria-label="Secret values">
      <div className="row between wrap"><strong>Values</strong>
        {!values && <button type="button" disabled={!!job} onClick={start}>{job ? "Reading…" : "Show values"}</button>}
        {values && <button type="button" className="ghost" onClick={hide}>Hide now ({left} s)</button>}
      </div>
      <p className="small muted">Every view is recorded in the audit log. The executor encrypts the values to a key made in this tab, so they never reach MOKS's database or API in readable form, and they disappear after {HIDE_AFTER_SECONDS / 60} minutes.</p>
      <ErrorBox error={error} />
      {values && <div className="table-wrap"><table className="small"><thead><tr><th>Key</th><th>Value</th><th /></tr></thead><tbody>
        {Object.entries(values.keys).map(([k, v]) => (
          <tr key={k}><td className="mono">{k}</td>
            <td className="mono name-full">{shown.has(k) ? (text(v) === "" ? <span className="muted">empty</span> : text(v)) : "••••••••"}{v.base64 !== undefined && <span className="muted"> (binary, base64)</span>}</td>
            <td className="row end"><button type="button" className="ghost small" onClick={() => setShown((s) => { const n = new Set(s); n.has(k) ? n.delete(k) : n.add(k); return n; })}>{shown.has(k) ? "Hide" : "Show"}</button>
              <button type="button" className="ghost small" onClick={() => copy(k, v)}>Copy</button></td></tr>))}
      </tbody></table></div>}
      {values && <p className="small muted">{values.type}{values.version ? <> · version <code>{values.version}</code></> : null}</p>}
    </section>
  );
}

// ---- schedules ---------------------------------------------------------------------------------------------

interface Schedule { id: string; name: string; status: string; paused_reason: string | null; interval_minutes: number; version: number; schedule_hash: string; requested_by: string | null;
  held: boolean; needs_fresh_approval: boolean; policy: "manual" | "scheduled";
  namespaces: string[]; stale_mappings: string[]; mappings: { id: string; secret: string; aws_secret_name: string; direction: string; last_status: string | null }[];
  next_run_at: string | null; last_run_at: string | null; last_plan_id: string | null;
  last_outcome: { status?: string; written?: number; needs_review?: { mapping_id: string; secret: string; aws: string; classification: string; why: string }[]; error?: string | null };
  approvals: { by: string | null; decision: string; comment: string; at: string | null; current: boolean }[]; allowed: Record<string, boolean> }

const SCHEDULE_TONE: Record<string, Tone> = { active: "ok", pending: "warn", paused: "neutral", held: "warn", rejected: "bad" };
const SCHEDULE_LABEL: Record<string, string> = { pending: "waiting for approval", held: "held by policy" };

function ScheduleDialog({ env, ids, onClose, onDone }: { env: string; ids: string[]; onClose: () => void; onDone: () => void }) {
  const [name, setName] = useState("");
  const [minutes, setMinutes] = useState(60);
  const [error, setError] = useState<unknown>();
  async function save(e: FormEvent) {
    e.preventDefault();
    try { await api(`/secrets/${env}/schedules`, { method: "POST", body: { name, mapping_ids: ids, interval_minutes: minutes } }); onDone(); } catch (err) { setError(err); }
  }
  return (
    <Dialog title={`Schedule ${ids.length} mapping${ids.length === 1 ? "" : "s"}`} onClose={onClose}>
      <form className="stack" onSubmit={save}>
        <p className="small muted">Once someone else approves it, each run compares these mappings and writes only one-sided changes in each mapping's direction.
          Conflicts, recreated secrets and anything blocked wait for a person. Nothing is ever deleted. Editing the schedule or a mapping needs a new approval.</p>
        <label>Name<input required maxLength={100} pattern="[A-Za-z0-9][A-Za-z0-9 ._\-]*" value={name} onChange={(e) => setName(e.target.value)} placeholder="shop secrets" /></label>
        <IntervalPicker value={minutes} onChange={setMinutes} />
        <ErrorBox error={error} />
        <div className="row end"><button>Create schedule</button></div>
      </form>
    </Dialog>
  );
}

function IntervalPicker({ value, onChange }: { value: number; onChange: (m: number) => void }) {
  const [custom, setCustom] = useState(!INTERVALS.some(([m]) => m === value));
  return (
    <div className="row wrap">
      <label>Runs<select value={custom ? -1 : value} onChange={(e) => { const m = Number(e.target.value); setCustom(m === -1); if (m !== -1) onChange(m); }}>
        {INTERVALS.map(([m, l]) => <option key={m} value={m}>{l}</option>)}<option value={-1}>Custom interval</option></select></label>
      {custom && <label>Every (minutes)<input type="number" min={5} max={10080} required value={value} onChange={(e) => onChange(Number(e.target.value))} /></label>}
    </div>
  );
}

function Schedules({ env, scope }: { env: string; scope: ScopeInfo }) {
  const list = useLoad(() => api<{ items: Schedule[] }>(`/secrets/${env}/schedules`), [env]);
  const [open, setOpen] = useState<string>();
  const current = list.data?.items.find((s) => s.id === open);
  const cols: Col<Schedule>[] = [
    { key: "name", head: "Schedule", primary: true, cell: (s) => <strong>{s.name}</strong> },
    { key: "status", head: "Status", status: true, cell: (s) => <Badge kind={SCHEDULE_TONE[s.status] ?? "neutral"}>{SCHEDULE_LABEL[s.status] ?? s.status}</Badge> },
    { key: "every", head: "Runs", cell: (s) => `every ${every(s.interval_minutes)}` },
    { key: "maps", head: "Mappings", cell: (s) => `${s.mappings.length}` },
    { key: "last", head: "Last run", cell: (s) => s.last_run_at ? <><Time iso={s.last_run_at} />{" · "}{s.last_outcome.written ?? 0} written{(s.last_outcome.needs_review?.length ?? 0) > 0 && <> · <Badge kind="warn">{s.last_outcome.needs_review!.length} need review</Badge></>}</> : "—" },
    { key: "next", head: "Next run", cell: (s) => s.status === "active" ? <Time iso={s.next_run_at} /> : "—" },
    { key: "by", head: "Created by", wide: true, cell: (s) => s.requested_by ?? "—" },
  ];
  return (
    <section className="stack">
      {canSchedule(scope)
        ? <p className="small muted">Select mappings on the Mappings tab and choose Schedule sync. A schedule runs only after someone else approves it{scope.can.approve ? "" : " (secrets_approver)"}.</p>
        : <div className="alert warn small" role="status"><strong>Manual-only sync.</strong> Every change to a secret is a plan someone reviews before it runs, so schedules do not run.
          Existing schedules are held with their approvals and run history kept; discovery still refreshes names and versions on its own.
          To sync, select mappings on the Mappings tab and choose Compare.</div>}
      <Loading load={list} />
      {list.data && <DataTable label="Sync schedules" rows={list.data.items} cols={cols} rowKey={(s) => s.id} onRow={(s) => setOpen(s.id)}
        empty={<Empty title="No schedules yet.">Schedule mappings from the Mappings tab, or when mapping secrets in bulk.</Empty>} />}
      {current && <ScheduleDrawer env={env} s={current} onClose={() => setOpen(undefined)} onChange={list.reload} />}
    </section>
  );
}

function ScheduleDrawer({ env, s, onClose, onChange }: { env: string; s: Schedule; onClose: () => void; onChange: () => void }) {
  const nav = useNavigate();
  const [comment, setComment] = useState("");
  const [minutes, setMinutes] = useState(s.interval_minutes);
  const [error, setError] = useState<unknown>();
  const [planFor, setPlanFor] = useState<string[]>();
  const { toast } = useFeedback();
  async function act(method: string, path: string, body?: any, done?: string) {
    setError(undefined);
    try { await api(`/secrets/${env}/schedules/${s.id}${path}`, { method, body }); if (done) toast(done); onChange(); } catch (e) { setError(e); }
  }
  const review = s.last_outcome.needs_review ?? [];
  return (
    <Drawer title={s.name} heading={<span className="row"><strong>{s.name}</strong><Badge kind={SCHEDULE_TONE[s.status] ?? "neutral"}>{SCHEDULE_LABEL[s.status] ?? s.status}</Badge></span>} onClose={onClose}
      footer={<div className="row wrap end">
        {s.allowed.run && <button type="button" className="ghost" onClick={() => act("POST", "/run", undefined, "The executor runs it within a few seconds.")}>Run now</button>}
        {s.allowed.pause && <button type="button" className="ghost" onClick={() => act("POST", "/pause")}>Pause</button>}
        {s.allowed.resume && <button type="button" onClick={() => act("POST", "/resume")}>Resume</button>}
        {s.allowed.retire && <button type="button" className="ghost danger" onClick={() => { if (window.confirm(`Retire ${s.name}? Mappings and secrets stay as they are.`)) act("DELETE", "", undefined, "Schedule retired.").then(onClose); }}>Retire</button>}
      </div>}>
      <dl className="kv">
        <dt>Runs</dt><dd>every {every(s.interval_minutes)}</dd>
        <dt>Next run</dt><dd>{s.status === "active" ? <Time iso={s.next_run_at} /> : "—"}</dd>
        <dt>Created by</dt><dd>{s.requested_by ?? "—"}</dd>
        <dt>Namespaces</dt><dd>{s.namespaces.join(", ")}</dd>
        <dt>Hash</dt><dd><code>{s.schedule_hash.slice(0, 12)}</code></dd>
      </dl>
      {s.paused_reason && <div className="alert warn small">{s.paused_reason}</div>}
      {s.stale_mappings.length > 0 && <div className="alert warn small stack">Edited since approval: {s.stale_mappings.join(", ")}. The schedule does not run until it is refreshed and approved again.
        {s.allowed.edit && <div><button type="button" className="small" onClick={() => act("PATCH", "", { version: s.version, refresh: true }, "Refreshed. It waits for approval again.")}>Refresh and ask for approval</button></div>}</div>}
      {s.needs_fresh_approval && s.policy === "scheduled" && <div className="alert warn small">This schedule was held by the manual-only policy. Its earlier approval no longer counts: it runs again only after someone approves its current version.</div>}
      {!s.allowed.approve && s.allowed.reject && <div className="row end"><button type="button" className="ghost danger" onClick={() => act("POST", "/decision", { approve: false, schedule_hash: s.schedule_hash, comment })}>Reject</button></div>}
      {s.allowed.approve && <section className="card stack">
        <strong>Approve this schedule</strong>
        <p className="small muted">Approving lets every run write one-sided changes for these {s.mappings.length} mappings, in each mapping's direction, without asking again. Conflicts and deletions still wait for a person.</p>
        <input aria-label="Approval comment" placeholder="Comment" value={comment} onChange={(e) => setComment(e.target.value)} />
        <div className="row end"><button type="button" className="ghost danger" onClick={() => act("POST", "/decision", { approve: false, schedule_hash: s.schedule_hash, comment })}>Reject</button>
          <button type="button" onClick={() => act("POST", "/decision", { approve: true, schedule_hash: s.schedule_hash, comment }, "Approved. The first run starts within seconds.")}>Approve</button></div>
      </section>}
      {s.last_run_at && <section className="stack">
        <strong>Last run <Time iso={s.last_run_at} /></strong>
        <span className="small">{s.last_outcome.status ?? "—"} · {s.last_outcome.written ?? 0} written{s.last_plan_id && <> · <Link to={`/secrets/plans/${s.last_plan_id}?env=${env}`}>open the plan</Link></>}</span>
        {s.last_outcome.error && <div className="alert error small">{s.last_outcome.error}</div>}
        {review.length > 0 && <div className="stack">
          <span className="small">Waiting for a person:</span>
          <ul className="small state-list">{review.map((r) => <li key={r.mapping_id}><span className="mono">{r.secret}</span>: {r.why}</li>)}</ul>
          <div><button type="button" className="small" onClick={() => setPlanFor(review.map((r) => r.mapping_id))}>Compare these for review</button></div>
        </div>}
      </section>}
      <section className="stack"><strong>Mappings</strong>
        <ul className="small">{s.mappings.map((m) => <li key={m.id}><span className="mono">{m.secret}</span> ⇄ <span className="mono">{m.aws_secret_name}</span> · {DIRECTION[m.direction]} {m.last_status && <Status s={m.last_status} />}</li>)}</ul></section>
      {s.allowed.edit && <section className="stack"><strong>Interval</strong>
        <IntervalPicker value={minutes} onChange={setMinutes} />
        <div><button type="button" className="ghost small" disabled={minutes === s.interval_minutes} onClick={() => act("PATCH", "", { version: s.version, interval_minutes: minutes }, "Saved. A new interval needs a new approval.")}>Save interval</button></div>
      </section>}
      {s.approvals.length > 0 && <section className="small stack"><strong>Decisions</strong>{s.approvals.map((a, n) => <span key={n}>{a.decision} by {a.by} <Time iso={a.at} />{a.comment && <>: {a.comment}</>}{!a.current && " (an earlier version)"}</span>)}</section>}
      <ErrorBox error={error} />
      {planFor && <PlanDialog env={env} kind="sync" ids={planFor} onClose={() => setPlanFor(undefined)} onCreated={(id) => nav(`/secrets/plans/${id}?env=${env}`)} />}
    </Drawer>
  );
}
