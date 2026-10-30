import { useCallback, useEffect, useState, type FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, API, qs } from "../api";
import { useAuth } from "../auth";
import { Badge, ErrorBox, PageHead, ScopePicker, StatusBadge, Time, useLoad } from "../components";

const SEV: Record<string, "bad" | "warn" | "info" | "neutral"> = { sev1: "bad", sev2: "bad", sev3: "warn", sev4: "neutral" };
const PLAN: Record<string, "ok" | "warn" | "bad" | "info" | "neutral"> = {
  reconciliation_required: "bad", reconciled: "neutral", pending: "info", approved: "ok", queued: "info", executing: "info", succeeded: "ok", recovered: "warn", failed: "bad", rejected: "bad", expired: "neutral", cancelled: "neutral",
};

// ---- Incidents --------------------------------------------------------------------------------

export function Incidents() {
  const { can } = useAuth();
  const nav = useNavigate();
  const [status, setStatus] = useState("");
  const list = useLoad(() => api<any[]>(`/incidents${qs({ status })}`), [status]);
  const alerts = useLoad(() => api<any[]>("/alerts/events"), []);
  const [env, setEnv] = useState("");
  const [ns, setNs] = useState("");
  const [form, setForm] = useState({ title: "", severity: "sev3", summary: "" });
  const [error, setError] = useState<unknown>();
  const onEnv = useCallback((e: string) => setEnv(e), []);
  const onNs = useCallback((n: string) => setNs(n), []);
  async function create(e: FormEvent) {
    e.preventDefault();
    try {
      const inc = await api<any>("/incidents", { method: "POST", body: { ...form, environment: env, namespace: ns || "*" } });
      nav(`/incidents/${inc.id}`);
    } catch (err) { setError(err); }
  }
  return (
    <>
      <PageHead title="Incidents" sub="Incident workspaces: timeline, evidence, hypotheses, actions and validation">
        <select value={status} onChange={(e) => setStatus(e.target.value)}><option value="">all</option>{["open", "mitigated", "resolved", "closed"].map((s) => <option key={s}>{s}</option>)}</select>
      </PageHead>
      <div className="card">
        <table>
          <thead><tr><th>Incident</th><th>Severity</th><th>Status</th><th>Scope</th><th>Source</th><th>Updated</th></tr></thead>
          <tbody>{list.data?.map((i) => (
            <tr key={i.id}>
              <td><Link to={`/incidents/${i.id}`}>{i.title}</Link>{i.postmortem_status !== "none" && <span className="small muted"> · postmortem {i.postmortem_status}</span>}</td>
              <td><Badge kind={SEV[i.severity]}>{i.severity}</Badge></td><td><Badge kind={i.status === "open" ? "bad" : i.status === "mitigated" ? "warn" : "ok"}>{i.status}</Badge></td>
              <td className="mono small">{i.environment}/{i.namespace}</td><td className="small">{i.source}</td><td><Time iso={i.updated_at} /></td>
            </tr>
          ))}</tbody>
        </table>
        {list.data && !list.data.length && <div className="empty">No incidents.</div>}
      </div>
      {can("incident.write") && (
        <form className="card stack" style={{ marginTop: 14 }} onSubmit={create}>
          <h2>Open an incident</h2>
          <div className="row">
            <ScopePicker env={env} ns={ns} onEnv={onEnv} onNs={onNs} allowAll />
            <label>Severity<select value={form.severity} onChange={(e) => setForm({ ...form, severity: e.target.value })}>{["sev1", "sev2", "sev3", "sev4"].map((s) => <option key={s}>{s}</option>)}</select></label>
          </div>
          <label>Title<input required minLength={3} value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })} placeholder="Checkout returning 502s" /></label>
          <label>Summary<textarea rows={2} value={form.summary} onChange={(e) => setForm({ ...form, summary: e.target.value })} /></label>
          <ErrorBox error={error} />
          <div><button>Open incident</button></div>
        </form>
      )}
      <div className="card" style={{ marginTop: 14 }}>
        <h2>Recent alerts</h2>
        <table>
          <thead><tr><th>Alert</th><th>Status</th><th>Decision</th><th>Scope</th><th>Received</th></tr></thead>
          <tbody>{alerts.data?.slice(0, 30).map((a) => (
            <tr key={a.id}>
              <td>{a.alertname}<div className="small muted">{a.annotations?.summary}</div></td>
              <td><Badge kind={a.status === "firing" ? "bad" : "ok"}>{a.status}</Badge></td>
              <td className="small">{a.decision}{a.incident_id && <> · <Link to={`/incidents/${a.incident_id}`}>incident</Link></>}{a.investigation_id && <> · <Link to={`/investigations/${a.investigation_id}`}>investigation</Link></>}</td>
              <td className="mono small">{a.environment}/{a.labels?.namespace ?? "*"}</td><td><Time iso={a.received_at} /></td>
            </tr>
          ))}</tbody>
        </table>
        {alerts.data && !alerts.data.length && <div className="empty">No alerts received. Enable auto-investigate on an environment or point Alertmanager/Grafana at the webhook.</div>}
      </div>
    </>
  );
}

