import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { api, apiForm, qs } from "../api";
import { useAuth } from "../auth";
import { Badge, type Env, ErrorBox, LineChart, PageHead, Stat, Time, useInterval, useLoad, rowAction } from "../components";
import { PlanForm } from "./Ops";
import { useFeedback } from "../ui";

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
        <input aria-label="Search knowledge" style={{ flex: 1 }} value={q} onChange={(e) => setQ(e.target.value)} placeholder="e.g. checkout 502 runbook, redis failover, rollback procedure" />
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

const SCHEDULES: [number, string][] = [[0, "Off"], [60, "Hourly"], [360, "Every 6 hours"], [1440, "Daily"], [10080, "Weekly"]];

function SyncState({ s }: { s: any }) {
  const kind = !s.last_sync_status ? "neutral" : s.last_sync_status === "ok" ? "ok" : s.last_sync_status === "error" ? "bad" : "warn";
  const c = s.last_counts;
  return (
    <>
      {s.last_sync_status ? <Badge kind={kind}>{s.last_sync_status}</Badge> : <Badge>not synced yet</Badge>} {s.stale && <Badge kind="warn">stale</Badge>}
      {s.sync_state !== "idle" && <Badge kind="info">{s.sync_state}</Badge>}
      <div className="muted">last success <Time iso={s.last_success_at} /> · last attempt <Time iso={s.last_attempt_at} /></div>
      {c && <div className="muted">+{c.added} added, {c.updated} updated, {c.deleted} deleted, {c.excluded} excluded</div>}
      {c?.failed > 0 && <div><Badge kind="warn">{c.failed} files failed</Badge></div>}
      {s.last_error && <div className="muted">{s.last_error}</div>}
    </>
  );
}

