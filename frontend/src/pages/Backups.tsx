import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";
import { useAuth } from "../auth";
import { Badge, DataTable, ErrorBox, fmtBytes, Loading, PageHead, Time, useInterval, useLoad, type Env } from "../components";
import { Dialog, Drawer, Empty, Identifier, useFeedback } from "../ui";

const PHASE: Record<string, "ok" | "warn" | "bad" | "info"> = { Completed: "ok", PartiallyFailed: "warn", Failed: "bad", FailedValidation: "bad", InProgress: "info" };
const DB_STATUS: Record<string, "ok" | "bad" | "info"> = { success: "ok", failed: "bad", queued: "info", running: "info" };
const REFS = [["access_key_id", "Variable holding the R2 access key id"], ["secret_access_key", "Variable holding the R2 secret access key"], ["sse_key", "Variable holding the encryption key (32 bytes, base64)"], ["repo_password", "Variable holding the volume repository password"]];

function parseList(text: string): string[] { return text.split(/[\s,]+/).filter(Boolean); }

function PlanButton({ env, path, label, title, fields, build, ghost, approved }: { env: string; path: string; label: string; title: string; fields?: React.ReactNode; build: () => Record<string, unknown>; ghost?: boolean; approved?: boolean }) {
  const nav = useNavigate();
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("");
  const [error, setError] = useState<unknown>();
  const { toast } = useFeedback();
  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      const r = await api<{ plan_id: string }>(`/backups/${env}/${path}`, { method: "POST", body: { ...build(), reason } as any });
      if (approved) nav(`/actions/${r.plan_id}`);
      else { setOpen(false); setReason(""); toast("Started. Progress shows on this page."); }
    } catch (err) { setError(err); }
  }
  return (
    <>
      <button type="button" className={ghost ? "ghost small" : ""} onClick={() => setOpen(true)}>{label}</button>
      {open && <Dialog title={title} onClose={() => setOpen(false)}>
        <form className="stack" onSubmit={submit}>
          {fields}
          <label>Reason<textarea required minLength={5} rows={2} value={reason} onChange={(e) => setReason(e.target.value)} /></label>
          <ErrorBox error={error} />
          <div className="small muted">{approved ? "This creates an action plan. You review it next, and nothing runs until it is approved and executed." : "This starts right away, with no approval, and is recorded in the audit log."}</div>
          <div className="row end"><button>{approved ? "Review plan" : "Start"}</button></div>
        </form>
      </Dialog>}
    </>
  );
}

function size(bytes?: number | null): string { return bytes == null ? "—" : fmtBytes(bytes); }

function duration(seconds?: number | null): string {
  if (seconds == null) return "—";
  return seconds < 60 ? `${seconds} s` : `${Math.floor(seconds / 60)} min ${seconds % 60} s`;
}

