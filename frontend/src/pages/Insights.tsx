import { useCallback, useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { api, apiForm, qs } from "../api";
import { useAuth } from "../auth";
import { Badge, ErrorBox, LineChart, PageHead, ScopePicker, SourceRef, Time, useLoad } from "../components";
import { PlanForm } from "./Ops";

// ---- Knowledge ----------------------------------------------------------------------------------

export function Knowledge() {
  const { can } = useAuth();
  const [q, setQ] = useState("");
  const [res, setRes] = useState<any>();
  const [error, setError] = useState<unknown>();
  const status = useLoad(() => api<any>("/knowledge/status"), []);
  async function search(e: FormEvent) {
    e.preventDefault();
    setError(null);
    try { setRes(await api(`/knowledge/search${qs({ q, k: 10 })}`)); } catch (err) { setError(err); }
  }
  return (
    <>
      <PageHead title="Runbooks & knowledge" sub={status.data ? `${status.data.backend} index ${status.data.index?.name ?? "(not built yet)"} · ${status.data.index?.model ?? ""} · ${status.data.chunks} passages` : ""} />
      <form className="card row" onSubmit={search}>
        <input style={{ flex: 1 }} value={q} onChange={(e) => setQ(e.target.value)} placeholder="e.g. checkout 502 runbook, redis failover, rollback procedure" />
        <button disabled={q.length < 2}>Search</button>
      </form>
      <ErrorBox error={error} />
      {res && <div className="stack" style={{ marginTop: 14 }}>
        <div className="small muted">{res.results.length} results ({res.mode}). Only sources you are allowed to read are searched and returned.</div>
        {res.results.map((h: any) => (
          <div key={h.chunk_id} className="card">
            <div className="row between"><strong>{h.title}{h.heading && <span className="muted"> › {h.heading}</span>}</strong>
              <span className="small muted">{h.citation.source} · {h.scope.environment}/{h.scope.namespace}</span></div>
            <pre style={{ marginTop: 6 }}>{h.text.slice(0, 1200)}</pre>
            <div className="small" style={{ marginTop: 6 }}>
              {h.citation.url ? <a href={h.citation.url} target="_blank" rel="noreferrer noopener">{h.citation.path}{h.citation.lines?.[0] ? ` L${h.citation.lines[0]}-${h.citation.lines[1]}` : ""}</a> : h.citation.path}
              {h.citation.revision && <span className="mono muted"> @ {h.citation.revision.slice(0, 10)}</span>}
              <span className="muted"> · updated <Time iso={h.freshness.document_updated} /></span>
            </div>
          </div>
        ))}
      </div>}
      {can("admin.connectors") && <KnowledgeSources onChange={status.reload} />}
    </>
  );
}

function KnowledgeSources({ onChange }: { onChange: () => void }) {
  const { can } = useAuth();
  const list = useLoad(() => api<any[]>("/knowledge/sources"), []);
  const [f, setF] = useState({ name: "", type: "github", repository: "", include: "", scope_environment: "*", scope_namespace: "*", credType: "none", credName: "" });
  const [error, setError] = useState<unknown>();
  async function add(e: FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      await api("/knowledge/sources", { method: "POST", body: { name: f.name, type: f.type, repository: f.type === "github" ? f.repository : null,
        include: f.include ? f.include.split(/[\s,]+/).filter(Boolean) : [], scope_environment: f.scope_environment, scope_namespace: f.scope_namespace,
        credential_ref: f.credType === "env" ? { type: "env", name: f.credName } : { type: "none" } } });
      list.reload(); onChange();
    } catch (err) { setError(err); }
  }
  async function upload(id: string, file: File) {
    const form = new FormData();
    form.append("file", file);
    try { const r = await apiForm(`/knowledge/sources/${id}/upload`, form); alert(r.status === "excluded" ? `Excluded: ${r.excluded_reason}` : `Indexed ${r.path}`); list.reload(); onChange(); }
    catch (err) { setError(err); }
  }
  return (
    <div className="card stack" style={{ marginTop: 14 }}>
      <div className="row between"><h2>Sources</h2>{can("admin.llm") && <button className="ghost small" onClick={() => api("/knowledge/reindex", { method: "POST" }).then(() => alert("Reindex queued"))}>Rebuild index (embedding change)</button>}</div>
      <table>
        <thead><tr><th>Source</th><th>Scope</th><th>Documents</th><th>Last sync</th><th /></tr></thead>
        <tbody>{list.data?.map((s) => (
          <tr key={s.id}>
            <td>{s.name} <Badge>{s.type}</Badge><div className="small mono muted">{s.config?.repository}{s.revision ? ` @ ${s.revision.slice(0, 8)}` : ""}</div></td>
            <td className="mono small">{s.scope_environment}/{s.scope_namespace}</td>
            <td className="small">{Object.entries(s.documents).map(([k, v]) => `${v} ${k}`).join(", ") || "—"}</td>
            <td className="small">{s.last_sync_status && <Badge kind={s.last_sync_status === "ok" ? "ok" : "bad"}>{s.last_sync_status}</Badge>} <Time iso={s.last_sync_at} />
              {s.last_error && <div className="muted">{s.last_error}</div>}</td>
            <td className="row">
              {s.type === "github" && <button className="small" onClick={() => api(`/knowledge/sources/${s.id}/sync`, { method: "POST" }).then(() => setTimeout(list.reload, 1500))}>Sync</button>}
              {s.type === "upload" && <label className="btn small" style={{ cursor: "pointer" }}>Upload<input type="file" hidden accept=".md,.txt,.yaml,.yml,.json,.rst,.adoc,.sh,.py,.tf"
                onChange={(e) => e.target.files?.[0] && upload(s.id, e.target.files[0])} /></label>}
              <button className="ghost small" onClick={() => confirm(`Delete ${s.name} and remove its documents from search?`) && api(`/knowledge/sources/${s.id}`, { method: "DELETE" }).then(list.reload)}>Delete</button>
            </td>
          </tr>
        ))}</tbody>
      </table>
      <form className="stack" onSubmit={add}>
        <div className="row">
          <label>Name<input required pattern="[a-z0-9][a-z0-9-]*" value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} /></label>
          <label>Type<select value={f.type} onChange={(e) => setF({ ...f, type: e.target.value })}><option value="github">GitHub repository</option><option value="upload">Uploads</option></select></label>
          {f.type === "github" && <label>Repository<input required placeholder="owner/name" value={f.repository} onChange={(e) => setF({ ...f, repository: e.target.value })} /></label>}
          <label>Scope env<input value={f.scope_environment} onChange={(e) => setF({ ...f, scope_environment: e.target.value })} /></label>
          <label>Scope namespace<input value={f.scope_namespace} onChange={(e) => setF({ ...f, scope_namespace: e.target.value })} /></label>
        </div>
        {f.type === "github" && <div className="row">
          <label style={{ flex: 1 }}>Include globs (default: docs, runbooks, Markdown, YAML, Helm, Terraform, workflows)<input value={f.include} onChange={(e) => setF({ ...f, include: e.target.value })} placeholder="docs/** **/*.md k8s/**" /></label>
          <label>Token<select value={f.credType} onChange={(e) => setF({ ...f, credType: e.target.value })}><option value="none">public repo</option><option value="env">env variable</option></select></label>
          {f.credType === "env" && <label>Variable<input value={f.credName} onChange={(e) => setF({ ...f, credName: e.target.value })} placeholder="MOKS_CRED_GITHUB_TOKEN" /></label>}
        </div>}
        <ErrorBox error={error} />
        <div><button>Add source</button></div>
      </form>
    </div>
  );
}