function KnowledgeSources({ onChange }: { onChange: () => void }) {
  const { confirm: ask, toast } = useFeedback();
  const { can } = useAuth();
  const list = useLoad(() => api<any[]>("/knowledge/sources"), []);
  useInterval(() => { if (!list.loading && list.data?.some((s) => s.sync_state !== "idle")) list.reload(); }, 3000);
  const [f, setF] = useState({ name: "", type: "github", repository: "", include: "", scope_environment: "*", scope_namespace: "*", credType: "none", credName: "" });
  const [error, setError] = useState<unknown>();
  const [editing, setEditing] = useState<any>(null);
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
  function startEdit(s: any) {
    setEditing({ id: s.id, name: s.name, github: s.type === "github", repository: s.config?.repository, include: (s.config?.include ?? []).join(", "), exclude: (s.config?.exclude ?? []).join(", "),
      scope_environment: s.scope_environment, scope_namespace: s.scope_namespace, owner: s.owner ?? "", service: s.service ?? "", enabled: s.enabled,
      credType: s.credential_ref?.type === "env" ? "env" : "none", credName: s.credential_ref?.name ?? "" });
  }
  async function saveEdit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    const globs = (v: string) => v.split(/[\s,]+/).filter(Boolean);
    try {
      await api(`/knowledge/sources/${editing.id}`, { method: "PUT", body: { scope_environment: editing.scope_environment, scope_namespace: editing.scope_namespace,
        owner: editing.owner || null, service: editing.service || null, enabled: editing.enabled,
        ...(editing.github ? { include: globs(editing.include), exclude: globs(editing.exclude), credential_ref: editing.credType === "env" ? { type: "env", name: editing.credName } : { type: "none" } } : {}) } });
      setEditing(null); list.reload(); onChange();
    } catch (err) { setError(err); }
  }
  const ed = (k: string, v: unknown) => setEditing({ ...editing, [k]: v });
  async function upload(id: string, file: File) {
    const form = new FormData();
    form.append("file", file);
    try { const r = await apiForm(`/knowledge/sources/${id}/upload`, form); toast(r.status === "excluded" ? `Excluded: ${r.excluded_reason}` : `Indexed ${r.path}`); list.reload(); onChange(); }
    catch (err) { setError(err); }
  }
  return (
    <div className="card stack" style={{ marginTop: 14 }}>
      <div className="row between"><h2>Sources</h2>{can("admin.llm") && <button className="ghost small" onClick={() => api("/knowledge/reindex", { method: "POST" }).then(() => toast("Reindex queued"), (e) => toast(e.message, "bad"))}>Rebuild index (embedding change)</button>}</div>
      <table>
        <thead><tr><th>Source</th><th>Scope</th><th>Documents</th><th>Sync</th><th>Schedule</th><th /></tr></thead>
        <tbody>{list.data?.map((s) => (
          <tr key={s.id}>
            <td>{s.name} <Badge>{s.type}</Badge><div className="small mono muted">{s.config?.repository}{s.revision ? ` @ ${s.revision.slice(0, 8)}` : ""}</div></td>
            <td className="mono small">{s.scope_environment}/{s.scope_namespace}</td>
            <td className="small">{Object.entries(s.documents).map(([k, v]) => `${v} ${k}`).join(", ") || "—"}</td>
            <td className="small">{s.syncable ? <SyncState s={s} /> : <span className="muted">Managed by uploads</span>}</td>
            <td className="small">{s.syncable ? <>
              <select aria-label={`Schedule for ${s.name}`} value={s.sync_interval_minutes} onChange={(e) => api(`/knowledge/sources/${s.id}/schedule`, { method: "PUT", body: { sync_interval_minutes: Number(e.target.value) } }).then(list.reload)}>
                {!SCHEDULES.some(([m]) => m === s.sync_interval_minutes) && <option value={s.sync_interval_minutes}>every {s.sync_interval_minutes} min</option>}
                {SCHEDULES.map(([m, l]) => <option key={m} value={m}>{l}</option>)}
              </select>
              {s.next_sync_at && <div className="muted">next {new Date(s.next_sync_at).toLocaleString()} ({Intl.DateTimeFormat().resolvedOptions().timeZone})</div>}
            </> : "—"}</td>
            <td className="row">
              {s.syncable && <button className="small" disabled={s.sync_state !== "idle"} onClick={() => api(`/knowledge/sources/${s.id}/sync`, { method: "POST" }).then(list.reload)}>{s.sync_state === "idle" ? "Sync now" : s.sync_state === "running" ? "Syncing…" : "Queued…"}</button>}
              {s.type === "upload" && <label className="btn small" style={{ cursor: "pointer" }}>Upload<input type="file" hidden accept=".md,.txt,.yaml,.yml,.json,.rst,.adoc,.sh,.py,.tf"
                onChange={(e) => e.target.files?.[0] && upload(s.id, e.target.files[0])} /></label>}
              <button className="ghost small" onClick={() => startEdit(s)}>Edit</button>
              <button className="ghost small" onClick={async () => { if (await ask({ title: `Delete ${s.name}?`, body: "This removes its documents from search.", action: "Delete source", danger: true })) api(`/knowledge/sources/${s.id}`, { method: "DELETE" }).then(list.reload); }}>Delete</button>
            </td>
          </tr>
        ))}</tbody>
      </table>
      {editing && <form className="stack" onSubmit={saveEdit} aria-label={`Edit ${editing.name}`}>
        <h3>Edit {editing.name}{editing.repository ? ` (${editing.repository})` : ""}</h3>
        <div className="row">
          <label>Scope env<input value={editing.scope_environment} onChange={(e) => ed("scope_environment", e.target.value)} /></label>
          <label>Scope namespace<input value={editing.scope_namespace} onChange={(e) => ed("scope_namespace", e.target.value)} /></label>
          <label>Owner<input value={editing.owner} onChange={(e) => ed("owner", e.target.value)} /></label>
          <label>Service<input value={editing.service} onChange={(e) => ed("service", e.target.value)} /></label>
          <label className="inline"><input type="checkbox" checked={editing.enabled} onChange={(e) => ed("enabled", e.target.checked)} /> enabled</label>
        </div>
        {editing.github && <div className="row">
          <label style={{ flex: 1 }}>Include globs<input value={editing.include} onChange={(e) => ed("include", e.target.value)} placeholder="default set" /></label>
          <label style={{ flex: 1 }}>Exclude globs<input value={editing.exclude} onChange={(e) => ed("exclude", e.target.value)} /></label>
          <label>Token<select value={editing.credType} onChange={(e) => ed("credType", e.target.value)}><option value="none">shared token, or none</option><option value="env">env variable</option></select></label>
          {editing.credType === "env" && <label>Variable<input required pattern="MOKS_CRED_[A-Z0-9_]+" value={editing.credName} onChange={(e) => ed("credName", e.target.value)} placeholder="MOKS_CRED_GITHUB_TOKEN" /></label>}
        </div>}
        <div className="small muted">Indexed documents are kept. A new scope applies straight away; new include or exclude rules apply at the next sync, which removes files that no longer qualify. The value of a token is never entered here: this names the variable that holds it.</div>
        <div className="row"><button>Save</button><button type="button" className="ghost" onClick={() => setEditing(null)}>Cancel</button></div>
      </form>}
      <form className="stack" onSubmit={add}>
        <div className="row">
          <label>Name<input required pattern="[a-z0-9][a-z0-9\-]*" value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} /></label>
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