export function IncidentView() {
  const { id } = useParams();
  const { can } = useAuth();
  const inc = useLoad(() => api<any>(`/incidents/${id}`), [id]);
  const [note, setNote] = useState({ text: "", kind: "note" });
  const [hyp, setHyp] = useState({ text: "", confidence: "low" });
  const [pm, setPm] = useState<any>(null);
  const [error, setError] = useState<unknown>();
  const d = inc.data;
  useEffect(() => { if (d && pm === null) setPm(d.postmortem ?? { impact: "", root_cause: "", root_cause_verified: false, contributing_factors: "", detection: "", resolution: "", lessons: "", action_items: [] }); }, [d, pm]);
  async function act(fn: () => Promise<any>) {
    setError(null);
    try { await fn(); inc.reload(); } catch (e) { setError(e); }
  }
  if (!d) return <ErrorBox error={inc.error} />;
  const write = can("incident.write");
  return (
    <>
      <PageHead title={d.title} sub={<span className="row small"><Badge kind={SEV[d.severity]}>{d.severity}</Badge><Badge>{d.status}</Badge><span className="mono">{d.environment}/{d.namespace}</span><span>source {d.source}</span><span>opened <Time iso={d.created_at} /></span></span>}>
        <a className="btn" href={`${API}/incidents/${id}/handover.md`} target="_blank" rel="noreferrer">Handover</a>
        <a className="btn" href={`${API}/incidents/${id}/report.md`} target="_blank" rel="noreferrer">Report (Markdown)</a>
        {write && <select value={d.status} onChange={(e) => act(() => api(`/incidents/${id}`, { method: "PATCH", body: { status: e.target.value } }))}>
          {["open", "mitigated", "resolved", "closed"].map((s) => <option key={s}>{s}</option>)}</select>}
        <Link className="btn" to={`/investigate?env=${d.environment}&ns=${d.namespace === "*" ? "" : d.namespace}&incident=${id}&q=${encodeURIComponent(d.title)}`}>Investigate</Link>
      </PageHead>
      <ErrorBox error={error} />
      <div className="grid" style={{ gridTemplateColumns: "minmax(0, 3fr) minmax(300px, 2fr)", alignItems: "start" }}>
        <div className="stack">
          {d.summary && <div className="card"><h2>Summary</h2><p style={{ margin: 0 }}>{d.summary}</p></div>}
          {write && <div className="card filters"><label>Assigned to<input defaultValue={d.commander ?? ""} type="email" placeholder="engineer@example.com"
            onBlur={(e) => { if (e.target.value !== (d.commander ?? "")) act(() => api(`/incidents/${id}`, { method: "PATCH", body: { commander: e.target.value } })); }} /></label>
            <label>Severity<select value={d.severity} onChange={(e) => act(() => api(`/incidents/${id}`, { method: "PATCH", body: { severity: e.target.value } }))}>{["sev1", "sev2", "sev3", "sev4"].map((x) => <option key={x}>{x}</option>)}</select></label></div>}
          {d.detection && <div className="card"><h2>Detection</h2>
            <dl className="kv small"><dt>Rule</dt><dd className="mono">{d.detection.rule} (skill {d.detection.skill} v{d.detection.rule_version})</dd>
              <dt>Resource</dt><dd className="mono">{d.detection.resource}</dd><dt>Observed</dt><dd>{new Date(d.detection.first_observed).toLocaleString()} to {new Date(d.detection.last_observed).toLocaleString()}, {d.detection.count} checks</dd>
              <dt>Severity</dt><dd>{d.detection.severity_reason}</dd><dt>Owner</dt><dd>{d.detection.owner}</dd><dt>Runbook</dt><dd>{d.detection.runbook}</dd></dl>
            <div className="small muted">An automatic incident authorizes no change. Remediation stays behind action plans and approvals.</div></div>}
          <div className="card">
            <h2>Hypotheses</h2>
            <ul>{d.hypotheses.map((h: any, i: number) => <li key={i}><Badge kind={h.status === "verified" ? "ok" : h.status === "refuted" ? "neutral" : "warn"}>{h.status}</Badge> <Badge>{h.confidence}</Badge> {h.text}
              {write && <select style={{ marginLeft: 6 }} value={h.status} onChange={(e) => act(() => api(`/incidents/${id}`, { method: "PATCH", body: { hypotheses: d.hypotheses.map((x: any, j: number) => j === i ? { ...x, status: e.target.value } : x) } }))}>
                {["open", "supported", "refuted", "verified"].map((s) => <option key={s}>{s}</option>)}</select>}</li>)}</ul>
            {!d.hypotheses.length && <div className="muted small">No hypotheses yet.</div>}
            {write && <form className="row" onSubmit={(e) => { e.preventDefault(); act(() => api(`/incidents/${id}`, { method: "PATCH", body: { hypotheses: [...d.hypotheses, { ...hyp, status: "open" }] } })).then(() => setHyp({ text: "", confidence: "low" })); }}>
              <input style={{ flex: 1 }} required value={hyp.text} onChange={(e) => setHyp({ ...hyp, text: e.target.value })} placeholder="Suspected cause" />
              <select value={hyp.confidence} onChange={(e) => setHyp({ ...hyp, confidence: e.target.value })}><option>low</option><option>medium</option><option>high</option></select>
              <button className="small">Add</button></form>}
          </div>
          <div className="card">
            <h2>Investigations</h2>
            {d.investigations.map((i: any) => <div key={i.id} style={{ marginBottom: 8 }}><StatusBadge status={i.status} /> <Link to={`/investigations/${i.id}`}>{i.question}</Link> <span className="small muted">({i.trigger})</span>
              {i.summary && <div className="small muted">{i.summary}</div>}</div>)}
            {!d.investigations.length && <div className="muted small">None linked. Start one with Investigate.</div>}
          </div>
          <div className="card">
            <h2>Actions</h2>
            {d.actions.map((a: any) => <div key={a.id}><Badge kind={PLAN[a.status]}>{a.status}</Badge> <Link to={`/actions/${a.id}`}>{a.action} {a.target}</Link> <span className="small muted"><Time iso={a.created_at} /></span></div>)}
            {!d.actions.length && <div className="muted small">No action plans.</div>}
          </div>
          {pm && (
            <div className="card stack">
              <div className="row between"><h2>Postmortem</h2><Badge kind={d.postmortem_status === "approved" ? "ok" : "neutral"}>{d.postmortem_status}</Badge></div>
              {(["impact", "root_cause", "contributing_factors", "detection", "resolution", "lessons"] as const).map((k) => (
                <label key={k}>{k.replace("_", " ")}<textarea rows={2} disabled={d.postmortem_status === "approved" || !write} value={pm[k] ?? ""} onChange={(e) => setPm({ ...pm, [k]: e.target.value })} /></label>
              ))}
              <label className="inline"><input type="checkbox" checked={!!pm.root_cause_verified} disabled={d.postmortem_status === "approved" || !write} onChange={(e) => setPm({ ...pm, root_cause_verified: e.target.checked })} /> root cause verified by a validated change</label>
              <label>Action items (one per line)<textarea rows={3} disabled={d.postmortem_status === "approved" || !write} value={(pm.action_items ?? []).join("\n")} onChange={(e) => setPm({ ...pm, action_items: e.target.value.split("\n").filter(Boolean) })} /></label>
              {d.postmortem_status !== "approved" && <div className="row">
                {write && <button onClick={() => act(() => api(`/incidents/${id}/postmortem`, { method: "PUT", body: pm }))}>Save draft</button>}
                {can("action.approve") && <button className="ghost" onClick={() => act(() => api(`/incidents/${id}/postmortem/approve`, { method: "POST" }))}>Approve (adds it to the knowledge base)</button>}
              </div>}
            </div>
          )}
        </div>
        <div className="card">
          <h2>Timeline</h2>
          {write && <form className="stack" onSubmit={(e) => { e.preventDefault(); act(() => api(`/incidents/${id}/events`, { method: "POST", body: note })).then(() => setNote({ text: "", kind: "note" })); }}>
            <textarea rows={2} required value={note.text} onChange={(e) => setNote({ ...note, text: e.target.value })} placeholder="Note, evidence, communication…" />
            <div className="row"><select value={note.kind} onChange={(e) => setNote({ ...note, kind: e.target.value })}>{["note", "evidence", "validation", "communication"].map((k) => <option key={k}>{k}</option>)}</select><button className="small">Add</button></div>
          </form>}
          <ul className="timeline" style={{ marginTop: 10 }}>
            {[...d.timeline].reverse().map((e: any) => <li key={e.id}><span className="small muted">{new Date(e.ts).toLocaleTimeString()}</span>
              <span className={`dot ${e.kind === "alert" ? "bad" : e.kind === "validation" ? "ok" : ""}`} />
              <span><Badge>{e.kind}</Badge> {e.payload.text ?? JSON.stringify(e.payload)} <span className="small muted">{e.actor ?? "system"}</span></span></li>)}
          </ul>
        </div>
      </div>
    </>
  );
}

