import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { useAuth } from "../auth";
import { Badge, ErrorBox, PageHead, Time, useLoad } from "../components";

const SEV: Record<string, "bad" | "warn" | "neutral"> = { sev1: "bad", sev2: "bad", sev3: "warn", sev4: "neutral" };
const RESULT: Record<string, "bad" | "ok" | "neutral"> = { firing: "bad", ok: "ok", unknown: "neutral" };

export function Detections() {
  const { can } = useAuth();
  const admin = can("admin.policies");
  const rules = useLoad(() => api<any>("/detections/rules"), []);
  const states = useLoad(() => api<any>("/detections/states"), []);
  const windows = useLoad(() => api<any>("/detections/maintenance"), []);
  const [text, setText] = useState("");
  const [error, setError] = useState<unknown>();
  const [preview, setPreview] = useState<any>(null);
  const [testing, setTesting] = useState("");
  const envs: any[] = rules.data?.environments ?? [];
  const [env, setEnv] = useState("");
  const target = env || envs[0]?.name || "";
  const [w, setW] = useState({ namespace: "*", rule_id: "*", starts_at: "", ends_at: "", reason: "" });
  const rows = (rules.data?.rules ?? []).filter((r: any) => JSON.stringify([r.id, r.title, r.skill]).toLowerCase().includes(text.toLowerCase()));

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
      <ErrorBox error={rules.error ?? error} />
      {envs.length > 0 && !envs.some((e) => e.detection_enabled) && <div className="alert info small" style={{ marginBottom: 12 }}>
        Detection is off in every environment. {admin ? <>Turn it on per environment under <Link to="/admin/environments">Environments</Link>.</> : "An administrator can turn it on."}</div>}
      <div className="card filters" style={{ marginBottom: 14 }}>
        <label>Search<input value={text} onChange={(e) => setText(e.target.value)} /></label>
        <label>Test in<select value={target} onChange={(e) => setEnv(e.target.value)}>{envs.map((e) => <option key={e.name}>{e.name}</option>)}</select></label>
        <span className="small muted">{rows.length} rules. Unknown means the source did not answer: it neither opens nor resolves an incident.</span>
      </div>
      {preview && <div className="card stack" style={{ marginBottom: 14 }}>
        <div className="row between"><h2>Dry run: <span className="mono">{preview.rule}</span> in {preview.environment}</h2><button className="ghost small" onClick={() => setPreview(null)}>Close</button></div>
        <div className="small"><Badge kind={RESULT[preview.status]}>{preview.status}</Badge> {preview.reason} {preview.partial && "Some namespaces could not be read."} Nothing was written.</div>
        {preview.preview.length > 0 ? <table><thead><tr><th>Resource</th><th>Detail</th><th>Would</th></tr></thead>
          <tbody>{preview.preview.map((p: any, i: number) => <tr key={i}><td className="mono small">{p.namespace ? `${p.namespace}/` : ""}{p.resource}</td><td className="small">{p.detail}</td><td className="small">{p.would}</td></tr>)}</tbody></table>
          : <div className="small muted">No resource matches right now.</div>}
      </div>}
      <div className="card table-wrap">
        <table>
          <thead><tr><th>Rule</th><th>Source</th><th>Severity</th><th>Schedule</th><th>Last result</th><th /></tr></thead>
          <tbody>{rows.map((r: any) => (
            <tr key={r.id}>
              <td>{r.title} {!r.enabled && <Badge>off</Badge>}<div className="mono small muted">{r.id} · {r.skill} v{r.version} · {r.owner}</div>
                {r.unavailable_in.length > 0 && <div className="small muted">Needs {r.needs} in {r.unavailable_in.join(", ")}</div>}</td>
              <td className="small">{r.kind === "finding" ? `detector: ${r.finding}` : r.kind}{r.query && <div className="mono muted">{r.query.slice(0, 80)}</div>}</td>
              <td><Badge kind={SEV[r.severity]}>{r.severity}</Badge></td>
              <td className="small">every {r.interval_minutes} min, {r.sustained_checks} to open, {r.recovery_checks} to clear{r.auto_investigate ? ", investigates" : ""}{r.auto_resolve ? ", auto-resolves" : ""}</td>
              <td className="small">{r.runs.map((x: any) => <div key={x.environment} title={x.reason ?? ""}>{x.environment} <Badge kind={RESULT[x.status]}>{x.status}</Badge> <Time iso={x.at} /></div>)}{!r.runs.length && <span className="muted">not run</span>}</td>
              <td className="row">{admin && <>
                <button className="ghost small" disabled={testing === r.id || !target} onClick={() => test(r.id)}>{testing === r.id ? "Testing…" : "Dry run"}</button>
                <button className="ghost small" onClick={() => toggle(r)}>{r.enabled ? "Turn off" : "Turn on"}</button></>}</td>
            </tr>
          ))}</tbody>
        </table>
        {!admin && <div className="small muted">Changing rules and dry runs need the Administrator role.</div>}
      </div>
      <div className="grid cols-2" style={{ marginTop: 14, alignItems: "start" }}>
        <div className="card">
          <h2>Current conditions</h2>
          {states.data?.states.length ? <table><tbody>{states.data.states.map((s: any, i: number) => (
            <tr key={i}><td className="small">{s.environment}<div className="mono muted">{s.resource}</div></td><td><Badge kind={s.status === "firing" ? "bad" : s.status === "suppressed" ? "neutral" : "warn"}>{s.status}</Badge></td>
              <td className="small mono">{s.rule}</td><td className="small"><Time iso={s.last_seen} /></td><td>{s.incident_id && <Link to={`/incidents/${s.incident_id}`}>incident</Link>}</td></tr>
          ))}</tbody></table> : <div className="small muted">Nothing is firing.</div>}
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