/** Metadata of one cluster backup and the objects inside it: names and counts, never object data. */
function BackupDetails({ env, name, onClose }: { env: string; name: string; onClose: () => void }) {
  const d = useLoad(() => api<any>(`/backups/${env}/items/${name}`), [env, name]);
  const [search, setSearch] = useState("");
  const b = d.data;
  const c = b?.contents;
  const q = search.trim().toLowerCase();
  const found = !c || !q ? [] : c.items.filter((i: any) => `${i.kind} ${i.namespace} ${i.name}`.toLowerCase().includes(q)).slice(0, 200);
  return (
    <Drawer title={`Backup ${name}`} onClose={onClose}>
      <Loading load={d} rows={6} />
      {b && <div className="stack">
        <dl className="kv">
          <dt>Status</dt><dd><Badge kind={PHASE[b.phase] ?? "neutral"}>{b.phase}</Badge> {b.errors} errors, {b.warnings} warnings</dd>
          <dt>Started</dt><dd><Time iso={b.started} /></dd>
          <dt>Took</dt><dd>{duration(b.duration_seconds)}</dd>
          <dt>Expires</dt><dd><Time iso={b.expires} /> (kept {b.ttl})</dd>
          <dt>Started by</dt><dd>{b.schedule ? `schedule ${b.schedule}` : "a person, through a plan"}</dd>
          <dt>Items</dt><dd>{b.items ?? "—"}{b.items_total ? ` of ${b.items_total}` : ""}</dd>
          <dt>Namespaces</dt><dd className="mono small">{b.namespaces.join(", ")}</dd>
          <dt>Excluded namespaces</dt><dd className="mono small">{b.excluded_namespaces.join(", ") || "none"}</dd>
          <dt>Resources</dt><dd className="mono small">{b.included_resources.join(", ")}{b.excluded_resources.length ? `, except ${b.excluded_resources.join(", ")}` : ""}</dd>
          <dt>Cluster objects</dt><dd>{b.cluster_resources === false ? "not included" : "included"}</dd>
          <dt>Volume data</dt><dd>{b.volumes ? `file backup: ${b.fs_backups.completed} of ${b.fs_backups.attempted} done` : "not included"}</dd>
          <dt>Stored in</dt><dd className="mono small">{b.storage_location}</dd>
          <dt>Format</dt><dd className="mono small">{b.format_version ?? "—"}</dd>
          <dt>Backup UID</dt><dd className="mono small">{b.uid}</dd>
        </dl>
        {b.failure && <div className="alert error small">{b.failure}</div>}
        {!c && b.contents_note && <div className="alert warn small">{b.contents_note}</div>}
        {c && <>
          <h3>What is inside ({c.total} objects)</h3>
          <DataTable label="Objects by kind" rows={c.kinds as any[]} rowKey={(k) => k.kind} cols={[
            { key: "k", head: "Kind", primary: true, cell: (k) => k.kind }, { key: "c", head: "Objects", cell: (k) => k.count }]} />
          <h3>By namespace</h3>
          <DataTable label="Objects by namespace" rows={c.namespaces as any[]} rowKey={(n) => n.namespace} cols={[
            { key: "n", head: "Namespace", primary: true, cell: (n) => <span className="mono small">{n.namespace}</span> },
            { key: "c", head: "Objects", cell: (n) => n.count },
            { key: "k", head: "Largest kinds", wide: true, cell: (n) => <span className="small">{Object.entries(n.kinds).sort((a: any, b: any) => b[1] - a[1]).slice(0, 4).map(([k, v]) => `${k} ${v}`).join(", ")}</span> }]} />
          <label>Find an object<input value={search} placeholder="checkout, Secret, moks…" onChange={(e) => setSearch(e.target.value)} /></label>
          {q && <div className="small">{found.length ? found.map((i: any) => <div key={`${i.kind}/${i.namespace}/${i.name}`} className="mono">{i.kind} {i.namespace ? `${i.namespace}/` : ""}{i.name}</div>) : "Nothing matches."}</div>}
          {c.truncated && <div className="small muted">Search covers the first {c.items.length} objects.</div>}
          {Object.entries({ ...c.errors }).length > 0 && <div className="alert error small mono">{JSON.stringify(c.errors)}</div>}
          {Object.entries({ ...c.warnings }).length > 0 && <div className="alert warn small mono">{JSON.stringify(c.warnings)}</div>}
        </>}
      </div>}
    </Drawer>
  );
}