// ---- Action plans -------------------------------------------------------------------------------

export function Actions() {
  const inbox = useLoad(() => api<any[]>("/actions/plans?inbox=true"), []);
  const all = useLoad(() => api<any[]>("/actions/plans"), []);
  return (
    <>
      <PageHead title="Approvals & actions" sub="Allowlisted remediation. Approval binds to the exact plan hash; plans expire and execute at most once." />
      <div className="card"><h2>Waiting for your approval ({inbox.data?.length ?? 0})</h2><PlanTable rows={inbox.data ?? []} /></div>
      <div className="card" style={{ marginTop: 14 }}><h2>All plans</h2><PlanTable rows={all.data ?? []} /></div>
    </>
  );
}

function PlanTable({ rows }: { rows: any[] }) {
  if (!rows.length) return <div className="empty">Nothing here.</div>;
  return (
    <table>
      <thead><tr><th>Action</th><th>Target</th><th>Status</th><th>Requested by</th><th>Expires</th></tr></thead>
      <tbody>{rows.map((p) => (
        <tr key={p.id}><td><Link to={`/actions/${p.id}`}>{p.title}</Link></td><td className="mono small">{p.environment}/{p.namespace} {p.target}</td>
          <td><Badge kind={PLAN[p.status]}>{p.status}</Badge>{p.requires_approval && <Badge kind="neutral">approval</Badge>}</td>
          <td className="small">{p.requested_by ?? "—"}</td><td><Time iso={p.expires_at} /></td></tr>
      ))}</tbody>
    </table>
  );
}