// ---- FinOps -----------------------------------------------------------------------------------

const money = (v: number | null | undefined) => (v == null ? "—" : v >= 100 ? v.toFixed(0) : v.toFixed(2));

export function FinOps() {
  const { can } = useAuth();
  const [env, setEnv] = useState(localStorage.getItem("moks.env") ?? "");
  const [ns, setNs] = useState(localStorage.getItem("moks.ns") ?? "");
  const [days, setDays] = useState(7);
  const onEnv = useCallback((e: string) => setEnv(e), []);
  const onNs = useCallback((n: string) => setNs(n), []);
  const ok = (p: Promise<any>) => p.catch((e) => ({ error: e.message }));
  const alloc = useLoad(() => (env && ns ? ok(api(`/finops/${env}/k8s/allocation${qs({ namespace: ns, days })}`)) : Promise.resolve(null)), [env, ns, days]);
  const rs = useLoad(() => (env && ns ? ok(api(`/finops/${env}/k8s/rightsizing${qs({ namespace: ns, days })}`)) : Promise.resolve(null)), [env, ns, days]);
  const aws = useLoad(() => (env ? ok(api(`/finops/${env}/aws/costs${qs({ group_by: "service", days: 30 })}`)) : Promise.resolve(null)), [env]);
  const anomalies = useLoad(() => (env ? ok(api(`/finops/${env}/aws/anomalies${qs({ days: 30 })}`)) : Promise.resolve(null)), [env]);
  const budgets = useLoad(() => (env ? ok(api(`/finops/${env}/aws/budgets`)) : Promise.resolve(null)), [env]);
  const forecast = useLoad(() => (env ? ok(api(`/finops/${env}/aws/forecast${qs({ days: 30 })}`)) : Promise.resolve(null)), [env]);
  const llm = useLoad(() => (can("admin.llm") ? api<any>("/finops/llm?days=30") : Promise.resolve(null)), []);
  const [propose, setPropose] = useState<any>(null);
  const nav = useNavigate();
  return (
    <>
      <PageHead title="FinOps" sub="Cloud spend, Kubernetes cost allocation and rightsizing, and MOKS's own LLM spend">
        <ScopePicker env={env} ns={ns} onEnv={onEnv} onNs={onNs} />
        <label>Usage window<select value={days} onChange={(e) => setDays(Number(e.target.value))}>{[1, 7, 14, 30].map((d) => <option key={d} value={d}>{d} days</option>)}</select></label>
      </PageHead>
      <div className="grid cols-2">
        <div className="card">
          <h2>Kubernetes allocation · {ns}</h2>
          {alloc.data?.error ? <div className="alert warn small">{alloc.data.error}</div> : alloc.data && <>
            <div className="row"><div><div className="stat">{money(alloc.data.data.total)}</div><div className="small muted">est. cost, {days} d</div></div>
              <div style={{ marginLeft: 24 }}><div className="stat">{money(alloc.data.data.idle)}</div><div className="small muted">idle (requested, unused)</div></div></div>
            <div className="small muted" style={{ margin: "6px 0" }}>{alloc.data.data.cost_model}</div>
            <table><thead><tr><th>Workload</th><th>Pods</th><th>CPU req / used</th><th>Mem req / used</th><th>Cost</th><th>Idle</th></tr></thead>
              <tbody>{alloc.data.data.workloads.map((w: any) => <tr key={w.workload}><td className="mono small">{w.workload}</td><td>{w.pods}</td>
                <td className="small">{w.cpu_request} / {w.cpu_avg}</td><td className="small">{w.mem_request_mib} / {w.mem_avg_mib} MiB</td><td>{money(w.cost)}</td><td>{money(w.idle_cost)}</td></tr>)}</tbody></table>
          </>}
        </div>
        <div className="card">
          <h2>Rightsizing · {ns}</h2>
          {rs.data?.error ? <div className="alert warn small">{rs.data.error}</div> : rs.data && <>
            <div className="small muted">Potential saving {money(rs.data.data.monthly_saving)}/month from p95 CPU and max memory over {days} days (+25% headroom).</div>
            <table><thead><tr><th>Container</th><th>Action</th><th>Current → recommended</th><th>Saving / mo</th><th /></tr></thead>
              <tbody>{rs.data.data.recommendations.map((r: any, i: number) => <tr key={i}>
                <td className="mono small">{r.workload}/{r.container}<div className="muted">{r.findings.join("; ")}</div></td>
                <td><Badge kind={r.action === "increase" ? "bad" : r.action === "reduce" ? "ok" : "warn"}>{r.action}</Badge></td>
                <td className="small mono">cpu {r.current.cpu_request ?? "—"}→{r.recommended.cpu_request}<br />mem {r.current.mem_request ?? "—"}→{r.recommended.mem_request}</td>
                <td>{r.monthly_saving < 0 ? <span className="small">+{money(-r.monthly_saving)} cost</span> : money(r.monthly_saving)}</td>
                <td>{can("action.request") && r.workload.startsWith("Deployment/") && <button className="small ghost" onClick={() => setPropose(r)}>Propose</button>}</td>
              </tr>)}</tbody></table>
            {!rs.data.data.recommendations.length && <div className="empty">Requests match usage.</div>}
          </>}
        </div>
      </div>
      {propose && <div className="drawer"><div className="row between"><h2>Propose resize</h2><button className="ghost small" onClick={() => setPropose(null)}>Close</button></div>
        <PlanForm env={env} ns={ns} initial={{ action: "patch_resources", target_kind: "Deployment", target_name: propose.workload.split("/")[1],
          params: { container: propose.container, requests: { cpu: propose.recommended.cpu_request, memory: propose.recommended.mem_request },
            limits: propose.recommended.mem_limit ? { memory: propose.recommended.mem_limit } : {} },
          reason: `Rightsizing from ${days}-day usage: p95 CPU ${propose.observed.cpu_p95}, max memory ${propose.observed.mem_max}. ${propose.findings.join("; ")}` }}
          onDone={(id) => nav(`/actions/${id}`)} /></div>}
      <div className="grid cols-2" style={{ marginTop: 14 }}>
        <div className="card">
          <h2>AWS spend (30 days, by service)</h2>
          {aws.data?.error ? <div className="alert info small">{aws.data.error}</div> : aws.data && <>
            <div className="row"><div><div className="stat">{money(aws.data.data.total)} {aws.data.data.unit}</div><div className="small muted">{aws.data.data.metric}</div></div>
              {forecast.data && !forecast.data.error && <div style={{ marginLeft: 24 }}><div className="stat">{money(forecast.data.data.total)}</div><div className="small muted">forecast next 30 d</div></div>}</div>
            <LineChart series={aws.data.data.series} />
            <SourceRef source={aws.data.source} />
          </>}
        </div>
        <div className="card stack">
          <h2>Anomalies & budgets</h2>
          {anomalies.data?.error ? <div className="alert info small">{anomalies.data.error}</div> : anomalies.data?.data?.map((a: any) => (
            <div key={a.id} className="small"><Badge kind="warn">impact {money(a.impact)}</Badge> {a.start} – {a.end ?? "ongoing"} · {a.root_causes.map((c: any) => [c.Service, c.Region, c.UsageType].filter(Boolean).join(" ")).join("; ")}</div>))}
          {anomalies.data?.data?.length === 0 && <div className="small muted">No cost anomalies in 30 days.</div>}
          {budgets.data && !budgets.data.error && budgets.data.data.map((b: any) => (
            <div key={b.name} className="small"><strong>{b.name}</strong> {money(b.actual)} of {money(b.limit)} {b.unit} ({b.actual_pct}%)
              {b.forecast_pct != null && <Badge kind={b.forecast_pct >= 100 ? "bad" : b.forecast_pct >= 80 ? "warn" : "ok"}>forecast {b.forecast_pct}%</Badge>}</div>))}
        </div>
      </div>
      {llm.data && <div className="card" style={{ marginTop: 14 }}>
        <h2>MOKS LLM spend (30 days): ${llm.data.total_usd.toFixed(2)}</h2>
        <LineChart series={llm.data.series} />
      </div>}
    </>
  );
}

