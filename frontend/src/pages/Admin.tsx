import { Fragment, useState, type FormEvent } from "react";
import { api, qs } from "../api";
import { useAuth } from "../auth";
import { Badge, ErrorBox, PageHead, Time, useLoad } from "../components";

function useAction() {
  const [error, setError] = useState<unknown>();
  const [busy, setBusy] = useState(false);
  async function run<T>(fn: () => Promise<T>): Promise<T | undefined> {
    setBusy(true);
    setError(null);
    try {
      return await fn();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }
  return { error, busy, run };
}

const lines = (s: string) => s.split(/[\s,]+/).map((x) => x.trim()).filter(Boolean);

// ---- Environments -----------------------------------------------------------------------------

export function Environments() {
  const list = useLoad(() => api<any[]>("/environments"), []);
  const blank = { name: "", display_name: "", kind: "minikube", is_production: false, allowed_namespaces: "*", denied_namespaces: "kube-system", max_query_window_minutes: 360, grafana_url: "" };
  const [form, setForm] = useState<any>(blank);
  const [editing, setEditing] = useState<string | null>(null);
  const a = useAction();
  async function save(e: FormEvent) {
    e.preventDefault();
    const body = { ...form, allowed_namespaces: lines(form.allowed_namespaces), denied_namespaces: lines(form.denied_namespaces), grafana_url: form.grafana_url || null };
    if (await a.run(() => api(editing ? `/environments/${editing}` : "/environments", { method: editing ? "PUT" : "POST", body }))) {
      setForm(blank);
      setEditing(null);
      list.reload();
    }
  }
  return (
    <>
      <PageHead title="Environments" sub="Clusters MOKS can read, and the infrastructure scope it may touch in each (independent of user roles)" />
      <div className="card">
        <table>
          <thead><tr><th>Name</th><th>Kind</th><th>Allowed namespaces</th><th>Denied</th><th>Max window</th><th /></tr></thead>
          <tbody>{list.data?.map((e) => (
            <tr key={e.name}>
              <td>{e.display_name} <span className="mono small muted">{e.name}</span> {e.is_production && <Badge kind="warn">production</Badge>}</td>
              <td>{e.kind}</td><td className="mono small">{e.allowed_namespaces.join(", ")}</td><td className="mono small">{e.denied_namespaces.join(", ")}</td>
              <td>{e.max_query_window_minutes} min</td>
              <td><button className="ghost small" onClick={() => { setEditing(e.name); setForm({ ...e, allowed_namespaces: e.allowed_namespaces.join(", "), denied_namespaces: e.denied_namespaces.join(", "), grafana_url: e.grafana_url ?? "" }); }}>Edit</button></td>
            </tr>
          ))}</tbody>
        </table>
      </div>
      <form className="card stack" style={{ marginTop: 14 }} onSubmit={save}>
        <h2>{editing ? `Edit ${editing}` : "Add environment"}</h2>
        <div className="row">
          <label>Name<input required disabled={!!editing} pattern="[a-z0-9][a-z0-9-]*" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} /></label>
          <label>Display name<input value={form.display_name} onChange={(e) => setForm({ ...form, display_name: e.target.value })} /></label>
          <label>Kind<select value={form.kind} onChange={(e) => setForm({ ...form, kind: e.target.value })}><option>minikube</option><option>eks</option></select></label>
          <label className="inline"><input type="checkbox" checked={form.is_production} onChange={(e) => setForm({ ...form, is_production: e.target.checked })} /> production</label>
        </div>
        <div className="row">
          <label>Allowed namespaces (patterns)<input value={form.allowed_namespaces} onChange={(e) => setForm({ ...form, allowed_namespaces: e.target.value })} /></label>
          <label>Denied namespaces<input value={form.denied_namespaces} onChange={(e) => setForm({ ...form, denied_namespaces: e.target.value })} /></label>
          <label>Max query window (min)<input type="number" min={5} max={10080} value={form.max_query_window_minutes} onChange={(e) => setForm({ ...form, max_query_window_minutes: Number(e.target.value) })} /></label>
          <label>Grafana URL<input value={form.grafana_url} onChange={(e) => setForm({ ...form, grafana_url: e.target.value })} placeholder="https://…/grafana" /></label>
        </div>
        <ErrorBox error={a.error} />
        <div className="row"><button disabled={a.busy}>{editing ? "Save" : "Create"}</button>{editing && <button type="button" className="ghost" onClick={() => { setEditing(null); setForm(blank); }}>Cancel</button>}</div>
      </form>
    </>
  );
}