export function PlanView() {
  const { id } = useParams();
  const { can, me } = useAuth();
  const plan = useLoad(() => api<any>(`/actions/plans/${id}`), [id]);
  const [comment, setComment] = useState("");
  const [outcome, setOutcome] = useState("not_applied");
  const [error, setError] = useState<unknown>();
  const p = plan.data;
  useEffect(() => {
    if (!p || !["queued", "executing"].includes(p.status)) return;
    const t = setInterval(plan.reload, 2500);
    return () => clearInterval(t);
  }, [p, plan.reload]);
  async function act(path: string, body?: any) {
    setError(null);
    try { await api(`/actions/plans/${id}/${path}`, { method: "POST", body }); plan.reload(); } catch (e) { setError(e); }
  }
  if (!p) return <ErrorBox error={plan.error} />;
  const mine = p.requested_by === me?.email;
  return (
    <>
      <PageHead title={`${p.title}: ${p.target}`} sub={<span className="row small"><Badge kind={PLAN[p.status]}>{p.status}</Badge><span className="mono">{p.environment}/{p.namespace}</span><span>expires <Time iso={p.expires_at} /></span></span>} />
      <ErrorBox error={error} />
      <div className="grid cols-2" style={{ alignItems: "start" }}>
        <div className="card stack">
          <h2>Plan under review</h2>
          <dl className="kv">
            <dt>Operation</dt><dd className="mono small">{p.operation}</dd>
            <dt>Parameters</dt><dd className="mono small">{JSON.stringify(p.params)}</dd>
            <dt>Reason</dt><dd>{p.reason}</dd>
            <dt>Expected impact</dt><dd>{p.expected_impact}</dd>
            <dt>Blast radius</dt><dd>{p.blast_radius}</dd>
            <dt>Preconditions</dt><dd><ul style={{ margin: 0, paddingLeft: 16 }}>{p.preconditions.map((x: string, i: number) => <li key={i}>{x}</li>)}</ul></dd>
            <dt>Validation</dt><dd><ul style={{ margin: 0, paddingLeft: 16 }}>{p.validation.map((x: string, i: number) => <li key={i}>{x}</li>)}</ul></dd>
            <dt>Rollback</dt><dd>{p.rollback} {p.auto_recover && <Badge kind="info">automatic recovery</Badge>}</dd>
            <dt>Pre-state</dt><dd className="mono small">{JSON.stringify(p.pre_state)}</dd>
            <dt>Evidence</dt><dd className="small">{p.evidence.length ? p.evidence.map((e: any, i: number) => <div key={i}>{e.ref ?? ""} {e.summary ?? JSON.stringify(e)}</div>) : "—"}
              {p.investigation_id && <div><Link to={`/investigations/${p.investigation_id}`}>source investigation</Link></div>}</dd>
            <dt>Plan hash</dt><dd className="mono small">{p.plan_hash}</dd>
            <dt>Requested by</dt><dd>{p.requested_by}</dd>
          </dl>
        </div>
        <div className="stack">
          <div className="card stack">
            <h2>Decision</h2>
            {p.approvals.map((a: any, i: number) => <div key={i} className="small"><Badge kind={a.decision === "approve" ? "ok" : "bad"}>{a.decision}</Badge> {a.approver} <Time iso={a.at} /> {a.comment && `— ${a.comment}`}</div>)}
            {p.status === "pending" && p.requires_approval && can("action.approve") && !mine && <>
              <textarea rows={2} value={comment} onChange={(e) => setComment(e.target.value)} placeholder="Comment (optional)" />
              <div className="row"><button onClick={() => act("approve", { plan_hash: p.plan_hash, comment })}>Approve this exact plan</button>
                <button className="danger" onClick={() => act("reject", { plan_hash: p.plan_hash, comment })}>Reject</button></div>
            </>}
            {mine && p.status === "pending" && p.requires_approval && <div className="small muted">Waiting for someone else to approve (you requested it).</div>}
            {can("action.execute") && (p.status === "approved" || (p.status === "pending" && !p.requires_approval)) &&
              <button onClick={() => act("execute", { plan_hash: p.plan_hash })}>Execute</button>}
            {p.status === "reconciliation_required" && <div className="alert error small stack">
              <strong>MOKS could not establish what happened, and will not retry.</strong>
              <div>{p.result?.recovery?.reason ?? p.result?.error}</div>
              {p.result?.observed && <div>Same object: {String(p.result.observed.same_object)}. Fields unchanged from before: {JSON.stringify(p.result.observed.unchanged_from_before)}</div>}
              <div>Check the object yourself, then record the outcome.</div>
              {can("action.approve") ? <>
                <label>What happened<select value={outcome} onChange={(e) => setOutcome(e.target.value)}>
                  <option value="not_applied">The change was not applied</option><option value="applied">The change was applied</option><option value="reverted">It was reverted by hand</option>
                </select></label>
                <textarea rows={2} value={comment} onChange={(e) => setComment(e.target.value)} placeholder="How you verified this (required)" />
                <button disabled={!comment.trim()} onClick={() => act("reconcile", { outcome, comment })}>Record outcome</button>
              </> : <div>Someone with the Approver role must record the outcome.</div>}
            </div>}
            {["pending", "approved", "queued"].includes(p.status) && (mine || can("action.approve")) && <button className="ghost" onClick={() => act("cancel")}>Cancel plan</button>}
          </div>
          {p.result && <div className="card"><h2>Result</h2>
            {p.result.validation && <div className="row" style={{ marginBottom: 8 }}><Badge kind={p.result.validation.ok ? "ok" : "bad"}>validation {p.result.validation.ok ? "passed" : "failed"}</Badge>
              {p.result.validation.reason && <span className="small">{p.result.validation.reason}</span>}</div>}
            {p.result.recovery?.status === "applied" && <div className="alert warn small">Recovery ran: the changed fields were restored ({p.result.recovery.validation?.ok ? "validated" : "recovery not validated"}).</div>}
            {p.result.error && <div className="alert error small">{p.result.error}</div>}
            <pre className="logbox">{JSON.stringify(p.result, null, 2)}</pre></div>}
        </div>
      </div>
    </>
  );
}