// ---- Evaluations --------------------------------------------------------------------------------

export function Evals() {
  const ds = useLoad(() => api<any[]>("/evals/datasets"), []);
  const runs = useLoad(() => api<any[]>("/evals/runs"), []);
  const [mode, setMode] = useState("rules");
  return (
    <>
      <PageHead title="Evaluation" sub="Replay recorded incidents through the real orchestrator and score the reports">
        <select value={mode} onChange={(e) => setMode(e.target.value)}><option value="rules">rules only</option><option value="llm">with the active LLM</option></select>
        {ds.data?.map((d) => <button key={d.name} onClick={() => api("/evals/runs", { method: "POST", body: { dataset: d.name, mode } }).then(() => setTimeout(runs.reload, 2000))}>Run {d.name} ({d.cases})</button>)}
        <button className="ghost" onClick={runs.reload}>Refresh</button>
      </PageHead>
      {runs.data?.map((r) => (
        <div key={r.id} className="card" style={{ marginBottom: 12 }}>
          <div className="row between"><h2>{r.dataset} · {r.mode}</h2><span>{r.score != null ? <Badge kind={r.score >= 0.9 ? "ok" : r.score >= 0.7 ? "warn" : "bad"}>score {r.score.toFixed(3)}</Badge> : <Badge>{r.status}</Badge>} <Time iso={r.created_at} /></span></div>
          <table><thead><tr><th>Case</th><th>Score</th><th>Failed checks</th><th>Findings</th><th>Tools</th></tr></thead>
            <tbody>{r.results.map((c: any) => <tr key={c.case}><td className="mono small">{c.case}</td><td>{c.score.toFixed(2)}</td>
              <td className="small">{Object.entries(c.checks).filter(([, v]) => !v).map(([k]) => k).join(", ") || "—"}</td>
              <td className="small">{c.findings.join(", ")}</td><td>{c.tool_calls}</td></tr>)}</tbody></table>
        </div>
      ))}
    </>
  );
}

