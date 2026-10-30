import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";
import { useAuth } from "../auth";
import { Badge, ErrorBox, PageHead, Time, useInterval, useLoad, type Env } from "../components";
import { Dialog } from "../ui";

const PHASE: Record<string, "ok" | "warn" | "bad" | "info"> = { Completed: "ok", PartiallyFailed: "warn", Failed: "bad", FailedValidation: "bad", InProgress: "info" };
const REFS = [["access_key_id", "R2 access key id"], ["secret_access_key", "R2 secret access key"], ["sse_key", "Encryption key (32 bytes, base64)"], ["repo_password", "Volume repository password"]];

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
  const data = <><label className="inline"><input type="checkbox" checked={volumes} onChange={(e) => setVolumes(e.target.checked)} /> Include PersistentVolume data</label>
    <label>Keep for (days)<input type="number" min={1} max={3650} value={days} onChange={(e) => setDays(Number(e.target.value))} /></label></>;
  const q = encodeURIComponent(`Review cluster backups: are they healthy, complete and restorable? Explain any failure.`);
  return (
    <>
      <PageHead title="Backups" sub="Every cluster object including Secrets, and volume data, stored encrypted in Cloudflare R2 by Velero.">
        <label>Environment<select value={env} onChange={(e) => setPicked(e.target.value)}>{envs.data?.map((e) => <option key={e.name} value={e.name}>{e.display_name}{e.is_production ? " (production)" : ""}</option>)}</select></label>
      </PageHead>
      <ErrorBox error={s.error ?? envs.error} />
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
          {d.backups.length ? <div className="table-wrap"><table>
            <thead><tr><th>Name</th><th>Status</th><th>Started</th><th>Expires</th><th>Items</th><th>Volumes</th><th>Errors</th><th /></tr></thead>
            <tbody>{d.backups.map((b: any) => <tr key={b.name}>
              <td className="mono small">{b.name}{b.failure && <div className="muted">{b.failure}</div>}</td>
              <td><Badge kind={PHASE[b.phase] ?? "neutral"}>{b.phase}</Badge></td>
              <td><Time iso={b.started} /></td><td><Time iso={b.expires} /></td>
              <td>{b.items ?? "—"}{b.items_total ? ` / ${b.items_total}` : ""}</td><td>{b.volumes ? "yes" : "no"}</td><td>{b.errors} / {b.warnings} warnings</td>
              <td>{can("action.request") && ["Completed", "PartiallyFailed"].includes(b.phase) && <PlanButton env={env} path="restore" label="Restore" title={`Restore ${b.name}`} ghost
                fields={<>{scope}<label>If an object already exists<select value={policy} onChange={(e) => setPolicy(e.target.value)}><option value="none">Keep what is there (safe)</option><option value="update">Overwrite it with the backup</option></select></label>
                  <label className="inline"><input type="checkbox" checked={pre} onChange={(e) => setPre(e.target.checked)} /> Take a backup first</label></>}
                build={() => ({ backup: b.name, included_namespaces: namespaces, existing_resource_policy: policy, pre_restore_backup: pre, restore_volumes: true })} />}</td>
            </tr>)}</tbody></table></div> : <div className="empty">No backups in this cluster yet.</div>}
        </div>
        {d.schedules.length > 0 && <div className="card"><h2>Schedules</h2><div className="table-wrap"><table><thead><tr><th>Name</th><th>Cron</th><th>Last backup</th><th>State</th></tr></thead>
          <tbody>{d.schedules.map((x: any) => <tr key={x.name}><td className="mono small">{x.name}</td><td className="mono small">{x.cron}</td><td><Time iso={x.last_backup} /></td><td>{x.paused ? <Badge kind="warn">paused</Badge> : <Badge kind="ok">active</Badge>}</td></tr>)}</tbody></table></div></div>}
        {d.restores.length > 0 && <div className="card"><h2>Restores</h2><div className="table-wrap"><table><thead><tr><th>Name</th><th>Backup</th><th>Status</th><th>Started</th><th>Errors</th></tr></thead>
          <tbody>{d.restores.map((r: any) => <tr key={r.name}><td className="mono small">{r.name}</td><td className="mono small">{r.backup}</td><td><Badge kind={PHASE[r.phase] ?? "neutral"}>{r.phase}</Badge></td><td><Time iso={r.started} /></td><td>{r.errors} / {r.warnings} warnings</td></tr>)}</tbody></table></div></div>}
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
        <div className="filters">{REFS.map(([k, label]) => <label key={k}>{label}<input required pattern="MOKS_CRED_[A-Z0-9_]+" className="mono" value={f.names[k]} onChange={(e) => set({ names: { ...f.names, [k]: e.target.value } })} />
          {store && <span className="small">{store.credential_status[k].available ? "available here" : "not set here"}</span>}</label>)}</div>
        <div className="row"><button disabled={busy}>Save</button>
          {store && <button type="button" className="ghost" disabled={busy} onClick={() => run(() => api("/backups/store/test", { method: "POST" }))}>Test write, read and delete</button>}
          {store?.last_test_at && <span className="small">{store.last_test_ok ? <Badge kind="ok">test passed</Badge> : <Badge kind="bad">test failed</Badge>} <Time iso={store.last_test_at} /> {store.last_test_error}</span>}</div>
      </form>
    </>
  );
}