/** Create an action plan, prefilled from an investigation's proposed remediation or a rightsizing row. */
export function PlanForm({ env, ns, initial, onDone }: { env: string; ns: string; initial?: any; onDone?: (id: string) => void }) {
  const cat = useLoad(() => api<any[]>("/actions/catalog"), []);
  const [f, setF] = useState<any>({ action: "restart_deployment", target_kind: "Deployment", target_name: "", params: "{}", reason: "", ...initial,
    ...(initial?.params ? { params: JSON.stringify(initial.params, null, 1) } : {}) });
  const [error, setError] = useState<unknown>();
  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      const p = await api<any>("/actions/plans", { method: "POST", body: { environment: env, namespace: ns, action: f.action, target_kind: f.target_kind,
        target_name: f.target_name, params: JSON.parse(f.params || "{}"), reason: f.reason, evidence: f.evidence ?? [], investigation_id: f.investigation_id, incident_id: f.incident_id } });
      onDone?.(p.id);
    } catch (err) { setError(err); }
  }
  const spec = cat.data?.find((c) => c.action === f.action);
  return (
    <form className="stack" onSubmit={submit}>
      <div className="row">
        <label>Action<select value={f.action} onChange={(e) => setF({ ...f, action: e.target.value, target_kind: cat.data?.find((c) => c.action === e.target.value)?.target_kinds[0] })}>
          {cat.data?.map((c) => <option key={c.action} value={c.action}>{c.title}</option>)}</select></label>
        <label>Kind<select value={f.target_kind} onChange={(e) => setF({ ...f, target_kind: e.target.value })}>{spec?.target_kinds.map((k: string) => <option key={k}>{k}</option>)}</select></label>
        {f.target_kind !== "Repository" && <label>Name<input required value={f.target_name} onChange={(e) => setF({ ...f, target_name: e.target.value })} /></label>}
      </div>
      <label>Parameters (JSON){spec && <span className="muted"> — fields: {Object.keys(spec.params.properties ?? {}).join(", ") || "none"}</span>}
        <textarea className="mono" rows={4} value={f.params} onChange={(e) => setF({ ...f, params: e.target.value })} /></label>
      <label>Reason and supporting evidence<textarea rows={2} required minLength={5} value={f.reason} onChange={(e) => setF({ ...f, reason: e.target.value })} /></label>
      <ErrorBox error={error} />
      <div className="small muted">MOKS reads the live object, records its resourceVersion, and builds the impact, preconditions, validation and rollback for review.</div>
      <div><button>Create plan</button></div>
    </form>
  );
}