// ---- SRE skills -------------------------------------------------------------------------------

export function Skills() {
  const { can } = useAuth();
  const list = useLoad(() => api<any>("/skills"), []);
  const [open, setOpen] = useState<string | null>(null);
  const detail = useLoad(() => (open ? api<any>(`/skills/${open}`) : Promise.resolve(null)), [open]);
  return (
    <>
      <PageHead title="SRE skills" sub="Playbooks from SKILL.md files. MOKS selects them from the question and detector findings, runs their read-only steps and follows their guidance.">
        {can("admin.policies") && <button className="ghost" onClick={() => api("/skills/reload", { method: "POST" }).then(list.reload)}>Reload</button>}
      </PageHead>
      {list.data?.errors?.length > 0 && <div className="alert error" style={{ marginBottom: 12 }}>{list.data.errors.map((e: any) => <div key={e.path} className="small">{e.path}: {e.error}</div>)}</div>}
      <div className="grid" style={{ gridTemplateColumns: "minmax(0, 2fr) minmax(0, 3fr)", alignItems: "start" }}>
        <div className="card">
          <table><tbody>{list.data?.skills.map((s: any) => (
            <tr key={s.name} className="clickable" onClick={() => setOpen(s.name)}>
              <td><strong className="mono">{s.name}</strong> {s.source === "custom" && <Badge kind="info">custom</Badge>}<div className="small muted">{s.description}</div>
                <div className="small">{s.triggers.findings.map((f: string) => <Badge key={f} kind="warn">{f}</Badge>)} {s.tools.map((t: string) => <Badge key={t}>{t}</Badge>)}</div></td>
            </tr>
          ))}</tbody></table>
        </div>
        <div className="card">
          {!detail.data ? <div className="empty">Select a skill.</div> : <>
            <h2 className="mono">{detail.data.name}</h2>
            <p className="muted">{detail.data.description}</p>
            <h3>Triggers</h3><div className="small">keywords: {detail.data.triggers.keywords.join(", ") || "—"} · findings: {detail.data.triggers.findings.join(", ") || "—"}</div>
            <h3 style={{ marginTop: 12 }}>Steps (read-only, policy-checked)</h3>
            <ol className="small">{detail.data.steps.map((s: any, i: number) => <li key={i}><span className="mono">{s.tool}</span> {s.for_each && <Badge>{s.for_each} ×{s.limit}</Badge>} {s.purpose && <span className="muted">— {s.purpose}</span>}
              <div className="mono muted">{JSON.stringify(s.args)}</div></li>)}</ol>
            {detail.data.remediation.length > 0 && <><h3>Remediation hints</h3><ul className="small">{detail.data.remediation.map((r: any, i: number) => <li key={i}>
              when <Badge kind="warn">{r.when}</Badge> → <span className="mono">{r.action}</span> {r.target} <span className="muted">{r.note}</span></li>)}</ul></>}
            <h3>Guidance</h3><pre className="logbox">{detail.data.body}</pre>
            <div className="small muted">{detail.data.path}</div>
          </>}
        </div>
      </div>
    </>
  );
}