// ---- Connectors -------------------------------------------------------------------------------

const TEMPLATES: Record<string, any> = {
  kubernetes: { mode: "in_cluster", timeout_seconds: 15 },
  prometheus: { base_url: "http://prometheus.monitoring.svc:9090/prometheus", public_url: "https://jithendar.com/prometheus", timeout_seconds: 15, auth: "none" },
  loki: { base_url: "http://loki.monitoring.svc:3100", timeout_seconds: 20, namespace_label: "namespace", auth: "none" },
  grafana: { base_url: "http://grafana.monitoring.svc/grafana" },
  cloudwatch: { region: "us-east-1", allowed_log_groups: [] },
  git: { provider: "github", repositories: [] },
};

export function Connectors() {
  const list = useLoad(() => api<any[]>("/admin/connectors"), []);
  const envs = useLoad(() => api<any[]>("/environments"), []);
  const [form, setForm] = useState<any>({ environment: "", type: "prometheus", name: "", enabled: true, config: JSON.stringify(TEMPLATES.prometheus, null, 2), credType: "none", credName: "", credPath: "" });
  const [editing, setEditing] = useState<string | null>(null);
  const [tests, setTests] = useState<Record<string, any>>({});
  const a = useAction();

  async function save(e: FormEvent) {
    e.preventDefault();
    let config: any;
    try { config = JSON.parse(form.config); } catch { a.run(() => Promise.reject(new Error("Config must be valid JSON"))); return; }
    const credential_ref = form.credType === "env" ? { type: "env", name: form.credName } : form.credType === "file" ? { type: "file", path: form.credPath } : { type: "none" };
    const body = { environment: form.environment || envs.data?.[0]?.name, type: form.type, name: form.name, enabled: form.enabled, config, credential_ref };
    if (await a.run(() => api(editing ? `/admin/connectors/${editing}` : "/admin/connectors", { method: editing ? "PUT" : "POST", body }))) {
      setEditing(null);
      list.reload();
    }
  }
  async function test(id: string) {
    setTests((t) => ({ ...t, [id]: { busy: true } }));
    const r = await api(`/admin/connectors/${id}/test`, { method: "POST" }).catch((e) => ({ ok: false, message: e.message }));
    setTests((t) => ({ ...t, [id]: r }));
    list.reload();
  }
  async function remove(id: string) {
    if (confirm("Delete this connector?")) { await a.run(() => api(`/admin/connectors/${id}`, { method: "DELETE" })); list.reload(); }
  }
  function edit(c: any) {
    setEditing(c.id);
    setForm({ environment: c.environment, type: c.type, name: c.name, enabled: c.enabled, config: JSON.stringify(c.config, null, 2),
      credType: c.credential_ref?.type ?? "none", credName: c.credential_ref?.name ?? "", credPath: c.credential_ref?.path ?? "" });
  }
  return (
    <>
      <PageHead title="Connectors" sub="Live data sources per environment. Credentials are references to mounted Secrets, never stored here." />
      <div className="card">
        <table>
          <thead><tr><th>Environment</th><th>Type</th><th>Name</th><th>Endpoint</th><th>Credential</th><th>Last test</th><th /></tr></thead>
          <tbody>{list.data?.map((c) => (
            <tr key={c.id}>
              <td className="mono small">{c.environment}</td>
              <td>{c.type} {!c.implemented && <Badge kind="neutral">planned</Badge>} {!c.enabled && <Badge>disabled</Badge>}</td>
              <td>{c.name}</td>
              <td className="mono small">{c.config.base_url ?? c.config.mode ?? ""}</td>
              <td className="mono small">{c.credential}</td>
              <td className="small">
                {tests[c.id]?.busy ? "testing…" : c.last_test_ok == null ? <span className="muted">never</span> : <Badge kind={c.last_test_ok ? "ok" : "bad"}>{c.last_test_ok ? "ok" : "failed"}</Badge>}
                {c.last_test_at && <> <Time iso={c.last_test_at} /></>}
                {c.last_test_message && <div className="muted">{c.last_test_message}</div>}
              </td>
              <td className="row"><button className="small" onClick={() => test(c.id)}>Test</button><button className="ghost small" onClick={() => edit(c)}>Edit</button><button className="ghost small" onClick={() => remove(c.id)}>Delete</button></td>
            </tr>
          ))}</tbody>
        </table>
      </div>
      <form className="card stack" style={{ marginTop: 14 }} onSubmit={save}>
        <h2>{editing ? "Edit connector" : "Add connector"}</h2>
        <div className="row">
          <label>Environment<select value={form.environment} onChange={(e) => setForm({ ...form, environment: e.target.value })}>{envs.data?.map((e) => <option key={e.name}>{e.name}</option>)}</select></label>
          <label>Type<select value={form.type} onChange={(e) => setForm({ ...form, type: e.target.value, config: JSON.stringify(TEMPLATES[e.target.value], null, 2) })}>
            {Object.keys(TEMPLATES).map((t) => <option key={t}>{t}</option>)}</select></label>
          <label>Name<input required pattern="[a-z0-9][a-z0-9-]*" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} /></label>
          <label className="inline"><input type="checkbox" checked={form.enabled} onChange={(e) => setForm({ ...form, enabled: e.target.checked })} /> enabled</label>
        </div>
        <label>Configuration (JSON; no secrets)<textarea rows={6} className="mono" value={form.config} onChange={(e) => setForm({ ...form, config: e.target.value })} /></label>
        <div className="row">
          <label>Credential<select value={form.credType} onChange={(e) => setForm({ ...form, credType: e.target.value })}><option value="none">none</option><option value="env">environment variable</option><option value="file">mounted file</option></select></label>
          {form.credType === "env" && <label>Variable (MOKS_CRED_*)<input value={form.credName} onChange={(e) => setForm({ ...form, credName: e.target.value })} placeholder="MOKS_CRED_LOKI_TOKEN" /></label>}
          {form.credType === "file" && <label>Path under credentials dir<input value={form.credPath} onChange={(e) => setForm({ ...form, credPath: e.target.value })} placeholder="eks/kubeconfig" /></label>}
        </div>
        <ErrorBox error={a.error} />
        <div className="row"><button disabled={a.busy}>{editing ? "Save" : "Add"}</button>{editing && <button type="button" className="ghost" onClick={() => setEditing(null)}>Cancel</button>}</div>
      </form>
    </>
  );
}

