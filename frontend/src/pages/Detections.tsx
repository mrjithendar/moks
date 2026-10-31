import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { useAuth } from "../auth";
import { Badge, DataTable, ErrorBox, Loading, PageHead, Time, useLoad } from "../components";
import { type Chip, FilterPanel, useUrlFilters } from "../filters";
import { type MenuItem, Empty, Identifier, Truncate } from "../ui";

const SEV: Record<string, "bad" | "warn" | "neutral"> = { sev1: "bad", sev2: "bad", sev3: "warn", sev4: "neutral" };
const RESULT: Record<string, "bad" | "ok" | "neutral"> = { firing: "bad", ok: "ok", unknown: "neutral" };

export function Detections() {
  const { can } = useAuth();
  const admin = can("admin.policies");
  const rules = useLoad(() => api<any>("/detections/rules"), []);
  const states = useLoad(() => api<any>("/detections/states"), []);
  const windows = useLoad(() => api<any>("/detections/maintenance"), []);
  const [{ q: text, severity }, setF, clearF] = useUrlFilters({ q: "", severity: "" });
  const [error, setError] = useState<unknown>();
  const [preview, setPreview] = useState<any>(null);
  const [testing, setTesting] = useState("");
  const envs: any[] = rules.data?.environments ?? [];
  const [env, setEnv] = useState("");
  const target = env || envs[0]?.name || "";
  const [w, setW] = useState({ namespace: "*", rule_id: "*", starts_at: "", ends_at: "", reason: "" });
  const rows: any[] = (rules.data?.rules ?? []).filter((r: any) => JSON.stringify([r.id, r.title, r.skill]).toLowerCase().includes(text.toLowerCase()) && (!severity || r.severity === severity));
  const chips: Chip[] = [...(text ? [{ key: "q", label: `matches ${text}`, onRemove: () => setF({ q: "" }) }] : []), ...(severity ? [{ key: "s", label: severity, onRemove: () => setF({ severity: "" }) }] : [])];

  async function run(fn: () => Promise<any>) {
    setError(null);
    try { return await fn(); } catch (e) { setError(e); }
  }
  async function test(id: string) {
    setTesting(id);
    const r = await run(() => api(`/detections/rules/${id}/test`, { method: "POST", body: { environment: target } }));
    if (r) setPreview(r);
    setTesting("");
  }
  async function toggle(r: any) {
    await run(() => api(`/detections/rules/${r.id}`, { method: "PUT", body: { enabled: !r.enabled, environments: r.policy_environments } }));
    rules.reload();
  }
  async function addWindow(e: FormEvent) {
    e.preventDefault();
    await run(() => api("/detections/maintenance", { method: "POST", body: { ...w, environment: target, starts_at: new Date(w.starts_at).toISOString(), ends_at: new Date(w.ends_at).toISOString() } }));
    windows.reload();
  }
  return (
    <>
      <PageHead title="Detection rules" sub="Deterministic rules from SRE skills that open and update incidents. An incident never authorizes a change." />
      <Loading load={rules} rows={6} />
      <ErrorBox error={error} />
      {envs.length > 0 && !envs.some((e) => e.detection_enabled) && <div className="alert info small" style={{ marginBottom: 12 }}>
        Detection is off in every environment. {admin ? <>Turn it on per environment under <Link to="/admin/environments">Environments</Link>.</> : "An administrator can turn it on."}</div>}
      <FilterPanel chips={chips} onClear={clearF} search={<input type="search" className="grow" aria-label="Search rules" value={text} onChange={(e) => setF({ q: e.target.value })} placeholder="Search rules" />}>
        <label>Severity<select value={severity} onChange={(e) => setF({ severity: e.target.value })}><option value="">All</option>{Object.keys(SEV).map((k) => <option key={k}>{k}</option>)}</select></label>
        <label>Test in<select value={target} onChange={(e) => setEnv(e.target.value)}>{envs.map((e) => <option key={e.name}>{e.name}</option>)}</select></label>
      </FilterPanel>
      <p className="small muted">{rows.length} rules. Unknown means the source did not answer: it neither opens nor resolves an incident.</p>
      {preview && <div className="card stack" style={{ marginBottom: 14 }}>
        <div className="row between"><h2>Dry run: <span className="mono">{preview.rule}</span> in {preview.environment}</h2><button className="ghost small" onClick={() => setPreview(null)}>Close</button></div>
        <div className="small"><Badge kind={RESULT[preview.status]}>{preview.status}</Badge> {preview.reason} {preview.partial && "Some namespaces could not be read."} Nothing was written.</div>
        {preview.preview.length > 0 ? <DataTable label="Dry run matches" rows={preview.preview as any[]} rowKey={(p) => `${p.namespace}${p.resource}`} cols={[
          { key: "r", head: "Resource", primary: true, cell: (p) => <Identifier name={`${p.namespace ? `${p.namespace}/` : ""}${p.resource}`} what="resource" /> },
          { key: "d", head: "Detail", cell: (p) => p.detail }, { key: "w", head: "Would", cell: (p) => p.would }]} />
          : <div className="small muted">No resource matches right now.</div>}
      </div>}
      <div className="card">
        <DataTable label="Detection rules" rows={rows} rowKey={(r) => r.id} empty={<Empty title="No rules match." />}
          menu={admin ? (r) => ({ label: `Actions for ${r.title}`, items: [
            { label: testing === r.id ? "Testing…" : "Dry run", disabled: testing === r.id || !target, run: () => test(r.id) },
            { label: r.enabled ? "Turn off" : "Turn on", run: () => toggle(r) } ] as MenuItem[] }) : undefined}
          cols={[
            { key: "rule", head: "Rule", primary: true, cell: (r) => <><span className="row tight"><Truncate text={r.title} lines={2} />{!r.enabled && <Badge>off</Badge>}</span><Truncate text={`${r.id} · ${r.skill} v${r.version} · ${r.owner}`} mono />
              {r.unavailable_in.length > 0 && <span className="small muted">Needs {r.needs} in {r.unavailable_in.join(", ")}</span>}</> },
            { key: "sev", head: "Severity", status: true, cell: (r) => <Badge kind={SEV[r.severity]}>{r.severity}</Badge> },
            { key: "src", head: "Source", wide: true, cell: (r) => <span className="small">{r.kind === "finding" ? `detector: ${r.finding}` : r.kind}{r.query && <Truncate text={r.query} mono />}</span> },
            { key: "sch", head: "Schedule", cell: (r) => <span className="small">every {r.interval_minutes} min, {r.sustained_checks} to open, {r.recovery_checks} to clear{r.auto_investigate ? ", investigates" : ""}{r.auto_resolve ? ", auto-resolves" : ""}</span> },
            { key: "last", head: "Last result", cell: (r) => <span className="small">{r.runs.map((x: any) => <div key={x.environment} title={x.reason ?? ""}>{x.environment} <Badge kind={RESULT[x.status]}>{x.status}</Badge> <Time iso={x.at} /></div>)}{!r.runs.length && <span className="muted">not run</span>}</span> },
          ]} />
        {!admin && <div className="small muted">Changing rules and dry runs need the Administrator role.</div>}
      </div>
      <div className="grid cols-2" style={{ marginTop: 14, alignItems: "start" }}>
        <div className="card">
          <h2>Current conditions</h2>
          {states.data && <DataTable label="Current conditions" rows={states.data.states as any[]} rowKey={(s) => `${s.environment}${s.resource}${s.rule}`} empty={<div className="small muted">Nothing is firing.</div>} cols={[
            { key: "res", head: "Resource", primary: true, cell: (s) => <Identifier name={s.resource} what="resource" /> },
            { key: "st", head: "Status", status: true, cell: (s) => <Badge kind={s.status === "firing" ? "bad" : s.status === "suppressed" ? "neutral" : "warn"}>{s.status}</Badge> },
            { key: "env", head: "Environment", cell: (s) => s.environment },
            { key: "rule", head: "Rule", cell: (s) => <Truncate text={s.rule} mono />, meta: (s) => s.rule },
            { key: "at", head: "Last seen", cell: (s) => <Time iso={s.last_seen} /> },
            { key: "inc", head: "Incident", cell: (s) => (s.incident_id ? <Link to={`/incidents/${s.incident_id}`}>incident</Link> : "") , meta: (s) => (s.incident_id ? <Link to={`/incidents/${s.incident_id}`}>incident</Link> : "") },
          ]} />}
        </div>
        <div className="card stack">
          <h2>Maintenance windows</h2>
          {windows.data?.windows.map((x: any) => <div key={x.id} className="row between small"><span>{x.environment}/{x.namespace} · {new Date(x.starts_at).toLocaleString()} to {new Date(x.ends_at).toLocaleString()} · {x.reason}</span>
            {admin && <button className="ghost small" onClick={async () => { await run(() => api(`/detections/maintenance/${x.id}`, { method: "DELETE" })); windows.reload(); }}>Remove</button>}</div>)}
          {windows.data && !windows.data.windows.length && <div className="small muted">None scheduled.</div>}
          {admin && <form className="filters" onSubmit={addWindow}>
            <label>Namespace<input value={w.namespace} onChange={(e) => setW({ ...w, namespace: e.target.value })} /></label>
            <label>From<input required type="datetime-local" value={w.starts_at} onChange={(e) => setW({ ...w, starts_at: e.target.value })} /></label>
            <label>To<input required type="datetime-local" value={w.ends_at} onChange={(e) => setW({ ...w, ends_at: e.target.value })} /></label>
            <label>Reason<input required minLength={3} value={w.reason} onChange={(e) => setW({ ...w, reason: e.target.value })} /></label>
            <button>Add</button>
          </form>}
        </div>
      </div>
    </>
  );
}