function DatabaseDetails({ b, onClose }: { b: any; onClose: () => void }) {
  const inv = b.inventory ?? {};
  return (
    <Drawer title={`${b.namespace}/${b.workload}`} onClose={onClose}>
      <div className="stack">
        <dl className="kv">
          <dt>Status</dt><dd><Badge kind={DB_STATUS[b.status] ?? "neutral"}>{b.status}</Badge></dd>
          <dt>Started</dt><dd><Time iso={b.started_at} /></dd>
          <dt>Finished</dt><dd><Time iso={b.finished_at} /></dd>
          <dt>Started by</dt><dd>{b.trigger === "schedule" ? "schedule" : <>a person{b.plan_id && <> (<a href={`${import.meta.env.BASE_URL}actions/${b.plan_id}`}>plan</a>)</>}</>}</dd>
          <dt>Engine</dt><dd>{b.engine} {inv.version}</dd>
          <dt>Image</dt><dd className="mono small">{b.image ?? "—"}</dd>
          <dt>Pod</dt><dd className="mono small">{b.pod ?? "—"}</dd>
          <dt>Dump size</dt><dd>{size(b.dump_bytes)}</dd>
          <dt>Stored size</dt><dd>{size(b.bytes)}, encrypted, kept until removed</dd>
          <dt>Stored as</dt><dd className="mono small">{b.object_key ?? "—"}</dd>
          <dt>SHA-256</dt><dd className="mono small">{b.sha256 ?? "—"}{b.sha256 && " (matched when read back)"}</dd>
        </dl>
        {b.error && <div className="alert error small">{b.error}</div>}
        {inv.error && <div className="alert warn small">{inv.error}</div>}
        {inv.not_included?.length > 0 && <div className="alert warn small">Not in this backup: {inv.not_included.join(", ")}</div>}
        {inv.databases?.map((d: any) => <div key={d.name} className="stack">
          <h3>{d.name} ({size(d.bytes)}, {d.tables.length} {b.engine === "mongodb" ? "collections" : "tables"})</h3>
          <DataTable label={`Contents of ${d.name}`} rows={d.tables as any[]} rowKey={(t) => `${t.schema ?? ""}.${t.name}`} empty={<Empty title="Empty." />} cols={[
            { key: "n", head: "Name", primary: true, cell: (t) => <span className="mono small">{t.schema && t.schema !== "public" ? `${t.schema}.` : ""}{t.name}</span> },
            { key: "r", head: "Rows (estimate)", cell: (t) => t.rows ?? "—" },
            { key: "s", head: "Size", cell: (t) => size(t.bytes) }]} />
        </div>)}
      </div>
    </Drawer>
  );
}

function Databases({ env }: { env: string }) {
  const { can } = useAuth();
  const s = useLoad(() => (env ? api<any>(`/backups/${env}/databases`) : Promise.resolve(null)), [env]);
  useInterval(() => { if (!s.loading) s.reload(); }, 15000);
  const [open, setOpen] = useState<any>(null);
  const [hours, setHours] = useState(24);
  const [paused, setPaused] = useState(false);
  const d = s.data;
  if (!d) return <div className="card"><h2>Databases</h2><Loading load={s} rows={3} /></div>;
  const last = (t: any) => d.backups.find((b: any) => b.namespace === t.namespace && b.workload === t.workload);
  const schedule = (t: any) => d.schedules.find((x: any) => x.namespace === t.namespace && x.workload === t.workload);
  return (
    <div className="card stack"><h2>Databases ({d.targets.length})</h2>
      <p className="small muted" style={{ margin: 0 }}>Velero cannot copy these volumes, so MOKS runs each database's own dump tool, encrypts the result and stores it in R2. Backups are kept until someone removes them.</p>
      {d.findings.map((f: any) => <div key={f.code} className={`alert small ${f.severity === "bad" ? "error" : "warn"}`}>{f.text}</div>)}
      <DataTable label="Databases" rows={d.targets as any[]} rowKey={(t) => `${t.namespace}/${t.workload}`} empty={<Empty title="No PostgreSQL or MongoDB found in this cluster." />} cols={[
        { key: "n", head: "Database", primary: true, cell: (t) => <span className="mono small">{t.namespace}/{t.workload}</span> },
        { key: "e", head: "Engine", cell: (t) => t.engine },
        { key: "l", head: "Last backup", cell: (t) => { const b = last(t); return b ? <><Badge kind={DB_STATUS[b.status] ?? "neutral"}>{b.status}</Badge> <Time iso={b.finished_at ?? b.created_at} /></> : "never"; } },
        { key: "s", head: "Schedule", cell: (t) => { const x = schedule(t); return !x ? "none" : x.paused ? "paused" : `every ${x.interval_hours} h`; } },
        { key: "a", head: "Actions", cell: (t) => (can("action.request") && t.enabled ? <div className="row">
          <PlanButton env={env} path="databases/backup" label="Back up now" title={`Back up ${t.namespace}/${t.workload}`} ghost build={() => ({ namespace: t.namespace, workload: t.workload, kind: t.kind })} />
          <PlanButton env={env} path="databases/schedule" label="Schedule" title={`Schedule ${t.namespace}/${t.workload}`} ghost
            fields={<><label>Every (hours)<input type="number" min={1} max={720} value={hours} onChange={(e) => setHours(Number(e.target.value))} /></label>
              <label className="inline"><input type="checkbox" checked={paused} onChange={(e) => setPaused(e.target.checked)} /> Paused</label></>}
            build={() => ({ namespace: t.namespace, workload: t.workload, kind: t.kind, interval_hours: hours, paused })} /></div> : "") }]} />
      <h3>History</h3>
      <DataTable label="Database backups" rows={d.backups as any[]} rowKey={(b) => b.id} empty={<Empty title="No database backups yet." />} cols={[
        { key: "n", head: "Database", primary: true, cell: (b) => <button type="button" className="ghost small" onClick={() => setOpen(b)}>{b.namespace}/{b.workload}</button> },
        { key: "p", head: "Status", status: true, cell: (b) => <Badge kind={DB_STATUS[b.status] ?? "neutral"}>{b.status}</Badge> },
        { key: "s", head: "Started", cell: (b) => <Time iso={b.started_at ?? b.created_at} /> },
        { key: "z", head: "Size", cell: (b) => size(b.bytes) },
        { key: "i", head: "Inside", wide: true, cell: (b) => (b.inventory?.table_count != null ? `${b.inventory.table_count} ${b.engine === "mongodb" ? "collections" : "tables"}, about ${b.inventory.row_estimate} rows` : b.error ? <span className="small muted">{b.error}</span> : "—") }]} />
      {d.not_covered.length > 0 && <div className="small muted">Not covered: {d.not_covered.map((x: any) => `${x.namespace}/${x.workload} (${x.service})`).join(", ")}.</div>}
      {open && <DatabaseDetails b={open} onClose={() => setOpen(null)} />}
    </div>
  );
}