// ---- LLM providers ----------------------------------------------------------------------------

export function LLMProviders() {
  const list = useLoad(() => api<any[]>("/admin/llm"), []);
  const blank = { name: "", purpose: "chat", provider: "bedrock", model: "", endpoint: "", region: "us-east-1", credType: "none", credName: "",
    timeout_seconds: 60, max_output_tokens: 2000, daily_budget_usd: "5", input_price_per_mtok: "0", output_price_per_mtok: "0" };
  const [form, setForm] = useState<any>(blank);
  const [editing, setEditing] = useState<string | null>(null);
  const [tests, setTests] = useState<Record<string, any>>({});
  const a = useAction();
  async function save(e: FormEvent) {
    e.preventDefault();
    const body = { ...form, endpoint: form.endpoint || null, region: form.provider === "bedrock" ? form.region : null,
      credential_ref: form.credType === "env" ? { type: "env", name: form.credName } : { type: "none" } };
    delete body.credType; delete body.credName;
    if (await a.run(() => api(editing ? `/admin/llm/${editing}` : "/admin/llm", { method: editing ? "PUT" : "POST", body }))) {
      setForm(blank); setEditing(null); list.reload();
    }
  }
  async function test(id: string) {
    setTests((t) => ({ ...t, [id]: { message: "testing…" } }));
    setTests((t) => ({ ...t }));
    const r = await api(`/admin/llm/${id}/test`, { method: "POST" }).catch((e) => ({ ok: false, message: e.message }));
    setTests((t) => ({ ...t, [id]: r }));
  }
  return (
    <>
      <PageHead title="LLM providers" sub="Provider-neutral chat model used for planning and reports. Without an active chat model, investigations are rule-based." />
      <div className="card">
        <table>
          <thead><tr><th>Name</th><th>Purpose</th><th>Provider / model</th><th>Credential</th><th>Budget (24 h)</th><th>Status</th><th /></tr></thead>
          <tbody>{list.data?.map((c) => (
            <tr key={c.id}>
              <td>{c.name}</td><td>{c.purpose}</td>
              <td className="mono small">{c.provider} / {c.model}{c.endpoint && <div className="muted">{c.endpoint}</div>}{c.region && <div className="muted">{c.region}</div>}</td>
              <td className="mono small">{c.credential}</td>
              <td className="small">${Number(c.spent_last_24h_usd ?? 0).toFixed(4)} of ${c.daily_budget_usd}</td>
              <td>{c.is_active ? <Badge kind="ok">active</Badge> : <Badge>inactive</Badge>}{tests[c.id] && <div className={`small ${tests[c.id].ok ? "" : "muted"}`}>{tests[c.id].message}</div>}</td>
              <td className="row">
                <button className="small" onClick={() => test(c.id)}>Test</button>
                <button className="ghost small" onClick={async () => { await a.run(() => api(`/admin/llm/${c.id}/activate${qs({ active: !c.is_active })}`, { method: "POST" })); list.reload(); }}>{c.is_active ? "Deactivate" : "Activate"}</button>
                <button className="ghost small" onClick={() => { setEditing(c.id); setForm({ ...c, endpoint: c.endpoint ?? "", region: c.region ?? "us-east-1", credType: c.credential_ref?.type ?? "none", credName: c.credential_ref?.name ?? "" }); }}>Edit</button>
              </td>
            </tr>
          ))}</tbody>
        </table>
        {list.data && !list.data.length && <div className="empty">No LLM configured. Investigations use rule-based diagnosis only.</div>}
      </div>
      <form className="card stack" style={{ marginTop: 14 }} onSubmit={save}>
        <h2>{editing ? "Edit provider" : "Add provider"}</h2>
        <div className="row">
          <label>Name<input required pattern="[a-z0-9][a-z0-9-]*" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} /></label>
          <label>Purpose<select value={form.purpose} onChange={(e) => setForm({ ...form, purpose: e.target.value })}><option>chat</option><option>embedding</option></select></label>
          <label>Provider<select value={form.provider} onChange={(e) => setForm({ ...form, provider: e.target.value })}>{["bedrock", "openai", "gemini", "ollama"].map((p) => <option key={p}>{p}</option>)}</select></label>
          <label>Model<input required value={form.model} onChange={(e) => setForm({ ...form, model: e.target.value })} placeholder={form.provider === "bedrock" ? "inference profile or model ID" : "model name"} /></label>
        </div>
        <div className="row">
          {form.provider === "bedrock" ? <label>Region<input value={form.region} onChange={(e) => setForm({ ...form, region: e.target.value })} /></label> :
            <label>Endpoint{form.provider === "ollama" ? " (required)" : " (optional)"}<input value={form.endpoint} onChange={(e) => setForm({ ...form, endpoint: e.target.value })} placeholder={form.provider === "ollama" ? "http://ollama.ollama:11434" : "default"} /></label>}
          {form.provider !== "bedrock" && (
            <label>API key reference<select value={form.credType} onChange={(e) => setForm({ ...form, credType: e.target.value })}><option value="none">none</option><option value="env">environment variable</option></select></label>
          )}
          {form.credType === "env" && form.provider !== "bedrock" && <label>Variable<input value={form.credName} onChange={(e) => setForm({ ...form, credName: e.target.value })} placeholder="MOKS_CRED_OPENAI_API_KEY" /></label>}
          {form.provider === "bedrock" && <span className="small muted">Bedrock uses the pod's AWS identity (IRSA / Pod Identity) or a mounted profile.</span>}
        </div>
        <div className="row">
          <label>Timeout (s)<input type="number" value={form.timeout_seconds} onChange={(e) => setForm({ ...form, timeout_seconds: Number(e.target.value) })} /></label>
          <label>Max output tokens<input type="number" value={form.max_output_tokens} onChange={(e) => setForm({ ...form, max_output_tokens: Number(e.target.value) })} /></label>
          <label>Daily budget (USD)<input value={form.daily_budget_usd} onChange={(e) => setForm({ ...form, daily_budget_usd: e.target.value })} /></label>
          <label>Input $ / 1M tokens<input value={form.input_price_per_mtok} onChange={(e) => setForm({ ...form, input_price_per_mtok: e.target.value })} /></label>
          <label>Output $ / 1M tokens<input value={form.output_price_per_mtok} onChange={(e) => setForm({ ...form, output_price_per_mtok: e.target.value })} /></label>
        </div>
        <ErrorBox error={a.error} />
        <div className="row"><button disabled={a.busy}>{editing ? "Save" : "Add"}</button>{editing && <button type="button" className="ghost" onClick={() => { setEditing(null); setForm(blank); }}>Cancel</button>}</div>
      </form>
    </>
  );
}

