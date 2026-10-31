import { useCallback, useEffect, useState, type FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, API, qs } from "../api";
import { useAuth } from "../auth";
import { Badge, DataTable, ErrorBox, Loading, PageHead, ScopePicker, StatusBadge, Time, useInterval, useLoad } from "../components";
import { type Chip, FilterPanel, useUrlFilters } from "../filters";
import { Empty, Truncate, useFeedback } from "../ui";

const SEV: Record<string, "bad" | "warn" | "info" | "neutral"> = { sev1: "bad", sev2: "bad", sev3: "warn", sev4: "neutral" };
const PLAN: Record<string, "ok" | "warn" | "bad" | "info" | "neutral"> = {
  reconciliation_required: "bad", reconciled: "neutral", pending: "info", approved: "ok", queued: "info", executing: "info", succeeded: "ok", recovered: "warn", failed: "bad", rejected: "bad", expired: "neutral", cancelled: "neutral",
};

// ---- Incidents --------------------------------------------------------------------------------

export function Incidents() {
  const { can } = useAuth();
  const nav = useNavigate();
  const [f, set, clear] = useUrlFilters({ status: "", severity: "", environment: "", q: "" });
  const list = useLoad(() => api<any[]>(`/incidents${qs({ status: f.status })}`), [f.status]);
  const rows = (list.data ?? []).filter((i) => (!f.severity || i.severity === f.severity) && (!f.environment || i.environment === f.environment) && (!f.q || i.title.toLowerCase().includes(f.q.toLowerCase())));
  const envs = [...new Set((list.data ?? []).map((i) => i.environment as string))].sort();
  const chips: Chip[] = [
    ...(f.status ? [{ key: "st", label: f.status, onRemove: () => set({ status: "" }) }] : []), ...(f.severity ? [{ key: "sv", label: f.severity, onRemove: () => set({ severity: "" }) }] : []),
    ...(f.environment ? [{ key: "e", label: f.environment, onRemove: () => set({ environment: "" }) }] : []), ...(f.q ? [{ key: "q", label: `title has ${f.q}`, onRemove: () => set({ q: "" }) }] : []),
  ];
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
      <PageHead title="Incidents" sub="Incident workspaces: timeline, evidence, hypotheses, actions and validation" />
      <FilterPanel chips={chips} onClear={clear}
        search={<input type="search" className="grow" aria-label="Search incidents" value={f.q} onChange={(e) => set({ q: e.target.value })} placeholder="Search titles" />}>
        <label>Status<select aria-label="Filter by status" value={f.status} onChange={(e) => set({ status: e.target.value })}><option value="">All</option>{["open", "mitigated", "resolved", "closed"].map((s) => <option key={s}>{s}</option>)}</select></label>
        <label>Severity<select value={f.severity} onChange={(e) => set({ severity: e.target.value })}><option value="">All</option>{Object.keys(SEV).map((s) => <option key={s}>{s}</option>)}</select></label>
        <label>Environment<select value={f.environment} onChange={(e) => set({ environment: e.target.value })}><option value="">All</option>{envs.map((e) => <option key={e}>{e}</option>)}</select></label>
      </FilterPanel>
      <div className="card mt">
        <Loading load={list} rows={5} />
        {list.data && <DataTable label="Incidents" rows={rows} rowKey={(i) => i.id} empty={<Empty title={chips.length ? "No incidents match." : "No incidents."}>{chips.length ? "Clear a filter to see more." : "Open one below, or enable detection on an environment."}</Empty>} cols={[
          { key: "t", head: "Incident", primary: true, cell: (i) => <><Link className="clamp-2" title={i.title} to={`/incidents/${i.id}`}>{i.title}</Link>{i.postmortem_status !== "none" && <span className="small muted">postmortem {i.postmortem_status}</span>}</> },
          { key: "sev", head: "Severity", status: true, cell: (i) => <Badge kind={SEV[i.severity]}>{i.severity}</Badge> },
          { key: "st", head: "Status", status: true, cell: (i) => <Badge kind={i.status === "open" ? "bad" : i.status === "mitigated" ? "warn" : "ok"}>{i.status}</Badge> },
          { key: "scope", head: "Scope", cell: (i) => <Truncate text={`${i.environment}/${i.namespace}`} mono />, meta: (i) => `${i.environment}/${i.namespace}` },
          { key: "src", head: "Source", wide: true, cell: (i) => i.source },
          { key: "at", head: "Updated", cell: (i) => <Time iso={i.updated_at} /> },
        ]} />}
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
          <div className="form-foot"><button>Open incident</button></div>
        </form>
      )}
      <div className="card" style={{ marginTop: 14 }}>
        <h2>Recent alerts</h2>
        {alerts.data && <DataTable label="Recent alerts" rows={alerts.data.slice(0, 30)} rowKey={(a) => a.id}
          empty={<Empty title="No alerts received.">Enable auto-investigate on an environment or point Alertmanager or Grafana at the webhook.</Empty>} cols={[
          { key: "a", head: "Alert", primary: true, cell: (a) => <><Truncate text={a.alertname} />{a.annotations?.summary && <span className="small muted clamp-2">{a.annotations.summary}</span>}</> },
          { key: "st", head: "Status", status: true, cell: (a) => <Badge kind={a.status === "firing" ? "bad" : "ok"}>{a.status}</Badge> },
          { key: "d", head: "Decision", cell: (a) => <span className="small">{a.decision}{a.incident_id && <> · <Link to={`/incidents/${a.incident_id}`}>incident</Link></>}{a.investigation_id && <> · <Link to={`/investigations/${a.investigation_id}`}>investigation</Link></>}</span> },
          { key: "s", head: "Scope", cell: (a) => <Truncate text={`${a.environment}/${a.labels?.namespace ?? "*"}`} mono />, meta: (a) => `${a.environment}/${a.labels?.namespace ?? "*"}` },
          { key: "at", head: "Received", cell: (a) => <Time iso={a.received_at} /> },
        ]} />}
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
  if (!d) return <Loading load={inc} rows={8} />;
  const write = can("incident.write");
  return (
    <>
      <PageHead title={d.title} sub={<span className="row small"><Badge kind={SEV[d.severity]}>{d.severity}</Badge><Badge>{d.status}</Badge><span className="mono">{d.environment}/{d.namespace}</span><span>source {d.source}</span><span>opened <Time iso={d.created_at} /></span></span>}>
        <a className="btn ghost" href={`${API}/incidents/${id}/handover.md`} target="_blank" rel="noreferrer">Handover</a>
        <a className="btn ghost" href={`${API}/incidents/${id}/report.md`} target="_blank" rel="noreferrer">Report (Markdown)</a>
        {write && <select aria-label="Incident status" value={d.status} onChange={(e) => act(() => api(`/incidents/${id}`, { method: "PATCH", body: { status: e.target.value } }))}>
          {["open", "mitigated", "resolved", "closed"].map((s) => <option key={s}>{s}</option>)}</select>}
        <Link className="btn" to={`/investigate?env=${d.environment}&ns=${d.namespace === "*" ? "" : d.namespace}&incident=${id}&q=${encodeURIComponent(d.title)}`}>Investigate</Link>
      </PageHead>
      <ErrorBox error={error} />
      <div className="grid split-3-2">
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
              {write && <select aria-label="Hypothesis status" style={{ marginLeft: 6 }} value={h.status} onChange={(e) => act(() => api(`/incidents/${id}`, { method: "PATCH", body: { hypotheses: d.hypotheses.map((x: any, j: number) => j === i ? { ...x, status: e.target.value } : x) } }))}>
                {["open", "supported", "refuted", "verified"].map((s) => <option key={s}>{s}</option>)}</select>}</li>)}</ul>
            {!d.hypotheses.length && <div className="muted small">No hypotheses yet.</div>}
            {write && <form className="row" onSubmit={(e) => { e.preventDefault(); act(() => api(`/incidents/${id}`, { method: "PATCH", body: { hypotheses: [...d.hypotheses, { ...hyp, status: "open" }] } })).then(() => setHyp({ text: "", confidence: "low" })); }}>
              <input aria-label="Suspected cause" style={{ flex: 1 }} required value={hyp.text} onChange={(e) => setHyp({ ...hyp, text: e.target.value })} placeholder="Suspected cause" />
              <select aria-label="Confidence" value={hyp.confidence} onChange={(e) => setHyp({ ...hyp, confidence: e.target.value })}><option>low</option><option>medium</option><option>high</option></select>
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
            <textarea aria-label="Timeline note" rows={2} required value={note.text} onChange={(e) => setNote({ ...note, text: e.target.value })} placeholder="Note, evidence, communication…" />
            <div className="row"><select aria-label="Entry type" value={note.kind} onChange={(e) => setNote({ ...note, kind: e.target.value })}>{["note", "evidence", "validation", "communication"].map((k) => <option key={k}>{k}</option>)}</select><button className="small">Add</button></div>
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
  return (
    <DataTable label="Action plans" rows={rows} rowKey={(p) => p.id} empty={<Empty title="Nothing here." />} cols={[
      { key: "a", head: "Action", primary: true, cell: (p) => <Link className="clamp-2" title={p.title} to={`/actions/${p.id}`}>{p.title}</Link> },
      { key: "st", head: "Status", status: true, cell: (p) => <><Badge kind={PLAN[p.status]}>{p.status}</Badge>{p.requires_approval && <Badge kind="neutral">approval</Badge>}</> },
      { key: "t", head: "Target", cell: (p) => <Truncate text={`${p.environment}/${p.namespace} ${p.target}`} mono />, meta: (p) => `${p.environment}/${p.namespace} ${p.target}` },
      { key: "by", head: "Requested by", cell: (p) => p.requested_by ?? "—" },
      { key: "exp", head: "Expires", cell: (p) => <Time iso={p.expires_at} /> },
    ]} />
  );
}