const money = (v: number | null | undefined) => (v == null ? "—" : v >= 100 ? v.toFixed(0) : v >= 1 || v === 0 ? v.toFixed(2) : v.toFixed(4));

function Share({ value, total }: { value: number; total: number }) {
  const pct = total > 0 ? Math.round((value / total) * 100) : 0;
  return <div className="row"><div className="bar" style={{ flex: 1 }}><span style={{ width: `${pct}%` }} /></div><span className="small muted">{pct}%</span></div>;
}

export function FinOps() {
  const { can } = useAuth();
  const nav = useNavigate();
  const envs = useLoad(() => api<Env[]>("/environments"), []);
  const [env, setEnv] = useState("all");
  const [ns, setNs] = useState("");
  const [days, setDays] = useState(7);
  const [text, setText] = useState("");
  const [kind, setKind] = useState("");
  const [propose, setPropose] = useState<any>(null);
  const [analyzing, setAnalyzing] = useState(false);
  const [analyzeError, setAnalyzeError] = useState<unknown>();
  const one = env !== "all";
  const nss = useLoad(() => (one ? api<{ namespaces: string[] }>(`/explore/${env}/namespaces`).catch(() => null) : Promise.resolve(null)), [env]);
  const ov = useLoad(() => api<any>(`/finops/overview${qs({ environment: env, namespace: ns || undefined, days })}`), [env, ns, days]);
  const d = ov.data;
  const t = d?.totals;
  const rows: any[] = d?.environments ?? [];
  const hasAws = one && rows.some((r) => r.aws || r.errors?.aws); // Cost Explorer calls are billed: only ask when connected
  const ok = (p: Promise<any>) => p.catch((e) => ({ error: e.message }));
  const anomalies = useLoad(() => (hasAws ? ok(api(`/finops/${env}/aws/anomalies${qs({ days: 30 })}`)) : Promise.resolve(null)), [env, hasAws]);
  const budgets = useLoad(() => (hasAws ? ok(api(`/finops/${env}/aws/budgets`)) : Promise.resolve(null)), [env, hasAws]);
  const forecast = useLoad(() => (hasAws ? ok(api(`/finops/${env}/aws/forecast${qs({ days: 30 })}`)) : Promise.resolve(null)), [env, hasAws]);
  const tag = (r: any) => (x: any) => ({ ...x, environment: r.environment });
  const workloads = rows.flatMap((r) => (r.workloads ?? []).map(tag(r))).sort((a, b) => b.cost - a.cost);
  const recs = rows.flatMap((r) => (r.rightsizing ?? []).map(tag(r)));
  const aws = rows.flatMap((r) => (r.aws?.groups ?? []).map(tag(r))).sort((a, b) => b.amount - a.amount);
  const kinds = [...new Set(workloads.map((w) => w.kind))];
  const match = (w: any) => (!kind || w.kind === kind) && (!text || JSON.stringify([w.workload, w.namespace, w.environment]).toLowerCase().includes(text.toLowerCase()));
  const shownWorkloads = workloads.filter(match);
  const saving = rows.reduce((n, r) => n + (r.monthly_saving ?? 0), 0);
  const unpriced = rows.some((r) => r.kubernetes && !r.kubernetes.unit_prices.cpu_hour);
  const typeRows = t ? [["CPU", t.cpu], ["Memory", t.memory], ["Storage", t.storage], ["Cloud services (AWS)", t.aws], ["LLM calls", t.llm]] as [string, number][] : [];

  async function analyze() {
    setAnalyzing(true);
    setAnalyzeError(null);
    try {
      const r = await api<{ id: string }>("/finops/analyze", { method: "POST", body: { environment: env, namespace: ns || undefined, days } });
      nav(`/investigations/${r.id}`);
    } catch (e) {
      setAnalyzeError(e);
      setAnalyzing(false);
    }
  }

  return (
    <>
      <PageHead title="FinOps" sub="Cost by resource type across all environments, including MOKS's own LLM calls">
        {can("investigation.run") && <button onClick={analyze} disabled={analyzing}>{analyzing ? "Starting…" : "Analyze with AI"}</button>}
      </PageHead>
      <ErrorBox error={ov.error ?? analyzeError} />
      <form className="card filters" onSubmit={(e) => e.preventDefault()}>
        <label>Environment
          <select value={env} onChange={(e) => { setEnv(e.target.value); setNs(""); }}>
            <option value="all">All environments</option>
            {envs.data?.map((e) => <option key={e.name} value={e.name}>{e.display_name}</option>)}
          </select>
        </label>
        {one && <label>Namespace
          <select value={ns} onChange={(e) => setNs(e.target.value)}>
            <option value="">All namespaces</option>
            {nss.data?.namespaces.map((n) => <option key={n}>{n}</option>)}
          </select>
        </label>}
        <label>Window<select value={days} onChange={(e) => setDays(Number(e.target.value))}>{[1, 7, 14, 30].map((n) => <option key={n} value={n}>{n} day{n > 1 ? "s" : ""}</option>)}</select></label>
        <label>Workload<input value={text} onChange={(e) => setText(e.target.value)} placeholder="name or namespace" /></label>
        <label>Kind<select value={kind} onChange={(e) => setKind(e.target.value)}><option value="">All kinds</option>{kinds.map((k) => <option key={k}>{k}</option>)}</select></label>
        {ov.loading && <span className="small muted">Loading…</span>}
      </form>
      {t && <>
        <div className="stat-row" style={{ marginTop: 14 }}>
          <Stat value={`$${money(t.all)}`} label={`total, ${days} d`} />
          <Stat value={`$${money(t.cpu)}`} label="CPU" />
          <Stat value={`$${money(t.memory)}`} label="memory" />
          <Stat value={`$${money(t.storage)}`} label="storage" />
          <Stat value={`$${money(t.aws)}`} label="cloud services" />
          <Stat value={`$${money(t.llm)}`} label={`LLM (${d.llm.calls} calls)`} />
          <Stat value={`$${money(t.idle)}`} label="idle (requested, unused)" />
          <Stat value={`$${money(saving)}`} label="possible saving / month" />
        </div>
        {unpriced && <div className="alert warn small" style={{ marginBottom: 14 }}>Kubernetes costs are $0 until a cost_model connector has a price: set monthly_cost_usd, or cpu_hour_usd and gib_hour_usd, under Connectors.</div>}
        <div className="grid cols-2">
          <div className="card">
            <h2>Cost by resource type</h2>
            <table>
              <thead><tr><th>Resource</th><th>Cost</th><th>Share</th></tr></thead>
              <tbody>{typeRows.map(([k, v]) => <tr key={k}><td>{k}</td><td>${money(v)}</td><td><Share value={v} total={t.all} /></td></tr>)}</tbody>
            </table>
          </div>
          <div className="card">
            <h2>Cost by environment</h2>
            <table>
              <thead><tr><th>Environment</th><th>CPU</th><th>Memory</th><th>Storage</th><th>AWS</th><th>LLM</th></tr></thead>
              <tbody>{rows.map((r) => {
                const k = r.kubernetes?.by_resource;
                return (
                  <tr key={r.environment}>
                    <td>{r.environment}{Object.entries(r.errors).map(([src, msg]) => <div key={src} className="small muted">{src}: {String(msg)}</div>)}</td>
                    <td>{k ? money(k.cpu) : "—"}</td><td>{k ? money(k.memory) : "—"}</td><td>{k ? money(k.storage) : "—"}</td>
                    <td>{r.aws ? money(r.aws.total) : "—"}</td><td>{money(d.llm.by_environment[r.environment] ?? 0)}</td>
                  </tr>
                );
              })}</tbody>
            </table>
          </div>
        </div>
        <div className="card" style={{ marginTop: 14 }}>
          <h2>LLM calls ({days} days)</h2>
          <div className="small muted" style={{ marginBottom: 6 }}>{d.llm.calls} calls, {d.llm.tokens.toLocaleString()} tokens. {d.llm.note}</div>
          <table>
            <thead><tr><th>Provider / model</th><th>Calls</th><th>Tokens in</th><th>Tokens out</th><th>Cost</th></tr></thead>
            <tbody>{d.llm.by_model.map((m: any) => <tr key={m.model}><td className="mono small">{m.model}</td><td>{m.calls}</td><td>{m.tokens_in.toLocaleString()}</td><td>{m.tokens_out.toLocaleString()}</td><td>${money(m.cost_usd)}</td></tr>)}</tbody>
          </table>
          {!d.llm.by_model.length && <div className="empty">No LLM calls in this window.</div>}
          {d.llm.series.length > 0 && <LineChart series={d.llm.series} />}
        </div>
        <div className="card" style={{ marginTop: 14 }}>
          <h2>Workloads ({shownWorkloads.length})</h2>
          <div className="table-wrap">
            <table>
              <thead><tr><th>Environment</th><th>Namespace</th><th>Workload</th><th>Pods</th><th>CPU req / used</th><th>Mem req / used</th><th>CPU $</th><th>Mem $</th><th>Storage $</th><th>Total</th><th>Idle</th></tr></thead>
              <tbody>{shownWorkloads.slice(0, 200).map((w) => (
                <tr key={w.environment + w.namespace + w.workload}>
                  <td className="small">{w.environment}</td><td className="mono small">{w.namespace}</td><td className="mono small">{w.workload}</td><td>{w.pods}</td>
                  <td className="small">{w.cpu_request} / {w.cpu_avg}</td><td className="small">{w.mem_request_mib} / {w.mem_avg_mib} MiB</td>
                  <td>{money(w.cost_by_resource.cpu)}</td><td>{money(w.cost_by_resource.memory)}</td><td>{money(w.cost_by_resource.storage)}</td>
                  <td>{money(w.cost)}</td><td>{money(w.idle_cost)}</td>
                </tr>
              ))}</tbody>
            </table>
          </div>
          {!shownWorkloads.length && <div className="empty">No workloads match.</div>}
        </div>
        <div className="card" style={{ marginTop: 14 }}>
          <h2>Rightsizing</h2>
          <div className="small muted">From p95 CPU and max memory over {days} days (+25% headroom).</div>
          <div className="table-wrap">
            <table>
              <thead><tr><th>Scope</th><th>Container</th><th>Action</th><th>Current → recommended</th><th>Saving / mo</th><th /></tr></thead>
              <tbody>{recs.filter(match).map((r, i) => (
                <tr key={i}>
                  <td className="mono small">{r.environment}/{r.namespace}</td>
                  <td className="mono small">{r.workload}/{r.container}<div className="muted">{r.findings.join("; ")}</div></td>
                  <td><Badge kind={r.action === "increase" ? "bad" : r.action === "reduce" ? "ok" : "warn"}>{r.action}</Badge></td>
                  <td className="small mono">cpu {r.current.cpu_request ?? "—"}→{r.recommended.cpu_request}<br />mem {r.current.mem_request ?? "—"}→{r.recommended.mem_request}</td>
                  <td>{r.monthly_saving < 0 ? <span className="small">+{money(-r.monthly_saving)} cost</span> : money(r.monthly_saving)}</td>
                  <td>{can("action.request") && r.workload.startsWith("Deployment/") && <button className="small ghost" onClick={() => setPropose(r)}>Propose</button>}</td>
                </tr>
              ))}</tbody>
            </table>
          </div>
          {!recs.length && <div className="empty">Requests match usage.</div>}
        </div>
        {aws.length > 0 && <div className="card" style={{ marginTop: 14 }}>
          <h2>Cloud services</h2>
          <table>
            <thead><tr><th>Environment</th><th>Service</th><th>Cost</th></tr></thead>
            <tbody>{aws.map((g, i) => <tr key={i}><td className="small">{g.environment}</td><td>{g.name}</td><td>${money(g.amount)}</td></tr>)}</tbody>
          </table>
        </div>}
      </>}
      {propose && <div className="drawer"><div className="row between"><h2>Propose resize</h2><button className="ghost small" onClick={() => setPropose(null)}>Close</button></div>
        <PlanForm env={propose.environment} ns={propose.namespace} initial={{ action: "patch_resources", target_kind: "Deployment", target_name: propose.workload.split("/")[1],
          params: { container: propose.container, requests: { cpu: propose.recommended.cpu_request, memory: propose.recommended.mem_request },
            limits: propose.recommended.mem_limit ? { memory: propose.recommended.mem_limit } : {} },
          reason: `Rightsizing from ${days}-day usage: p95 CPU ${propose.observed.cpu_p95}, max memory ${propose.observed.mem_max}. ${propose.findings.join("; ")}` }}
          onDone={(id) => nav(`/actions/${id}`)} /></div>}
      {hasAws && <div className="card stack" style={{ marginTop: 14 }}>
        <h2>Anomalies, budgets and forecast · {env}</h2>
        {forecast.data && !forecast.data.error && <div className="small">Forecast next 30 days: ${money(forecast.data.data.total)}</div>}
        {anomalies.data?.error ? <div className="small muted">{anomalies.data.error}</div> : anomalies.data?.data?.map((a: any) => (
          <div key={a.id} className="small"><Badge kind="warn">impact {money(a.impact)}</Badge> {a.start} – {a.end ?? "ongoing"} · {a.root_causes.map((c: any) => [c.Service, c.Region, c.UsageType].filter(Boolean).join(" ")).join("; ")}</div>))}
        {anomalies.data?.data?.length === 0 && <div className="small muted">No cost anomalies in 30 days.</div>}
        {budgets.data && !budgets.data.error && budgets.data.data.map((b: any) => (
          <div key={b.name} className="small"><strong>{b.name}</strong> {money(b.actual)} of {money(b.limit)} {b.unit} ({b.actual_pct}%)
            {b.forecast_pct != null && <Badge kind={b.forecast_pct >= 100 ? "bad" : b.forecast_pct >= 80 ? "warn" : "ok"}>forecast {b.forecast_pct}%</Badge>}</div>))}
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
        <select aria-label="Evaluation mode" value={mode} onChange={(e) => setMode(e.target.value)}><option value="rules">rules only</option><option value="llm">with the active LLM</option></select>
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

const CATEGORIES = ["kubernetes", "application", "aws", "devops", "support", "finops"];

export function Skills() {
  const { can } = useAuth();
  const list = useLoad(() => api<any>("/skills"), []);
  const [open, setOpen] = useState<string | null>(null);
  const [category, setCategory] = useState("");
  const [text, setText] = useState("");
  const detail = useLoad(() => (open ? api<any>(`/skills/${open}`) : Promise.resolve(null)), [open]);
  const rows = (list.data?.skills ?? []).filter((s: any) => (!category || s.category === category) && JSON.stringify([s.name, s.title, s.description]).toLowerCase().includes(text.toLowerCase()));
  const d = detail.data;
  const blocked = (s: any) => Object.entries(s.availability as Record<string, any>).filter(([, v]) => !v.available);
  async function toggle(s: any) {
    await api(`/skills/${s.name}/policy`, { method: "PUT", body: { enabled: !s.enabled } });
    list.reload();
    detail.reload();
  }
  return (
    <>
      <PageHead title="SRE skills" sub="Versioned playbooks from SKILL.md files. MOKS picks them from the question and findings, runs their read-only steps and follows their guidance.">
        {can("admin.policies") && <button className="ghost" onClick={() => api("/skills/reload", { method: "POST" }).then(list.reload)}>Reload</button>}
      </PageHead>
      {list.data?.errors?.length > 0 && <div className="alert error" style={{ marginBottom: 12 }}>{list.data.errors.map((e: any) => <div key={e.path} className="small">{e.path}: {e.error}</div>)}</div>}
      <div className="card filters" style={{ marginBottom: 14 }}>
        <label>Category<select value={category} onChange={(e) => setCategory(e.target.value)}><option value="">All</option>{CATEGORIES.map((c) => <option key={c}>{c}</option>)}</select></label>
        <label>Search<input value={text} onChange={(e) => setText(e.target.value)} /></label>
        <span className="small muted">{rows.length} skills</span>
      </div>
      <div className="grid" style={{ gridTemplateColumns: "minmax(0, 2fr) minmax(0, 3fr)", alignItems: "start" }}>
        <div className="card"><table><tbody>{rows.map((s: any) => (
          <tr key={s.name} className="clickable" {...rowAction(() => setOpen(s.name))}>
            <td><strong>{s.title}</strong> <Badge>{s.category}</Badge> <span className="small muted">v{s.version}</span> {s.source === "custom" && <Badge kind="info">custom</Badge>} {!s.enabled && <Badge kind="warn">off</Badge>}
              {s.detection_rules.length > 0 && <Badge kind="info">{s.detection_rules.length} detection</Badge>}
              <div className="small muted">{s.description}</div>
              {blocked(s).length > 0 && <div className="small muted">Unavailable in {blocked(s).map(([e]) => e).join(", ")}: {(blocked(s)[0][1] as any).reasons.join("; ")}</div>}</td>
          </tr>
        ))}</tbody></table></div>
        <div className="card stack">
          {!d ? <div className="empty">Select a skill.</div> : <>
            <div className="row between"><h2>{d.title} <span className="small muted">v{d.version} · {d.owner}</span></h2>
              {can("admin.policies") && <button className="ghost small" onClick={() => toggle(d)}>{d.enabled ? "Turn off" : "Turn on"}</button>}</div>
            <p className="muted" style={{ margin: 0 }}>{d.description}</p>
            <div className="small">Needs: {d.needs.join(", ") || "nothing beyond MOKS"}{d.telemetry.length > 0 && <> · metrics {d.telemetry.join(", ")}</>}</div>
            <div className="row small">{Object.entries(d.availability as Record<string, any>).map(([e, v]) => <Badge key={e} kind={v.available ? "ok" : "neutral"}>{e}: {v.available ? "available" : v.reasons[0]}</Badge>)}</div>
            <h3>Triggers</h3><div className="small">keywords: {d.triggers.keywords.join(", ") || "—"} · findings: {d.triggers.findings.join(", ") || "—"}</div>
            {d.steps.length > 0 && <><h3>Steps (read-only, policy-checked)</h3>
              <ol className="small">{d.steps.map((s: any, i: number) => <li key={i}><span className="mono">{s.tool}</span> {s.for_each && <Badge>{s.for_each}</Badge>} {s.purpose}</li>)}</ol></>}
            {d.detection.length > 0 && <><h3>Detection rules</h3><ul className="small">{d.detection.map((r: any) => <li key={r.id}><span className="mono">{r.id}</span> {r.kind} · {r.severity} · every {r.interval_minutes} min, {r.sustained_checks} checks to open</li>)}</ul></>}
            {d.hypotheses.length > 0 && <><h3>Hypotheses</h3><ul className="small">{d.hypotheses.map((h: any, i: number) => <li key={i}>{h.statement} <span className="muted">Supported by: {h.supports || "—"} Refuted by: {h.refutes || "—"}</span></li>)}</ul></>}
            {d.recovery_validation.length > 0 && <><h3>Recovery validation</h3><ul className="small">{d.recovery_validation.map((x: string, i: number) => <li key={i}>{x}</li>)}</ul></>}
            {d.remediation.length > 0 && <><h3>Remediation hints</h3><ul className="small">{d.remediation.map((r: any, i: number) => <li key={i}>when <Badge kind="warn">{r.when}</Badge> → <span className="mono">{r.action}</span> {r.target} <span className="muted">{r.note}</span></li>)}</ul></>}
            <h3>Guidance</h3><pre className="logbox">{d.body}</pre>
          </>}
        </div>
      </div>
    </>
  );
}