// ---- Users and roles --------------------------------------------------------------------------

export function Users() {
  const { me } = useAuth();
  const list = useLoad(() => api<any[]>("/admin/users"), []);
  const roles = useLoad(() => api<any[]>("/auth/roles"), []);
  const [nu, setNu] = useState({ email: "", display_name: "", password: "" });
  const [grant, setGrant] = useState<Record<string, { role: string; environment: string; namespace: string }>>({});
  const a = useAction();
  return (
    <>
      <PageHead title="Users & roles" sub="Application RBAC. Bindings scope a role to an environment and namespace pattern." />
      <div className="card" style={{ marginBottom: 14 }}>
        <h2>Roles</h2>
        <dl className="kv">{roles.data?.map((r) => <Fragment key={r.role}><dt><Badge kind="info">{r.role}</Badge></dt><dd>{r.description}</dd></Fragment>)}</dl>
      </div>
      <ErrorBox error={a.error} />
      <div className="card">
        <table>
          <thead><tr><th>User</th><th>Sign-in</th><th>Bindings</th><th>Grant</th><th /></tr></thead>
          <tbody>{list.data?.map((u) => {
            const g = grant[u.id] ?? { role: "viewer", environment: "*", namespace: "*" };
            return (
              <tr key={u.id}>
                <td>{u.display_name || u.email}<div className="small muted">{u.email}</div>{!u.is_active && <Badge kind="bad">disabled</Badge>}</td>
                <td className="small">{u.auth_provider}<div className="muted">last <Time iso={u.last_login_at} /></div></td>
                <td className="small">{u.bindings.map((b: any) => (
                  <div key={b.id}><Badge kind="info">{b.role}</Badge> <span className="mono">{b.environment}/{b.namespace}</span> <span className="muted">({b.source})</span>
                    <button className="ghost small" style={{ marginLeft: 4 }} onClick={async () => { await a.run(() => api(`/admin/users/${u.id}/bindings/${b.id}`, { method: "DELETE" })); list.reload(); }}>×</button></div>
                ))}{!u.bindings.length && <span className="muted">no access</span>}</td>
                <td>
                  <div className="row">
                    <select value={g.role} onChange={(e) => setGrant({ ...grant, [u.id]: { ...g, role: e.target.value } })}>{roles.data?.map((r) => <option key={r.role}>{r.role}</option>)}</select>
                    <input style={{ width: 90 }} value={g.environment} onChange={(e) => setGrant({ ...grant, [u.id]: { ...g, environment: e.target.value } })} title="environment pattern" />
                    <input style={{ width: 110 }} value={g.namespace} onChange={(e) => setGrant({ ...grant, [u.id]: { ...g, namespace: e.target.value } })} title="namespace pattern" />
                    <button className="small" onClick={async () => { await a.run(() => api(`/admin/users/${u.id}/bindings`, { method: "POST", body: g })); list.reload(); }}>Grant</button>
                  </div>
                </td>
                <td>{u.id !== me?.id && <button className="ghost small" onClick={async () => { await a.run(() => api(`/admin/users/${u.id}/active${qs({ active: !u.is_active })}`, { method: "POST" })); list.reload(); }}>{u.is_active ? "Disable" : "Enable"}</button>}</td>
              </tr>
            );
          })}</tbody>
        </table>
      </div>
      {me?.auth_mode !== "oidc" ? (
        <form className="card stack" style={{ marginTop: 14 }} onSubmit={async (e) => { e.preventDefault(); if (await a.run(() => api("/admin/users", { method: "POST", body: { ...nu, password: nu.password || null } }))) { setNu({ email: "", display_name: "", password: "" }); list.reload(); } }}>
          <h2>Add user</h2>
          <div className="row">
            <label>Email<input required type="email" value={nu.email} onChange={(e) => setNu({ ...nu, email: e.target.value })} /></label>
            <label>Display name<input value={nu.display_name} onChange={(e) => setNu({ ...nu, display_name: e.target.value })} /></label>
            {me?.auth_mode === "local" && <label>Initial password (≥ 12)<input type="password" minLength={12} required value={nu.password} onChange={(e) => setNu({ ...nu, password: e.target.value })} /></label>}
            <button disabled={a.busy}>Add</button>
          </div>
        </form>
      ) : <div className="small muted" style={{ marginTop: 10 }}>With single sign-on, users appear after their first sign-in; grant roles here or map IdP groups (MOKS_OIDC_GROUP_ROLES).</div>}
    </>
  );
}

