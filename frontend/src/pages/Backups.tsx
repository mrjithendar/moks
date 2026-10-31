import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";
import { useAuth } from "../auth";
import { Badge, DataTable, ErrorBox, Loading, PageHead, Time, useInterval, useLoad, type Env } from "../components";
import { Dialog, Empty, Identifier } from "../ui";

const PHASE: Record<string, "ok" | "warn" | "bad" | "info"> = { Completed: "ok", PartiallyFailed: "warn", Failed: "bad", FailedValidation: "bad", InProgress: "info" };
const REFS = [["access_key_id", "Variable holding the R2 access key id"], ["secret_access_key", "Variable holding the R2 secret access key"], ["sse_key", "Variable holding the encryption key (32 bytes, base64)"], ["repo_password", "Variable holding the volume repository password"]];

function parseList(text: string): string[] { return text.split(/[\s,]+/).filter(Boolean); }

function PlanButton({ env, path, label, title, fields, build, ghost }: { env: string; path: string; label: string; title: string; fields?: React.ReactNode; build: () => Record<string, unknown>; ghost?: boolean }) {
  const nav = useNavigate();
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("");
  const [error, setError] = useState<unknown>();
  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      const r = await api<{ plan_id: string }>(`/backups/${env}/${path}`, { method: "POST", body: { ...build(), reason } as any });
      nav(`/actions/${r.plan_id}`);
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
          <div className="small muted">This creates an action plan. You review it next, and nothing runs until it is approved and executed.</div>
          <div className="row end"><button>Review plan</button></div>
        </form>
      </Dialog>}
    </>
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
            { key: "n", head: "Name", primary: true, cell: (b) => <><Identifier name={b.name} />{b.failure && <span className="small muted">{b.failure}</span>}</> },
            { key: "p", head: "Status", status: true, cell: (b) => <Badge kind={PHASE[b.phase] ?? "neutral"}>{b.phase}</Badge> },
            { key: "s", head: "Started", cell: (b) => <Time iso={b.started} /> },
            { key: "x", head: "Expires", wide: true, cell: (b) => <Time iso={b.expires} /> },
            { key: "i", head: "Items", cell: (b) => <>{b.items ?? "—"}{b.items_total ? ` / ${b.items_total}` : ""}</>, meta: (b) => `${b.items ?? "—"} items` },
            { key: "v", head: "Volumes", wide: true, cell: (b) => (b.volumes ? "yes" : "no") },
            { key: "e", head: "Errors", cell: (b) => `${b.errors} / ${b.warnings} warnings`, meta: (b) => `${b.errors} errors, ${b.warnings} warnings` },
            { key: "a", head: "Restore", cell: (b) => (can("action.request") && ["Completed", "PartiallyFailed"].includes(b.phase) ? <PlanButton env={env} path="restore" label="Restore" title={`Restore ${b.name}`} ghost
                fields={<>{scope}<label>If an object already exists<select value={policy} onChange={(e) => setPolicy(e.target.value)}><option value="none">Keep what is there (safe)</option><option value="update">Overwrite it with the backup</option></select></label>
                  <label className="inline"><input type="checkbox" checked={pre} onChange={(e) => setPre(e.target.checked)} /> Take a backup first</label></>}
                build={() => ({ backup: b.name, included_namespaces: namespaces, existing_resource_policy: policy, pre_restore_backup: pre, restore_volumes: true })} /> : "") },
          ]} />
        </div>
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