export function PlanView() {
  const { id } = useParams();
  const { can, me } = useAuth();
  const plan = useLoad(() => api<any>(`/actions/plans/${id}`), [id]);
  const [comment, setComment] = useState("");
  const [outcome, setOutcome] = useState("not_applied");
  const [forceReason, setForceReason] = useState("");
  const [error, setError] = useState<unknown>();
  const { confirm } = useFeedback();
  const p = plan.data;
  useInterval(plan.reload, 2500, !!p && ["queued", "executing"].includes(p.status));
  async function act(path: string, body?: any) {
    setError(null);
    try { await api(`/actions/plans/${id}/${path}`, { method: "POST", body }); plan.reload(); } catch (e) { setError(e); }
  }
  if (!p) return <Loading load={plan} rows={8} />;
  const mine = p.requested_by === me?.email;
  const overdue = new Date(p.expires_at) <= new Date();
  const canForce = can("admin.policies") && (p.status === "expired" || (p.status === "pending" && (p.requires_approval || overdue)));
  async function forceApprove() {
    if (await confirm({ title: "Force approve this plan?", body: `${p.title} on ${p.target} in ${p.environment}/${p.namespace}. Your reason goes into the audit log.`, action: "Force approve", danger: true }))
      await act("force-approve", { plan_hash: p.plan_hash, reason: forceReason });
  }
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
        <div className="stack plan-decision">
          <div className="card stack">
            <h2>Decision</h2>
            {p.approvals.map((a: any, i: number) => <div key={i} className="small"><Badge kind={a.decision === "approve" ? "ok" : "bad"}>{a.decision}</Badge> {a.approver} <Time iso={a.at} /> {a.comment && `— ${a.comment}`}</div>)}
            {p.status === "pending" && p.requires_approval && can("action.approve") && (!mine || can("admin.policies")) && <>
              <textarea aria-label="Comment" rows={2} value={comment} onChange={(e) => setComment(e.target.value)} placeholder="Comment (optional)" />
              <div className="row"><button onClick={() => act("approve", { plan_hash: p.plan_hash, comment })}>Approve this exact plan</button>
                <button className="danger" onClick={() => act("reject", { plan_hash: p.plan_hash, comment })}>Reject</button></div>
            </>}
            {mine && !can("admin.policies") && p.status === "pending" && p.requires_approval && <div className="small muted">Waiting for someone else to approve (you requested it).</div>}
            {p.status === "pending" && !p.requires_approval && <div className="small muted">No approval needed: an administrator requested this, or this action does not need one. Review it, then execute. This is recorded in the audit log.</div>}
            {canForce && <details className="stack">
              <summary>Force approve</summary>
              <div className="small muted">Administrator override. {p.status === "expired" || overdue ? "This plan's time ran out: it gets a new window. The evidence and the object may have changed since it was written." : "This replaces the normal approval."} Execute is still a separate step, and it stops if the object changed.</div>
              <textarea aria-label="Reason for force approving" rows={2} value={forceReason} onChange={(e) => setForceReason(e.target.value)} placeholder="Why this cannot wait (required, recorded in the audit log)" />
              <button className="danger" disabled={forceReason.trim().length < 10} onClick={forceApprove}>Force approve this exact plan</button>
            </details>}
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
                <textarea rows={2} value={comment} onChange={(e) => setComment(e.target.value)} aria-label="How you verified this" placeholder="How you verified this (required)" />
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
      <div className="form-foot"><button>Create plan</button></div>
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
  const [d, set, clear] = useUrlFilters({ status: "" });
  const statuses = [...new Set((list.data ?? []).map((s) => s.status as string))].sort();
  const sessions = (list.data ?? []).filter((s) => !d.status || s.status === d.status);
  const chips: Chip[] = d.status ? [{ key: "s", label: d.status, onRemove: () => set({ status: "" }) }] : [];
  const pkgs = Object.entries(cat.data?.packages ?? {}).filter(([, p]: any) => p.images.includes(f.image));
  return (
    <>
      <PageHead title="Diagnostic sessions" sub="Disposable, time-limited pods for testing DNS, ports, certificates and HTTP from inside the cluster. No shell; commands are templates with validated arguments." />
      <form className="card stack" onSubmit={submit}>
        <div className="row"><ScopePicker env={env} ns={ns} onEnv={onEnv} onNs={onNs} />
          <label>Image<select value={f.image} onChange={(e) => setF({ ...f, image: e.target.value, packages: [] })}>{Object.entries(cat.data?.images ?? {}).map(([k, v]: any) => <option key={k} value={k}>{k} — {v.description}</option>)}</select></label>
          <label>TTL (min)<input type="number" min={5} max={240} value={f.ttl_minutes} onChange={(e) => setF({ ...f, ttl_minutes: Number(e.target.value) })} /></label></div>
        {pkgs.length > 0 && <div className="row">{pkgs.map(([k, p]: any) => <label key={k} className="inline"><input type="checkbox" checked={f.packages.includes(k)}
          onChange={(e) => setF({ ...f, packages: e.target.checked ? [...f.packages, k] : f.packages.filter((x) => x !== k) })} /> {p.spec} {p.elevated && <Badge kind="warn">elevated</Badge>}</label>)}</div>}
        <label>Reason<input required minLength={5} value={f.reason} onChange={(e) => setF({ ...f, reason: e.target.value })} /></label>
        <ErrorBox error={error} />
        <div className="small muted">The pod starts right away, with no approval. Elevated packages run as root inside the pod. Everything is recorded in the audit log.</div>
        <div className="form-foot"><button>Start session</button></div>
      </form>
      <FilterPanel chips={chips} onClear={clear}>
        <label>Status<select value={d.status} onChange={(e) => set({ status: e.target.value })}><option value="">All</option>{statuses.map((s) => <option key={s}>{s}</option>)}</select></label>
      </FilterPanel>
      <div className="card mt">
        <Loading load={list} rows={4} />
        {list.data && <DataTable label="Diagnostic sessions" rows={sessions} rowKey={(s) => s.id} empty={<Empty title={chips.length ? "No sessions match." : "No diagnostic sessions yet."} />} cols={[
          { key: "r", head: "Session", primary: true, cell: (s) => <Link className="clamp-2" title={s.reason} to={`/diagnostics/${s.id}`}>{s.reason}</Link> },
          { key: "st", head: "Status", status: true, cell: (s) => <Badge kind={s.status === "ready" ? "ok" : s.status === "failed" ? "bad" : "neutral"}>{s.status}</Badge> },
          { key: "i", head: "Image", cell: (s) => <Truncate text={s.image} mono />, meta: (s) => s.image },
          { key: "x", head: "Expires", cell: (s) => <Time iso={s.expires_at} /> },
        ]} />}
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
  const [target, setTarget] = useState("");
  const [error, setError] = useState<unknown>();
  const d = s.data;
  useEffect(() => {
    if (!d || !["requested", "starting", "ready"].includes(d.status)) return;
    const t = setInterval(s.reload, 3000);
    return () => clearInterval(t);
  }, [d, s.reload]);
  if (!d) return <Loading load={s} rows={6} />;
  const tools: string[] = cat.data?.images?.[d.image_key]?.tools ?? [];
  const spec = cat.data?.tools?.[tool];
  async function check(e: FormEvent) {
    e.preventDefault();
    setError(null);
    try { await api(`/diagnostics/sessions/${id}/check`, { method: "POST", body: { target } }); s.reload(); } catch (err) { setError(err); }
  }
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
      {d.plan && d.status === "requested" && (d.plan.status === "pending"
        ? <div className="alert info" style={{ marginBottom: 12 }}>Waiting for someone who can execute in this environment to start <Link to={`/actions/${d.plan.id}`}>the session</Link>.</div>
        : <div className="alert info" style={{ marginBottom: 12 }}>Starting the pod. This usually takes under a minute.</div>)}
      {d.error && <div className="alert error">{d.error}</div>}
      {d.status === "ready" && can("action.execute") && <form className="card stack" onSubmit={check} style={{ marginBottom: 12 }}>
        <h2>Quick check</h2>
        <label>Host, host:port or URL<input required value={target} placeholder="checkout.shop.svc.cluster.local:8080" onChange={(e) => setTarget(e.target.value)} /></label>
        <div className="row"><button disabled={!target.trim()}>Check it</button><span className="small muted">Runs the DNS, port, certificate and HTTP checks this image supports.</span></div>
      </form>}
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
          {c.finding && <div className="small"><strong>{c.finding}</strong></div>}
          {c.output && <pre className="logbox">{c.output}</pre>}</div>)}
        {!d.commands.length && <div className="muted small">No commands yet.</div>}
      </div>
    </>
  );
}
