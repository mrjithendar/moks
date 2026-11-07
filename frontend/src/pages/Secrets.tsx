import { useEffect, useMemo, useState, type FormEvent, type ReactNode } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { api, ApiError, qs } from "../api";
import { Badge, DataTable, ErrorBox, Loading, PageHead, Time, useInterval, useLoad, type Col } from "../components";
import { FilterPanel, useUrlFilters } from "../filters";
import { StateView } from "../states";
import { Dialog, Empty, Identifier, useFeedback } from "../ui";

/** Kubernetes <-> AWS Secrets Manager: inventories, mappings, reviewed plans and history. Nothing on this page ever
 *  shows a secret value: the API returns names, versions, key names (with secrets.compare) and masked fingerprints. */

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
  cluster_wide: boolean; can: Record<string, boolean>;
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
  const tabs: [string, string][] = [["inventory", "Inventory"], ["mappings", "Mappings"], ["plans", "Plans"], ["history", "History"]];
  return (
    <>
      <PageHead title="Secrets sync" sub="Discover, compare and manually sync Kubernetes Secrets with AWS Secrets Manager. Every write is reviewed and approved; nothing syncs on its own.">
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
        {scope.data && !scope.data.aws.length && <StateView kind="empty" title="No AWS Secrets Manager connector" why={`${env} has no aws_secrets connector.`} next={<>An administrator adds one on <Link to="/admin/connectors">Connectors</Link>.</>} />}
        {scope.data && f.tab === "inventory" && <Inventory env={env} scope={scope.data} />}
        {scope.data && f.tab === "mappings" && <Mappings env={env} scope={scope.data} />}
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
  const inv = useLoad(() => api<{ items: Row[]; truncated: boolean; hidden: number; scopes: { scope: string; complete: boolean; count: number; error: string | null; discovered_at: string | null }[] }>(
    `/secrets/${env}/inventory${qs({ side: f.side, q: f.q, namespace: f.ns, type: f.type, owner: f.owner, status: f.status, prefix: f.prefix })}`), [env, f.side, f.q, f.ns, f.type, f.owner, f.status, f.prefix]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [discovering, setDiscovering] = useState(false);
  const [proposing, setProposing] = useState(false);
  useEffect(() => setSelected(new Set()), [env, f.side]);
  const rows = inv.data?.items ?? [];
  const namespaces = [...new Set(scope.kubernetes.flatMap((k) => k.namespaces))].sort();
  const toggle = (id: string) => setSelected((s) => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n; });
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
    f.type && { key: "type", label: `Type: ${f.type}`, onRemove: () => setF({ type: "" }) }, f.owner && { key: "owner", label: `Owner: ${f.owner}`, onRemove: () => setF({ owner: "" }) },
    f.status && { key: "status", label: `Status: ${STATUS_LABEL[f.status] ?? f.status}`, onRemove: () => setF({ status: "" }) },
    f.prefix && { key: "prefix", label: `Prefix: ${f.prefix}`, onRemove: () => setF({ prefix: "" }) }].filter(Boolean) as { key: string; label: string; onRemove: () => void }[];
  const partial = (inv.data?.scopes ?? []).filter((s) => !s.complete);
  return (
    <section className="stack">
      <FilterPanel chips={chips} onClear={clear}
        search={<input type="search" aria-label="Search secret names" placeholder="Search names…" defaultValue={f.q} onKeyDown={(e) => { if (e.key === "Enter") setF({ q: (e.target as HTMLInputElement).value }); }} />}
        actions={<>
          {scope.can.discover && <button type="button" className="ghost" onClick={() => setDiscovering(true)}>Discover</button>}
          {scope.can.map && selected.size > 0 && <button type="button" onClick={() => setProposing(true)}>Map {selected.size} selected</button>}
        </>}
        advanced={<div className="row wrap">
          <label>Type<input value={f.type} onChange={(e) => setF({ type: e.target.value })} placeholder="Opaque" /></label>
          <label>Owner<select value={f.owner} onChange={(e) => setF({ owner: e.target.value })}><option value="">Any</option><option value="none">None or MOKS</option>
            {["external-secrets", "secrets-store-csi", "cert-manager", "helm", "argocd", "flux", "sealed-secrets", "aws-rotation"].map((o) => <option key={o}>{o}</option>)}</select></label>
          <label>Status<select value={f.status} onChange={(e) => setF({ status: e.target.value })}><option value="">Any</option>{Object.entries(STATUS_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select></label>
          {f.side === "aws" && <label>Name prefix<input value={f.prefix} onChange={(e) => setF({ prefix: e.target.value })} placeholder="moks/" /></label>}
        </div>}>
        <label>Source<select value={f.side} onChange={(e) => setF({ side: e.target.value })}><option value="kubernetes">Kubernetes</option><option value="aws">AWS Secrets Manager</option></select></label>
        {f.side === "kubernetes" && <label>Namespace<select value={f.ns} onChange={(e) => setF({ ns: e.target.value })}><option value="">All you can see</option>{namespaces.map((n) => <option key={n}>{n}</option>)}</select></label>}
      </FilterPanel>
      {inv.error && denied(inv.error) ? <StateView kind="denied" why={(inv.error as Error).message} /> : <Loading load={inv} />}
      {partial.length > 0 && <StateView kind="partial" why="The last discovery could not read everything; rows it did not reach are from earlier runs."
        next="Fix the permission or retry Discover."><ul className="small state-list">{partial.map((s) => <li key={s.scope}><span className="mono">{s.scope}</span>: {s.error ?? "incomplete"}</li>)}</ul></StateView>}
      {(inv.data?.hidden ?? 0) > 0 && <p className="small muted">{inv.data!.hidden} secrets are outside your namespaces and not listed.</p>}
      {inv.data?.truncated && <p className="small muted">Showing the first {rows.length}. Filter to narrow the list.</p>}
      {inv.data && <DataTable label={f.side === "kubernetes" ? "Kubernetes Secrets" : "AWS secrets"} rows={rows} cols={cols} rowKey={(r) => r.id}
        empty={<Empty title={inv.data.scopes.length ? "No secrets match." : "Nothing discovered yet."}>{!inv.data.scopes.length && "Use Discover to list secret names. Discovery reads metadata and never writes."}</Empty>} />}
      {discovering && <DiscoverDialog env={env} scope={scope} onClose={() => setDiscovering(false)} onDone={() => { setDiscovering(false); inv.reload(); }} />}
      {proposing && <MappingEditor env={env} scope={scope} ids={[...selected]} onClose={() => setProposing(false)} onDone={() => { setProposing(false); setSelected(new Set()); inv.reload(); }} />}
    </section>
  );
}

function DiscoverDialog({ env, scope, onClose, onDone }: { env: string; scope: ScopeInfo; onClose: () => void; onDone: () => void }) {
  const [kube, setKube] = useState(scope.kubernetes[0]?.id ?? "");
  const cluster = scope.kubernetes.find((k) => k.id === kube);
  const [ns, setNs] = useState<string[]>([]);
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
        {scope.kubernetes.length > 0 && <label>Cluster<select value={kube} onChange={(e) => { setKube(e.target.value); setNs([]); }}>{scope.kubernetes.map((k) => <option key={k.id} value={k.id} disabled={!k.writable}>{k.name}{k.writable ? "" : " (no executor target)"}</option>)}</select></label>}
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

function Mappings({ env, scope }: { env: string; scope: ScopeInfo }) {
  const nav = useNavigate();
  const list = useLoad(() => api<{ items: Mapping[] }>(`/secrets/${env}/mappings`), [env]);
  const [q, setQ] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [planKind, setPlanKind] = useState<"sync" | "delete">();
  const [editing, setEditing] = useState<Mapping>();
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
          <button type="button" disabled={!selected.size} onClick={() => setPlanKind("sync")}>Compare {selected.size || ""} selected</button>
          <button type="button" className="ghost danger" disabled={!selected.size} onClick={() => setPlanKind("delete")}>Propose deletion</button>
        </>}
      </div>
      <Loading load={list} />
      {list.data && <DataTable label="Secret mappings" rows={rows} cols={cols} rowKey={(m) => m.id}
        menu={scope.can.map ? (m) => ({ label: `Actions for ${m.secret_name}`, items: [{ label: "Edit", run: () => setEditing(m) }, { label: "Retire", run: () => retire(m), danger: true }] }) : undefined}
        empty={<Empty title="No mappings yet.">Select discovered secrets on the Inventory tab and map them.</Empty>} />}
      {planKind && <PlanDialog env={env} kind={planKind} ids={[...selected]} onClose={() => setPlanKind(undefined)} onCreated={(id) => nav(`/secrets/plans/${id}?env=${env}`)} />}
      {editing && <EditMapping env={env} m={editing} onClose={() => setEditing(undefined)} onDone={() => { setEditing(undefined); list.reload(); }} />}
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

function PlanDialog({ env, kind, ids, onClose, onCreated, rollback }: { env: string; kind: "sync" | "delete" | "rollback"; ids: string[]; onClose: () => void; onCreated: (id: string) => void; rollback?: Record<string, any> }) {
  const [reason, setReason] = useState("");
  const [targets, setTargets] = useState<string[]>(["aws"]);
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
    { key: "what", head: "Plan", primary: true, cell: (p) => <span>{p.kind} · {p.mapping_count} mapping{p.mapping_count === 1 ? "" : "s"}</span> },
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