export function Backups() {
  const { can } = useAuth();
  const envs = useLoad(() => api<Env[]>("/environments"), []);
  const [picked, setPicked] = useState("");
  const env = picked || envs.data?.[0]?.name || "";
  const s = useLoad(() => (env ? api<any>(`/backups/${env}`) : Promise.resolve(null)), [env]);
  useInterval(() => { if (!s.loading) s.reload(); }, 15000);
  const [ns, setNs] = useState("*");
  const [volumes, setVolumes] = useState(true);
  const [days, setDays] = useState(3650);
  const [cron, setCron] = useState("0 2 * * *");
  const [policy, setPolicy] = useState("none");
  const [pre, setPre] = useState(true);
  const [details, setDetails] = useState("");
  const namespaces = parseList(ns);
  const d = s.data;
  const scope = <label>Namespaces (space separated, * for all)<input value={ns} onChange={(e) => setNs(e.target.value)} /></label>;
  const data = <>{d?.volumes_supported !== false && <label className="inline"><input type="checkbox" checked={volumes} onChange={(e) => setVolumes(e.target.checked)} /> Include PersistentVolume data</label>}
    <label>Keep for (days)<input type="number" min={1} max={3650} value={days} onChange={(e) => setDays(Number(e.target.value))} /></label></>;
  const q = encodeURIComponent(`Review cluster backups: are they healthy, complete and restorable? Explain any failure.`);
  return (
    <>
      <PageHead title="Backups" sub="Cluster objects including Secrets, and volume data where the cluster supports it, stored encrypted in Cloudflare R2 by Velero.">
        <label>Environment<select value={env} onChange={(e) => setPicked(e.target.value)}>{envs.data?.map((e) => <option key={e.name} value={e.name}>{e.display_name}{e.is_production ? " (production)" : ""}</option>)}</select></label>
      </PageHead>
      <Loading load={s} rows={5} />
      <ErrorBox error={envs.error} />
      {d && <div className="stack">
        {d.findings.map((f: any) => <div key={f.code} className={`alert ${f.severity === "bad" ? "error" : "warn"}`}>{f.text}</div>)}
        {!d.findings.length && <div className="alert info">No problems found. A backup is only proven by a test restore.</div>}
        <div className="row">
          {can("action.request") && d.installed && d.store && <PlanButton env={env} path="configure" label={d.locations.length ? "Update R2 connection" : "Connect to R2"} title="Connect this cluster to R2" build={() => ({})} ghost />}
          {can("action.request") && d.installed && <PlanButton env={env} path="backup" label="Back up now" title="Back up this cluster" fields={<>{scope}{data}</>} build={() => ({ included_namespaces: namespaces, include_volumes: volumes, ttl_days: days })} />}
          {can("action.request") && d.installed && <PlanButton env={env} path="schedule" label="Set schedule" title="Backup schedule" ghost fields={<>{scope}{data}<label>Cron (UTC)<input value={cron} onChange={(e) => setCron(e.target.value)} /></label></>}
            build={() => ({ name: "daily", cron, included_namespaces: namespaces, include_volumes: volumes, ttl_days: days })} />}
          {can("investigation.run") && <a className="btn ghost" href={`${import.meta.env.BASE_URL}investigate?env=${env}&ns=*&q=${q}`}>Ask MOKS to review</a>}
        </div>
        <div className="card"><h2>Backups ({d.backups.length})</h2>
          <DataTable label="Backups" rows={d.backups as any[]} rowKey={(b) => b.name} empty={<Empty title="No backups in this cluster yet.">Back up now, or set a schedule.</Empty>} cols={[
            { key: "n", head: "Name", primary: true, cell: (b) => <><button type="button" className="ghost small" onClick={() => setDetails(b.name)}>{b.name}</button>{b.failure && <span className="small muted">{b.failure}</span>}</> },
            { key: "p", head: "Status", status: true, cell: (b) => <Badge kind={PHASE[b.phase] ?? "neutral"}>{b.phase}</Badge> },
            { key: "s", head: "Started", cell: (b) => <Time iso={b.started} /> },
            { key: "x", head: "Expires", wide: true, cell: (b) => <Time iso={b.expires} /> },
            { key: "i", head: "Items", cell: (b) => <>{b.items ?? "—"}{b.items_total ? ` / ${b.items_total}` : ""}</>, meta: (b) => `${b.items ?? "—"} items` },
            { key: "v", head: "Volumes", wide: true, cell: (b) => (b.volumes ? "yes" : "no") },
            { key: "e", head: "Errors", cell: (b) => `${b.errors} / ${b.warnings} warnings`, meta: (b) => `${b.errors} errors, ${b.warnings} warnings` },
            { key: "a", head: "Restore", cell: (b) => (can("action.request") && ["Completed", "PartiallyFailed"].includes(b.phase) ? <PlanButton env={env} path="restore" label="Restore" title={`Restore ${b.name}`} ghost approved
                fields={<>{scope}<label>If an object already exists<select value={policy} onChange={(e) => setPolicy(e.target.value)}><option value="none">Keep what is there (safe)</option><option value="update">Overwrite it with the backup</option></select></label>
                  <label className="inline"><input type="checkbox" checked={pre} onChange={(e) => setPre(e.target.checked)} /> Take a backup first</label></>}
                build={() => ({ backup: b.name, included_namespaces: namespaces, existing_resource_policy: policy, pre_restore_backup: pre, restore_volumes: true })} /> : "") },
          ]} />
        </div>
        {details && <BackupDetails env={env} name={details} onClose={() => setDetails("")} />}
        <Databases env={env} />
        {d.schedules.length > 0 && <div className="card"><h2>Schedules</h2><DataTable label="Schedules" rows={d.schedules as any[]} rowKey={(x) => x.name} cols={[
          { key: "n", head: "Name", primary: true, cell: (x) => <Identifier name={x.name} /> },
          { key: "st", head: "State", status: true, cell: (x) => (x.paused ? <Badge kind="warn">paused</Badge> : <Badge kind="ok">active</Badge>) },
          { key: "c", head: "Cron", cell: (x) => <span className="mono small">{x.cron}</span> },
          { key: "l", head: "Last backup", cell: (x) => <Time iso={x.last_backup} /> }]} /></div>}
        {d.restores.length > 0 && <div className="card"><h2>Restores</h2><DataTable label="Restores" rows={d.restores as any[]} rowKey={(r) => r.name} cols={[
          { key: "n", head: "Name", primary: true, cell: (r) => <Identifier name={r.name} /> },
          { key: "p", head: "Status", status: true, cell: (r) => <Badge kind={PHASE[r.phase] ?? "neutral"}>{r.phase}</Badge> },
          { key: "b", head: "Backup", cell: (r) => <Identifier name={r.backup} what="backup name" /> },
          { key: "s", head: "Started", cell: (r) => <Time iso={r.started} /> },
          { key: "e", head: "Errors", cell: (r) => `${r.errors} / ${r.warnings} warnings` }]} /></div>}
      </div>}
    </>
  );
}