// ---- Diagnostics ------------------------------------------------------------------------------

export function Diagnostics() {
  const cat = useLoad(() => api<any>("/diagnostics/catalog"), []);
  const list = useLoad(() => api<any[]>("/diagnostics/sessions"), []);
  const [env, setEnv] = useState("");
  const [ns, setNs] = useState("");
  const [f, setF] = useState({ image: "netshoot", packages: [] as string[], ttl_minutes: 30, reason: "" });
  const [error, setError] = useState<unknown>();
  const nav = useNavigate();
  const onEnv = useCallback((e: string) => setEnv(e), []);
  const onNs = useCallback((n: string) => setNs(n), []);
  async function submit(e: FormEvent) {
    e.preventDefault();
    try { const s = await api<any>("/diagnostics/sessions", { method: "POST", body: { ...f, environment: env } }); nav(`/diagnostics/${s.id}`); } catch (err) { setError(err); }
  }
  const pkgs = Object.entries(cat.data?.packages ?? {}).filter(([, p]: any) => p.images.includes(f.image));
  return (
    <>
      <PageHead title="Diagnostic sessions" sub="Disposable, TTL-bound pods with approved tools. No shell; commands are catalog templates with validated arguments." />
      <form className="card stack" onSubmit={submit}>
        <div className="row"><ScopePicker env={env} ns={ns} onEnv={onEnv} onNs={onNs} />
          <label>Image<select value={f.image} onChange={(e) => setF({ ...f, image: e.target.value, packages: [] })}>{Object.entries(cat.data?.images ?? {}).map(([k, v]: any) => <option key={k} value={k}>{k} — {v.description}</option>)}</select></label>
          <label>TTL (min)<input type="number" min={5} max={240} value={f.ttl_minutes} onChange={(e) => setF({ ...f, ttl_minutes: Number(e.target.value) })} /></label></div>
        {pkgs.length > 0 && <div className="row">{pkgs.map(([k, p]: any) => <label key={k} className="inline"><input type="checkbox" checked={f.packages.includes(k)}
          onChange={(e) => setF({ ...f, packages: e.target.checked ? [...f.packages, k] : f.packages.filter((x) => x !== k) })} /> {p.spec} {p.elevated && <Badge kind="warn">elevated</Badge>}</label>)}</div>}
        <label>Reason<input required minLength={5} value={f.reason} onChange={(e) => setF({ ...f, reason: e.target.value })} /></label>
        <ErrorBox error={error} />
        <div className="small muted">Production environments and elevated packages need an approved plan before the pod starts.</div>
        <div><button>Request session</button></div>
      </form>
      <div className="card" style={{ marginTop: 14 }}>
        <table><thead><tr><th>Session</th><th>Image</th><th>Status</th><th>Expires</th></tr></thead>
          <tbody>{list.data?.map((s) => <tr key={s.id}><td><Link to={`/diagnostics/${s.id}`}>{s.reason}</Link></td><td className="mono small">{s.image}</td>
            <td><Badge kind={s.status === "ready" ? "ok" : s.status === "failed" ? "bad" : "neutral"}>{s.status}</Badge></td><td><Time iso={s.expires_at} /></td></tr>)}</tbody></table>
      </div>
    </>
  );
}