// ---- Audit ------------------------------------------------------------------------------------

export function Audit() {
  const [f, setF] = useState({ action: "", actor: "", decision: "", environment: "" });
  const [applied, setApplied] = useState(f);
  const list = useLoad(() => api<{ events: any[] }>(`/admin/audit${qs({ ...applied, limit: 200 })}`), [applied]);
  const [verify, setVerify] = useState<any>();
  const [open, setOpen] = useState<number | null>(null);
  return (
    <>
      <PageHead title="Audit log" sub="Append-only, hash-chained record of sign-ins, tool calls, authorization decisions and configuration changes">
        <button className="ghost" onClick={async () => setVerify(await api("/admin/audit/verify"))}>Verify chain</button>
      </PageHead>
      {verify && <div className={`alert ${verify.ok ? "info" : "error"}`} style={{ marginBottom: 12 }}>{verify.ok ? `Chain intact: ${verify.checked} records verified.` : `Chain broken at record #${verify.broken_at} (after ${verify.checked} checked).`}</div>}
      <form className="card row" onSubmit={(e) => { e.preventDefault(); setApplied(f); }}>
        <label>Action<input value={f.action} onChange={(e) => setF({ ...f, action: e.target.value })} placeholder="tool.call, auth.*" /></label>
        <label>Actor<input value={f.actor} onChange={(e) => setF({ ...f, actor: e.target.value })} /></label>
        <label>Decision<select value={f.decision} onChange={(e) => setF({ ...f, decision: e.target.value })}><option value="">any</option><option>allow</option><option>deny</option><option>n/a</option></select></label>
        <label>Environment<input value={f.environment} onChange={(e) => setF({ ...f, environment: e.target.value })} /></label>
        <button>Filter</button>
      </form>
      <ErrorBox error={list.error} />
      <div className="card" style={{ marginTop: 14 }}>
        <table>
          <thead><tr><th>#</th><th>Time</th><th>Actor</th><th>Action</th><th>Resource</th><th>Scope</th><th>Decision</th><th>Outcome</th></tr></thead>
          <tbody>{list.data?.events.map((e) => (
            <Fragment key={e.id}>
              <tr className="clickable" onClick={() => setOpen(open === e.id ? null : e.id)}>
                <td className="mono small">{e.chain_seq}</td><td className="small">{new Date(e.ts).toLocaleString()}</td>
                <td className="small">{e.actor_email ?? "system"}</td><td className="mono small">{e.action}</td>
                <td className="mono small">{[e.resource_type, e.resource_id].filter(Boolean).join(":")}</td>
                <td className="mono small">{[e.environment, e.namespace].filter(Boolean).join("/")}</td>
                <td>{e.decision === "deny" ? <Badge kind="bad">deny</Badge> : e.decision === "allow" ? <Badge kind="ok">allow</Badge> : <span className="muted small">—</span>}</td>
                <td className="small">{e.outcome}</td>
              </tr>
              {open === e.id && <tr><td colSpan={8}><pre className="logbox">{JSON.stringify({ details: e.details, ip: e.ip, hash: e.hash }, null, 2)}</pre></td></tr>}
            </Fragment>
          ))}</tbody>
        </table>
      </div>
    </>
  );
}