export function BackupStorage() {
  const s = useLoad(() => api<any>("/backups/store"), []);
  const [form, setForm] = useState<any>(null);
  const [error, setError] = useState<unknown>();
  const [busy, setBusy] = useState(false);
  const store = s.data?.store;
  const f = form ?? { account_id: store?.account_id ?? "", bucket: store?.bucket ?? "", prefix: store?.prefix ?? "clusters", jurisdiction: store?.jurisdiction ?? "default",
    names: Object.fromEntries(REFS.map(([k]) => [k, store?.credentials?.[k]?.name ?? `MOKS_CRED_R2_${k.toUpperCase()}`])) };
  const set = (patch: any) => setForm({ ...f, ...patch });
  async function run(fn: () => Promise<any>) {
    setBusy(true);
    setError(null);
    try { await fn(); s.reload(); } catch (e) { setError(e); }
    setBusy(false);
  }
  const save = (e: FormEvent) => {
    e.preventDefault();
    run(() => api("/backups/store", { method: "PUT", body: { account_id: f.account_id, bucket: f.bucket, prefix: f.prefix, jurisdiction: f.jurisdiction,
      credentials: Object.fromEntries(REFS.map(([k]) => [k, { type: "env", name: f.names[k] }])) } })).then(() => setForm(null));
  };
  return (
    <>
      <PageHead title="Backup storage" sub="Cloudflare R2 settings. Credentials are names of mounted Secrets, never values, so nothing secret is stored in the database." />
      <ErrorBox error={s.error ?? error} />
      <form className="card stack" onSubmit={save}>
        <div className="filters">
          <label>Account ID<input required pattern="[a-f0-9]{32}" value={f.account_id} onChange={(e) => set({ account_id: e.target.value })} /></label>
          <label>Bucket<input required value={f.bucket} onChange={(e) => set({ bucket: e.target.value })} /></label>
          <label>Prefix<input value={f.prefix} onChange={(e) => set({ prefix: e.target.value })} /></label>
          <label>Jurisdiction<select value={f.jurisdiction} onChange={(e) => set({ jurisdiction: e.target.value })}><option value="default">Default</option><option value="eu">EU</option></select></label>
        </div>
        <h2>Credential references</h2>
        <p className="small muted" style={{ margin: 0 }}>Enter variable names here, not keys. The values go in the Kubernetes Secret, and this page never accepts them.</p>
        <div className="filters">{REFS.map(([k, label]) => <label key={k}>{label}<input required pattern="MOKS_CRED_[A-Z0-9_]+" title="The name of a variable in the Secret, such as MOKS_CRED_R2_ACCESS_KEY_ID. Never the key itself." className="mono" value={f.names[k]} onChange={(e) => set({ names: { ...f.names, [k]: e.target.value } })} />
          {store && <span className="small">{store.credential_status[k].available ? "available here" : "not set here"}</span>}</label>)}</div>
        <div className="row"><button disabled={busy}>Save</button>
          {store && <button type="button" className="ghost" disabled={busy} onClick={() => run(() => api("/backups/store/test", { method: "POST" }))}>Test write, read and delete</button>}
          {store?.last_test_at && <span className="small">{store.last_test_ok ? <Badge kind="ok">test passed</Badge> : <Badge kind="bad">test failed</Badge>} <Time iso={store.last_test_at} /> {store.last_test_error}</span>}</div>
      </form>
    </>
  );
}