export function DiagnosticView() {
  const { id } = useParams();
  const { can } = useAuth();
  const s = useLoad(() => api<any>(`/diagnostics/sessions/${id}`), [id]);
  const cat = useLoad(() => api<any>("/diagnostics/catalog"), []);
  const [tool, setTool] = useState("");
  const [args, setArgs] = useState<Record<string, string>>({});
  const [error, setError] = useState<unknown>();
  const d = s.data;
  useEffect(() => {
    if (!d || !["requested", "starting", "ready"].includes(d.status)) return;
    const t = setInterval(s.reload, 3000);
    return () => clearInterval(t);
  }, [d, s.reload]);
  if (!d) return <ErrorBox error={s.error} />;
  const tools: string[] = cat.data?.images?.[d.image_key]?.tools ?? [];
  const spec = cat.data?.tools?.[tool];
  async function run(e: FormEvent) {
    e.preventDefault();
    setError(null);
    try { await api(`/diagnostics/sessions/${id}/commands`, { method: "POST", body: { tool, args: Object.fromEntries(Object.entries(args).filter(([, v]) => v)) } }); s.reload(); } catch (err) { setError(err); }
  }
  return (
    <>
      <PageHead title={`Diagnostic session ${d.pod ?? ""}`} sub={<span className="row small"><Badge kind={d.status === "ready" ? "ok" : "neutral"}>{d.status}</Badge><span className="mono">{d.image}</span><span>expires <Time iso={d.expires_at} /></span></span>}>
        {["ready", "starting", "requested"].includes(d.status) && <button className="danger" onClick={() => api(`/diagnostics/sessions/${id}/terminate`, { method: "POST" }).then(s.reload)}>Terminate</button>}
      </PageHead>
      {d.plan && d.status === "requested" && <div className="alert info" style={{ marginBottom: 12 }}>Start is governed by <Link to={`/actions/${d.plan.id}`}>action plan</Link> ({d.plan.status}).</div>}
      {d.error && <div className="alert error">{d.error}</div>}
      {d.status === "ready" && can("action.execute") && (
        <form className="card row" onSubmit={run}>
          <label>Tool<select value={tool} onChange={(e) => { setTool(e.target.value); setArgs({}); }}><option value="">choose…</option>{tools.map((t) => <option key={t} value={t}>{t} — {cat.data.tools[t].description}</option>)}</select></label>
          {spec?.args.map((a: string) => <label key={a}>{a}<input value={args[a] ?? ""} onChange={(e) => setArgs({ ...args, [a]: e.target.value })} /></label>)}
          <button disabled={!tool}>Run</button>
          <ErrorBox error={error} />
        </form>
      )}
      <div className="card" style={{ marginTop: 14 }}>
        <h2>Commands</h2>
        {d.commands.map((c: any) => <div key={c.id} style={{ marginBottom: 10 }}>
          <div className="small"><Badge kind={c.status === "done" && c.exit_code === 0 ? "ok" : c.status === "queued" || c.status === "running" ? "info" : "bad"}>{c.status}{c.exit_code != null ? ` (${c.exit_code})` : ""}</Badge> <span className="mono">{c.argv.join(" ")}</span></div>
          {c.output && <pre className="logbox">{c.output}</pre>}</div>)}
        {!d.commands.length && <div className="muted small">No commands yet.</div>}
      </div>
    </>
  );
}
