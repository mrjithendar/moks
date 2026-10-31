import { Fragment, useState, type FormEvent } from "react";
import { api, qs } from "../api";
import { useAuth } from "../auth";
import { Badge, DataTable, ErrorBox, Loading, PageHead, Time, useLoad } from "../components";
import { type Chip, FilterPanel } from "../filters";
import { Drawer, Empty, Truncate, useFeedback } from "../ui";

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

// ---- Data sources -------------------------------------------------------------------------------

const REGIONS = ["us-east-1", "us-east-2", "us-west-1", "us-west-2", "eu-west-1", "eu-central-1", "ap-south-1", "ap-southeast-1", "ap-northeast-1"];

interface Field { key: string; label: string; kind?: "number" | "list" | "bool" | "region"; placeholder?: string; required?: boolean }
interface Source { label: string; type: string; hint: string; defaults: any; fields: Field[] }

const REGION: Field = { key: "region", label: "Region", kind: "region", required: true };
const ROLE: Field = { key: "role_arn", label: "Role ARN to assume (optional)", placeholder: "arn:aws:iam::123456789012:role/moks-read" };
const URLS: Field[] = [{ key: "base_url", label: "URL", required: true }, { key: "public_url", label: "Browser link URL (optional)" }];

const SOURCES: Record<string, Source> = {
  eks: { label: "Amazon EKS", type: "kubernetes", hint: "Reads the cluster with a short-lived token from the pod's AWS identity. That identity needs an EKS access entry. Read-only until you add config.executor (edit as JSON).",
    defaults: { mode: "eks", region: "us-east-1", timeout_seconds: 15 }, fields: [{ key: "cluster_name", label: "Cluster name", required: true }, REGION, ROLE] },
  in_cluster: { label: "Kubernetes (this cluster)", type: "kubernetes", hint: "Uses the ServiceAccount of the MOKS pod.", defaults: { mode: "in_cluster", timeout_seconds: 15, executor: { mode: "in_cluster" } }, fields: [] },
  kubeconfig: { label: "Kubernetes (kubeconfig)", type: "kubernetes", hint: "Needs a mounted kubeconfig file as the credential.", defaults: { mode: "kubeconfig", timeout_seconds: 15 }, fields: [{ key: "context", label: "Context (optional)" }] },
  cloudwatch: { label: "CloudWatch Logs", type: "cloudwatch", hint: "Logs Insights over the listed log groups only.", defaults: { region: "us-east-1", allowed_log_groups: [], max_window_minutes: 360, max_concurrent_queries: 2, poll_timeout_seconds: 60 },
    fields: [REGION, { key: "allowed_log_groups", label: "Allowed log groups", kind: "list", required: true, placeholder: "/aws/eks/prod/*" }, ROLE, { key: "max_window_minutes", label: "Max window (min)", kind: "number" }] },
  prometheus: { label: "Prometheus", type: "prometheus", hint: "PromQL over HTTP.", defaults: { base_url: "http://prometheus.monitoring.svc:9090/prometheus", public_url: "https://jithendar.com/prometheus", timeout_seconds: 15, auth: "none" }, fields: URLS },
  loki: { label: "Loki", type: "loki", hint: "LogQL over HTTP.", defaults: { base_url: "http://loki.monitoring.svc:3100", timeout_seconds: 20, namespace_label: "namespace", auth: "none" },
    fields: [URLS[0], { key: "namespace_label", label: "Namespace label" }, { key: "tenant_id", label: "Tenant ID (optional)" }] },
  grafana: { label: "Grafana", type: "grafana", hint: "Dashboards and annotations. Use a service account token as the credential.", defaults: { base_url: "http://grafana.monitoring.svc.cluster.local/grafana", public_url: "https://jithendar.com/grafana", timeout_seconds: 15 }, fields: URLS },
  git: { label: "GitHub", type: "git", hint: "Compares deployed revisions and syncs runbooks. Use a read-only token as the credential.", defaults: { provider: "github", repositories: [] },
    fields: [{ key: "repositories", label: "Repositories", kind: "list", required: true, placeholder: "owner/repo" }] },
  aws_cost: { label: "AWS Cost Explorer", type: "aws_cost", hint: "Each uncached request costs $0.01, so results are cached and capped.", defaults: { metric: "UnblendedCost", cache_minutes: 60, max_requests_per_hour: 60, allocation_tag_keys: [] },
    fields: [{ key: "cache_minutes", label: "Cache (min)", kind: "number" }, { key: "max_requests_per_hour", label: "Max requests / hour", kind: "number" }, ROLE] },
  cost_model: { label: "Cluster cost model", type: "cost_model", hint: "Prices Kubernetes usage. Give the monthly cluster cost, or unit prices.", defaults: { monthly_cost_usd: 0, cpu_share: 0.5, currency: "USD" },
    fields: [{ key: "monthly_cost_usd", label: "Monthly cluster cost (USD)", kind: "number" }, { key: "cpu_hour_usd", label: "or CPU core-hour (USD)", kind: "number" }, { key: "gib_hour_usd", label: "and GiB-hour (USD)", kind: "number" },
      { key: "storage_gib_month_usd", label: "Storage GiB-month (USD)", kind: "number" }] },
};

const PRESETS: Record<string, string[]> = { minikube: ["in_cluster", "prometheus", "loki", "cost_model"], eks: ["eks", "cloudwatch", "aws_cost"], kubernetes: ["kubeconfig"] };

interface SourceValue { source: string; name: string; enabled: boolean; config: any; advanced: boolean; json: string; credType: string; credName: string; credPath: string }

const blankSource = (source: string): SourceValue => ({ source, name: source.replace(/_/g, "-"), enabled: true, config: { ...SOURCES[source].defaults }, advanced: false, json: "", credType: source === "kubeconfig" ? "file" : "none", credName: "", credPath: "" });

function sourceOf(c: any): string {
  if (c.type !== "kubernetes") return SOURCES[c.type] ? c.type : "prometheus";
  return c.config.mode === "eks" ? "eks" : c.config.mode === "kubeconfig" ? "kubeconfig" : "in_cluster";
}

function connectorBody(v: SourceValue, environment: string) {
  const config = v.advanced ? JSON.parse(v.json) : v.config;
  const credential_ref = v.credType === "env" ? { type: "env", name: v.credName } : v.credType === "file" ? { type: "file", path: v.credPath } : { type: "none" };
  return { environment, type: SOURCES[v.source].type, name: v.name, enabled: v.enabled, config, credential_ref };
}

/** Structured fields for one data source, with a JSON escape hatch for options not listed here. */
function SourceFields({ value, onChange }: { value: SourceValue; onChange: (v: SourceValue) => void }) {
  const src = SOURCES[value.source];
  const setCfg = (k: string, v: unknown) => onChange({ ...value, config: { ...value.config, [k]: v } });
  return (
    <div className="stack">
      <div className="small muted">{src.hint}</div>
      <div className="row">
        <label>Name<input required pattern="[a-z0-9][a-z0-9\-]*" value={value.name} onChange={(e) => onChange({ ...value, name: e.target.value })} /></label>
        {!value.advanced && src.fields.map((f) => {
          const cur = value.config[f.key];
          if (f.kind === "list") return <label key={f.key}>{f.label}<input required={f.required} placeholder={f.placeholder} value={(cur ?? []).join(", ")} onChange={(e) => setCfg(f.key, e.target.value.split(/[\s,]+/).filter(Boolean))} /></label>;
          if (f.kind === "number") return <label key={f.key}>{f.label}<input type="number" step="any" min={0} value={cur ?? ""} onChange={(e) => setCfg(f.key, e.target.value === "" ? undefined : Number(e.target.value))} /></label>;
          return <label key={f.key}>{f.label}<input required={f.required} list={f.kind === "region" ? "aws-regions" : undefined} placeholder={f.placeholder} value={cur ?? ""} onChange={(e) => setCfg(f.key, e.target.value || undefined)} /></label>;
        })}
        <label>Credential
          <select value={value.credType} onChange={(e) => onChange({ ...value, credType: e.target.value })}>
            <option value="none">{src.type === "kubernetes" || src.type === "cloudwatch" || src.type === "aws_cost" ? "Pod identity" : "None"}</option>
            <option value="env">Environment variable</option><option value="file">Mounted file</option>
          </select>
        </label>
        {value.credType === "env" && <label>Variable (MOKS_CRED_*)<input required value={value.credName} onChange={(e) => onChange({ ...value, credName: e.target.value })} placeholder="MOKS_CRED_GRAFANA_TOKEN" /></label>}
        {value.credType === "file" && <label>Path under credentials dir<input required value={value.credPath} onChange={(e) => onChange({ ...value, credPath: e.target.value })} placeholder="eks/kubeconfig" /></label>}
        <label className="inline"><input type="checkbox" checked={value.enabled} onChange={(e) => onChange({ ...value, enabled: e.target.checked })} /> enabled</label>
        <label className="inline"><input type="checkbox" checked={value.advanced} onChange={(e) => onChange({ ...value, advanced: e.target.checked, json: JSON.stringify(value.config, null, 2) })} /> edit as JSON</label>
      </div>
      {value.advanced && <label>Configuration (JSON; no secrets)<textarea rows={6} className="mono" value={value.json} onChange={(e) => onChange({ ...value, json: e.target.value })} /></label>}
      <datalist id="aws-regions">{REGIONS.map((r) => <option key={r} value={r} />)}</datalist>
    </div>
  );
}

// ---- Environments -----------------------------------------------------------------------------

export function Environments() {
  const list = useLoad(() => api<any[]>("/environments"), []);
  const blank = { name: "", display_name: "", kind: "minikube", is_production: false, allowed_namespaces: "*", denied_namespaces: "kube-system", max_query_window_minutes: 360, grafana_url: "",
    is_default: false, detection_enabled: false, auto_investigate: false, alert_cooldown_minutes: 30, max_replicas: 10, allow_scale_to_zero: false, plan_ttl_minutes: 60 };
  const [form, setForm] = useState<any>(blank);
  const [editing, setEditing] = useState<string | null>(null);
  const [sources, setSources] = useState<SourceValue[]>(PRESETS.minikube.map(blankSource));
  const [adding, setAdding] = useState("");
  const a = useAction();
  const setKind = (kind: string) => {
    setForm({ ...form, kind });
    if (!editing) setSources(PRESETS[kind].map(blankSource));
  };
  async function save(e: FormEvent) {
    e.preventDefault();
    const body = { ...form, allowed_namespaces: lines(form.allowed_namespaces), denied_namespaces: lines(form.denied_namespaces), grafana_url: form.grafana_url || null };
    const done = await a.run(async () => {
      const bodies = editing ? [] : sources.map((v) => connectorBody(v, form.name)); // invalid JSON fails before anything is created
      await api(editing ? `/environments/${editing}` : "/environments", { method: editing ? "PUT" : "POST", body });
      for (const c of bodies) {
        await api("/admin/connectors", { method: "POST", body: c }).catch((err) => {
          throw new Error(`Environment ${form.name} was created, but data source ${c.name} failed: ${err.message}. Add it under Connectors.`);
        });
      }
      return true;
    });
    list.reload();
    if (done) {
      setForm(blank);
      setEditing(null);
      setSources(PRESETS.minikube.map(blankSource));
    }
  }
  return (
    <>
      <PageHead title="Environments" sub="Clusters MOKS can read, and the infrastructure scope it may touch in each (independent of user roles)" />
      <div className="card">
        <Loading load={list} rows={3} />
        {list.data && <DataTable label="Environments" rows={list.data as any[]} rowKey={(e) => e.name} empty={<Empty title="No environments yet.">Add the first one below.</Empty>}
          menu={(e) => ({ label: `Actions for ${e.name}`, items: [{ label: "Edit", run: () => { setEditing(e.name); setForm({ ...e, allowed_namespaces: e.allowed_namespaces.join(", "), denied_namespaces: e.denied_namespaces.join(", "), grafana_url: e.grafana_url ?? "" }); } }] })}
          cols={[
            { key: "n", head: "Name", primary: true, cell: (e) => <><span className="row tight"><Truncate text={e.display_name} />{e.is_production && <Badge kind="warn">production</Badge>}</span><Truncate text={e.name} mono /></> },
            { key: "k", head: "Kind", cell: (e) => e.kind },
            { key: "a", head: "Allowed namespaces", cell: (e) => <Truncate text={e.allowed_namespaces.join(", ")} mono />, meta: (e) => `allowed ${e.allowed_namespaces.join(", ")}` },
            { key: "d", head: "Denied", cell: (e) => <Truncate text={e.denied_namespaces.join(", ")} mono />, meta: (e) => (e.denied_namespaces.length ? `denied ${e.denied_namespaces.join(", ")}` : "") },
            { key: "w", head: "Max window", cell: (e) => `${e.max_query_window_minutes} min` },
          ]} />}
      </div>
      <form className="card stack" style={{ marginTop: 14 }} onSubmit={save}>
        <h2>{editing ? `Edit ${editing}` : "Add environment"}</h2>
        <div className="row">
          <label>Name<input required disabled={!!editing} pattern="[a-z0-9][a-z0-9\-]*" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} /></label>
          <label>Display name<input value={form.display_name} onChange={(e) => setForm({ ...form, display_name: e.target.value })} /></label>
          <label>Cluster type<select value={form.kind} onChange={(e) => setKind(e.target.value)}><option value="minikube">Minikube</option><option value="eks">Amazon EKS</option><option value="kubernetes">Kubernetes (other)</option></select></label>
          <label className="inline"><input type="checkbox" checked={form.is_production} onChange={(e) => setForm({ ...form, is_production: e.target.checked })} /> production</label>
          <label className="inline"><input type="checkbox" checked={form.is_default} onChange={(e) => setForm({ ...form, is_default: e.target.checked })} /> default for investigations</label>
        </div>
        <div className="row">
          <label>Allowed namespaces (patterns)<input value={form.allowed_namespaces} onChange={(e) => setForm({ ...form, allowed_namespaces: e.target.value })} /></label>
          <label>Denied namespaces<input value={form.denied_namespaces} onChange={(e) => setForm({ ...form, denied_namespaces: e.target.value })} /></label>
          <label>Max query window (min)<input type="number" min={5} max={10080} value={form.max_query_window_minutes} onChange={(e) => setForm({ ...form, max_query_window_minutes: Number(e.target.value) })} /></label>
          <label>Grafana URL<input value={form.grafana_url} onChange={(e) => setForm({ ...form, grafana_url: e.target.value })} placeholder="https://…/grafana" /></label>
        </div>
        <div className="row">
          <label className="inline"><input type="checkbox" checked={form.detection_enabled} onChange={(e) => setForm({ ...form, detection_enabled: e.target.checked })} /> run detection rules</label>
          <label className="inline"><input type="checkbox" checked={form.auto_investigate} onChange={(e) => setForm({ ...form, auto_investigate: e.target.checked })} /> investigate firing alerts automatically</label>
          <label>Alert cooldown (min)<input type="number" min={1} max={1440} value={form.alert_cooldown_minutes} onChange={(e) => setForm({ ...form, alert_cooldown_minutes: Number(e.target.value) })} /></label>
          <label>Max replicas (scale policy)<input type="number" min={1} max={1000} value={form.max_replicas} onChange={(e) => setForm({ ...form, max_replicas: Number(e.target.value) })} /></label>
          <label className="inline"><input type="checkbox" checked={form.allow_scale_to_zero} onChange={(e) => setForm({ ...form, allow_scale_to_zero: e.target.checked })} /> allow scale to zero</label>
          <label>Plan TTL (min)<input type="number" min={5} max={1440} value={form.plan_ttl_minutes} onChange={(e) => setForm({ ...form, plan_ttl_minutes: Number(e.target.value) })} /></label>
        </div>
        {!editing && <div className="stack">
          <h3>Data sources</h3>
          {sources.map((v, i) => (
            <div key={i} className="card stack">
              <div className="row between"><strong>{SOURCES[v.source].label}</strong><button type="button" className="ghost small" onClick={() => setSources(sources.filter((_, n) => n !== i))}>Remove</button></div>
              <SourceFields value={v} onChange={(n) => setSources(sources.map((x, j) => (j === i ? n : x)))} />
            </div>
          ))}
          <label>Add data source
            <select value={adding} onChange={(e) => { if (e.target.value) setSources([...sources, blankSource(e.target.value)]); setAdding(""); }}>
              <option value="">Choose…</option>
              {Object.entries(SOURCES).map(([k, v]) => <option key={k} value={k}>{v.label}</option>)}
            </select>
          </label>
        </div>}
        <ErrorBox error={a.error} />
        <div className="row"><button disabled={a.busy}>{editing ? "Save" : "Create"}</button>{editing && <button type="button" className="ghost" onClick={() => { setEditing(null); setForm(blank); }}>Cancel</button>}</div>
      </form>
    </>
  );
}

// ---- Connectors -------------------------------------------------------------------------------

export function Connectors() {
  const { confirm: ask } = useFeedback();
  const list = useLoad(() => api<any[]>("/admin/connectors"), []);
  const envs = useLoad(() => api<any[]>("/environments"), []);
  const [environment, setEnvironment] = useState("");
  const [form, setForm] = useState<SourceValue>(blankSource("eks"));
  const [editing, setEditing] = useState<string | null>(null);
  const [tests, setTests] = useState<Record<string, any>>({});
  const a = useAction();

  async function save(e: FormEvent) {
    e.preventDefault();
    const body = () => connectorBody(form, environment || envs.data?.[0]?.name);
    if (await a.run(() => api(editing ? `/admin/connectors/${editing}` : "/admin/connectors", { method: editing ? "PUT" : "POST", body: body() }))) {
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
    if (await ask({ title: "Delete this connector?", body: "MOKS stops reading from this data source.", action: "Delete connector", danger: true })) { await a.run(() => api(`/admin/connectors/${id}`, { method: "DELETE" })); list.reload(); }
  }
  function edit(c: any) {
    setEditing(c.id);
    setEnvironment(c.environment);
    setForm({ ...blankSource(sourceOf(c)), name: c.name, enabled: c.enabled, config: c.config,
      credType: c.credential_ref?.type ?? "none", credName: c.credential_ref?.name ?? "", credPath: c.credential_ref?.path ?? "" });
  }
  return (
    <>
      <PageHead title="Connectors" sub="Live data sources per environment. Credentials are references to mounted Secrets, never stored here." />
      <div className="card">
        <Loading load={list} rows={4} />
        {list.data && <DataTable label="Connectors" rows={list.data as any[]} rowKey={(c) => c.id} empty={<Empty title="No connectors yet.">Add one below, then test it.</Empty>}
          menu={(c) => ({ label: `Actions for ${c.name}`, items: [{ label: "Test", run: () => test(c.id) }, { label: "Edit", run: () => edit(c) }, { label: "Delete", danger: true, run: () => remove(c.id) }] })}
          cols={[
            { key: "n", head: "Name", primary: true, cell: (c) => <Truncate text={c.name} /> },
            { key: "t", head: "Last test", status: true, cell: (c) => <>{tests[c.id]?.busy ? "testing…" : c.last_test_ok == null ? <span className="muted small">never</span> : <Badge kind={c.last_test_ok ? "ok" : "bad"}>{c.last_test_ok ? "ok" : "failed"}</Badge>}</> },
            { key: "ty", head: "Type", cell: (c) => <>{c.type} {!c.implemented && <Badge kind="neutral">planned</Badge>} {!c.enabled && <Badge>disabled</Badge>}</> },
            { key: "e", head: "Environment", cell: (c) => <span className="mono small">{c.environment}</span> },
            { key: "u", head: "Endpoint", cell: (c) => <Truncate text={c.config.base_url ?? c.config.mode ?? ""} mono />, meta: (c) => <Truncate text={c.config.base_url ?? c.config.mode ?? ""} mono /> },
            { key: "c", head: "Credential", wide: true, cell: (c) => <Truncate text={c.credential ?? ""} mono /> },
            { key: "m", head: "Test result", cell: (c) => <span className="small">{c.last_test_at && <Time iso={c.last_test_at} />}{c.last_test_message && <span className="muted clamp-2">{c.last_test_message}</span>}</span> },
          ]} />}
      </div>
      <form className="card stack" style={{ marginTop: 14 }} onSubmit={save}>
        <h2>{editing ? "Edit connector" : "Add connector"}</h2>
        <div className="row">
          <label>Environment<select value={environment} onChange={(e) => setEnvironment(e.target.value)}>{envs.data?.map((e) => <option key={e.name}>{e.name}</option>)}</select></label>
          <label>Data source
            <select value={form.source} disabled={!!editing} onChange={(e) => setForm(blankSource(e.target.value))}>
              {Object.entries(SOURCES).map(([k, v]) => <option key={k} value={k}>{v.label}</option>)}
            </select>
          </label>
        </div>
        <SourceFields value={form} onChange={setForm} />
        <ErrorBox error={a.error} />
        <div className="row"><button disabled={a.busy}>{editing ? "Save" : "Add"}</button>{editing && <button type="button" className="ghost" onClick={() => setEditing(null)}>Cancel</button>}</div>
      </form>
    </>
  );
}

// ---- LLM providers ----------------------------------------------------------------------------

interface Preset { label: string; provider: string; endpoint?: string; key?: string; models?: string[]; prices?: Record<string, [string, string]>; hint: string }

/** A preset only fills in the form. Prices are USD per million tokens, from the provider's list, and worth checking. */
const LLM_PRESETS: Record<string, Preset> = {
  anthropic: { label: "Anthropic Claude", provider: "anthropic", key: "MOKS_CRED_ANTHROPIC_API_KEY", hint: "Claude has no embedding API, so embeddings need another provider.",
    models: ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"],
    prices: { "claude-opus-5-5": ["4", "20"], "claude-sonnet-5-5": ["2", "10"], "claude-haiku-5-5": ["0.10", "0.50"] } },
  openai: { label: "OpenAI", provider: "openai", key: "MOKS_CRED_OPENAI_API_KEY", hint: "Use a model name from your OpenAI account." },
  google: { label: "Google AI (Gemini)", provider: "gemini", key: "MOKS_CRED_GEMINI_API_KEY", hint: "Uses the Gemini API with an API key." },
  azure: { label: "Azure OpenAI", provider: "azure_openai", endpoint: "https://YOUR-RESOURCE.openai.azure.com", key: "MOKS_CRED_AZURE_OPENAI_API_KEY", hint: "Model is your deployment name." },
  bedrock: { label: "Amazon Bedrock", provider: "bedrock", models: ["us.amazon.nova-2-lite-v1:0"], hint: "Uses the pod's AWS identity, or the keys from scripts/create-runtime-key.sh." },
  mistral: { label: "Mistral", provider: "openai", endpoint: "https://api.mistral.ai/v1", key: "MOKS_CRED_MISTRAL_API_KEY", hint: "OpenAI-compatible API." },
  groq: { label: "Groq", provider: "openai", endpoint: "https://api.groq.com/openai/v1", key: "MOKS_CRED_GROQ_API_KEY", hint: "OpenAI-compatible API." },
  deepseek: { label: "DeepSeek", provider: "openai", endpoint: "https://api.deepseek.com/v1", key: "MOKS_CRED_DEEPSEEK_API_KEY", hint: "OpenAI-compatible API." },
  xai: { label: "xAI Grok", provider: "openai", endpoint: "https://api.x.ai/v1", key: "MOKS_CRED_XAI_API_KEY", hint: "OpenAI-compatible API." },
  together: { label: "Together AI", provider: "openai", endpoint: "https://api.together.xyz/v1", key: "MOKS_CRED_TOGETHER_API_KEY", hint: "OpenAI-compatible API." },
  fireworks: { label: "Fireworks AI", provider: "openai", endpoint: "https://api.fireworks.ai/inference/v1", key: "MOKS_CRED_FIREWORKS_API_KEY", hint: "OpenAI-compatible API." },
  openrouter: { label: "OpenRouter", provider: "openai", endpoint: "https://openrouter.ai/api/v1", key: "MOKS_CRED_OPENROUTER_API_KEY", hint: "One key for many providers." },
  ollama: { label: "Ollama", provider: "ollama", endpoint: "http://ollama.ollama:11434", hint: "Self-hosted; the endpoint is required." },
  compatible: { label: "Other OpenAI-compatible server", provider: "openai", endpoint: "https://", hint: "vLLM, LiteLLM, LM Studio or any server that speaks the OpenAI chat API." },
};
const presetOf = (form: any) => Object.keys(LLM_PRESETS).find((k) => LLM_PRESETS[k].provider === form.provider && (LLM_PRESETS[k].endpoint ?? "") === (form.endpoint ?? "")) ?? (form.provider === "openai" ? "compatible" : Object.keys(LLM_PRESETS).find((k) => LLM_PRESETS[k].provider === form.provider) ?? "compatible");

export { LLM_PRESETS, presetOf };

export function LLMProviders() {
  const { confirm: ask } = useFeedback();
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
        <Loading load={list} rows={3} />
        {list.data && <DataTable label="LLM providers" rows={list.data as any[]} rowKey={(c) => c.id} empty={<Empty title="No LLM configured.">Investigations use rule-based diagnosis only.</Empty>}
          menu={(c) => ({ label: `Actions for ${c.name}`, items: [
            { label: "Test", run: () => test(c.id) },
            { label: c.is_active ? "Deactivate" : "Activate", run: async () => { await a.run(() => api(`/admin/llm/${c.id}/activate${qs({ active: !c.is_active })}`, { method: "POST" })); list.reload(); } },
            { label: "Edit", run: () => { setEditing(c.id); setForm({ ...c, endpoint: c.endpoint ?? "", region: c.region ?? "us-east-1", credType: c.credential_ref?.type ?? "none", credName: c.credential_ref?.name ?? "" }); } },
            ...(!c.is_active ? [{ label: "Delete", danger: true, run: async () => { if (await ask({ title: `Delete ${c.name}?`, body: "Its usage history is kept.", action: "Delete provider", danger: true })) { await a.run(() => api(`/admin/llm/${c.id}`, { method: "DELETE" })); list.reload(); } } }] : []),
          ] })}
          cols={[
            { key: "n", head: "Name", primary: true, cell: (c) => <Truncate text={c.name} /> },
            { key: "s", head: "Status", status: true, cell: (c) => (c.is_active ? <Badge kind="ok">active</Badge> : <Badge>inactive</Badge>) },
            { key: "p", head: "Purpose", cell: (c) => c.purpose },
            { key: "m", head: "Provider / model", cell: (c) => <span className="mono small">{c.provider} / {c.model}{c.region && <span className="muted"> {c.region}</span>}</span> },
            { key: "e", head: "Endpoint", wide: true, cell: (c) => <Truncate text={c.endpoint ?? ""} mono /> },
            { key: "c", head: "Credential", wide: true, cell: (c) => <Truncate text={c.credential ?? ""} mono /> },
            { key: "b", head: "Budget (24 h)", cell: (c) => <span className="small">${Number(c.spent_last_24h_usd ?? 0).toFixed(4)} of ${c.daily_budget_usd}</span> },
            { key: "t", head: "Test", cell: (c) => (tests[c.id] ? <span className={`small ${tests[c.id].ok ? "" : "muted"}`}>{tests[c.id].message}</span> : "") },
          ]} />}
      </div>
      <form className="card stack" style={{ marginTop: 14 }} onSubmit={save}>
        <h2>{editing ? "Edit provider" : "Add provider"}</h2>
        <div className="row">
          <label>Name<input required pattern="[a-z0-9][a-z0-9\-]*" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} /></label>
          <label>Purpose<select value={form.purpose} onChange={(e) => setForm({ ...form, purpose: e.target.value })}><option>chat</option><option>embedding</option></select></label>
          <label>Provider<select value={presetOf(form)} onChange={(e) => { const p = LLM_PRESETS[e.target.value]; setForm({ ...form, provider: p.provider, endpoint: p.endpoint ?? "", model: "", credType: p.key ? "env" : "none", credName: p.key ?? "" }); }}>{Object.entries(LLM_PRESETS).map(([k, p]) => <option key={k} value={k}>{p.label}</option>)}</select></label>
          <label>Model<input required list="llm-models" value={form.model} onChange={(e) => { const price = LLM_PRESETS[presetOf(form)].prices?.[e.target.value]; setForm({ ...form, model: e.target.value, ...(price ? { input_price_per_mtok: price[0], output_price_per_mtok: price[1] } : {}) }); }} placeholder={form.provider === "bedrock" ? "inference profile or model ID" : "model name"} /></label>
          <datalist id="llm-models">{LLM_PRESETS[presetOf(form)].models?.map((m) => <option key={m} value={m} />)}</datalist>
          <datalist id="aws-regions">{REGIONS.map((r) => <option key={r} value={r} />)}</datalist>
        </div>
        <div className="row">
          {form.provider === "bedrock" ? <label>Region<input list="aws-regions" value={form.region} onChange={(e) => setForm({ ...form, region: e.target.value })} /></label> :
            <label>Endpoint{form.provider === "ollama" ? " (required)" : " (optional)"}<input value={form.endpoint} onChange={(e) => setForm({ ...form, endpoint: e.target.value })} placeholder={form.provider === "ollama" ? "http://ollama.ollama:11434" : "default"} /></label>}
          {form.provider !== "bedrock" && (
            <label>API key reference<select value={form.credType} onChange={(e) => setForm({ ...form, credType: e.target.value })}><option value="none">none</option><option value="env">environment variable</option></select></label>
          )}
          {form.credType === "env" && form.provider !== "bedrock" && <label>Variable<input value={form.credName} onChange={(e) => setForm({ ...form, credName: e.target.value })} placeholder="MOKS_CRED_OPENAI_API_KEY" /></label>}
          <span className="small muted">{LLM_PRESETS[presetOf(form)].hint}</span>
        </div>
        <div className="row">
          <label>Timeout (s)<input type="number" value={form.timeout_seconds} onChange={(e) => setForm({ ...form, timeout_seconds: Number(e.target.value) })} /></label>
          <label>Max output tokens<input type="number" value={form.max_output_tokens} onChange={(e) => setForm({ ...form, max_output_tokens: Number(e.target.value) })} /></label>
          <label>Daily budget (USD)<input value={form.daily_budget_usd} onChange={(e) => setForm({ ...form, daily_budget_usd: e.target.value })} /></label>
          <label>Input $ / 1M tokens<input value={form.input_price_per_mtok} onChange={(e) => setForm({ ...form, input_price_per_mtok: e.target.value })} /></label>
          <label>Output $ / 1M tokens<input value={form.output_price_per_mtok} onChange={(e) => setForm({ ...form, output_price_per_mtok: e.target.value })} /></label>
          {Number(form.input_price_per_mtok) === 0 && Number(form.output_price_per_mtok) === 0 && form.provider !== "ollama" && <span className="small muted">With no prices set, spend counts as $0 and the daily budget cannot limit this model.</span>}
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
        <div className="table-wrap wide-form" role="region" aria-label="Users and role bindings" tabIndex={0}>
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
                    <button className="ghost small" aria-label={`Remove ${b.role} on ${b.environment}/${b.namespace} from ${u.email}`} style={{ marginLeft: 4 }} onClick={async () => { await a.run(() => api(`/admin/users/${u.id}/bindings/${b.id}`, { method: "DELETE" })); list.reload(); }}>×</button></div>
                ))}{!u.bindings.length && <span className="muted">no access</span>}</td>
                <td>
                  <div className="row">
                    <select aria-label="Role" value={g.role} onChange={(e) => setGrant({ ...grant, [u.id]: { ...g, role: e.target.value } })}>{roles.data?.map((r) => <option key={r.role}>{r.role}</option>)}</select>
                    <input aria-label="Environment" style={{ width: 90 }} value={g.environment} onChange={(e) => setGrant({ ...grant, [u.id]: { ...g, environment: e.target.value } })} title="environment pattern" />
                    <input aria-label="Namespace" style={{ width: 110 }} value={g.namespace} onChange={(e) => setGrant({ ...grant, [u.id]: { ...g, namespace: e.target.value } })} title="namespace pattern" />
                    <button className="small" onClick={async () => { await a.run(() => api(`/admin/users/${u.id}/bindings`, { method: "POST", body: g })); list.reload(); }}>Grant</button>
                  </div>
                </td>
                <td>{u.id !== me?.id && <button className="ghost small" onClick={async () => { await a.run(() => api(`/admin/users/${u.id}/active${qs({ active: !u.is_active })}`, { method: "POST" })); list.reload(); }}>{u.is_active ? "Disable" : "Enable"}</button>}</td>
              </tr>
            );
          })}</tbody>
        </table>
        </div>
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

const AUDIT_RANGES: Record<string, number> = { "Last hour": 1, "Last 24 hours": 24, "Last 7 days": 168, "Last 30 days": 720 };
const PAGE = 200;

export function Audit() {
  const blank = { action: "", actor: "", decision: "", outcome: "", environment: "", namespace: "", resource: "", q_text: "", range: "", since: "", until: "" };
  const [f, setF] = useState(blank);
  const [applied, setApplied] = useState(blank);
  const [before, setBefore] = useState<number | undefined>();
  const [older, setOlder] = useState<any[]>([]);
  const facets = useLoad(() => api<Record<string, string[]>>("/admin/audit/facets"), []);
  const params = (x: typeof f) => {
    const { range, since, until, ...rest } = x;
    return { ...rest, since: range && range !== "custom" ? new Date(Date.now() - AUDIT_RANGES[range] * 3600_000).toISOString() : since ? new Date(since).toISOString() : undefined,
      until: range === "custom" && until ? new Date(until).toISOString() : undefined };
  };
  const list = useLoad(() => api<{ events: any[] }>(`/admin/audit${qs({ ...params(applied), limit: PAGE })}`), [applied]);
  const events = [...(list.data?.events ?? []), ...older];
  const [verify, setVerify] = useState<any>();
  const [open, setOpen] = useState<any>(null);
  const clear = () => { setF(blank); setApplied(blank); setOlder([]); setBefore(undefined); };
  const chips: Chip[] = (Object.entries(applied) as [keyof typeof blank, string][]).filter(([k, v]) => v && k !== "since" && k !== "until").map(([k, v]) => ({ key: k, label: `${k.replace("q_text", "details")}: ${v}`, onRemove: () => apply({ [k]: "" }) }));
  const apply = (patch: Partial<typeof f>) => { const n = { ...f, ...patch }; setF(n); setApplied(n); setOlder([]); setBefore(undefined); };
  const pick = (key: keyof typeof f, label: string, facet: string, any: string) => (
    <label>{label}
      <select value={f[key]} onChange={(e) => apply({ [key]: e.target.value })}>
        <option value="">{any}</option>
        {facets.data?.[facet]?.map((v) => <option key={v}>{v}</option>)}
      </select>
    </label>
  );
  async function loadOlder() {
    const last = events[events.length - 1]?.id;
    if (!last) return;
    const r = await api<{ events: any[] }>(`/admin/audit${qs({ ...params(applied), limit: PAGE, before_id: last })}`);
    setOlder([...older, ...r.events]);
    setBefore(r.events.length);
  }
  return (
    <>
      <PageHead title="Audit log" sub="Append-only, hash-chained record of sign-ins, tool calls, authorization decisions and configuration changes">
        <button className="ghost" onClick={async () => setVerify(await api("/admin/audit/verify"))}>Verify chain</button>
      </PageHead>
      {verify && <div className={`alert ${verify.ok ? "info" : "error"}`} style={{ marginBottom: 12 }}>{verify.ok ? `Chain intact: ${verify.checked} records verified.` : `Chain broken at record #${verify.broken_at} (after ${verify.checked} checked).`}</div>}
      <form onSubmit={(e) => { e.preventDefault(); apply({}); }}>
        <FilterPanel chips={chips} onClear={clear} onApply={() => apply({})} applying={list.loading} applyLabel="Filter"
          advanced={<div className="filters">
            {pick("outcome", "Outcome", "outcome", "Any")}
            {pick("namespace", "Namespace", "namespace", "All namespaces")}
            <label>Resource<input value={f.resource} onChange={(e) => setF({ ...f, resource: e.target.value })} placeholder="tool name, plan id" /></label>
            <label>Details contain<input value={f.q_text} onChange={(e) => setF({ ...f, q_text: e.target.value })} /></label>
            {f.range === "custom" && <>
              <label>From<input type="datetime-local" value={f.since} onChange={(e) => setF({ ...f, since: e.target.value })} /></label>
              <label>To<input type="datetime-local" value={f.until} onChange={(e) => setF({ ...f, until: e.target.value })} /></label>
            </>}
          </div>}>
          {pick("action", "Action", "action", "All actions")}
          {pick("actor", "Actor", "actor_email", "All actors")}
          <label>Decision<select value={f.decision} onChange={(e) => apply({ decision: e.target.value })}><option value="">Any</option><option>allow</option><option>deny</option><option>n/a</option></select></label>
          {pick("environment", "Environment", "environment", "All environments")}
          <label>Time
            <select value={f.range} onChange={(e) => apply({ range: e.target.value, since: "", until: "" })}>
              <option value="">Any time</option>
              {Object.keys(AUDIT_RANGES).map((r) => <option key={r}>{r}</option>)}
              <option value="custom">Custom range…</option>
            </select>
          </label>
        </FilterPanel>
      </form>
      <div className="card mt">
        <Loading load={list} rows={8} />
        {list.data && <DataTable label="Audit records" rows={events} rowKey={(e) => String(e.id)} onRow={setOpen} empty={<Empty title="No records match." />} cols={[
          { key: "a", head: "Action", primary: true, cell: (e) => <Truncate text={e.action} mono /> },
          { key: "d", head: "Decision", status: true, cell: (e) => (e.decision === "deny" ? <Badge kind="bad">deny</Badge> : e.decision === "allow" ? <Badge kind="ok">allow</Badge> : <span className="muted small">—</span>) },
          { key: "t", head: "Time", cell: (e) => <span className="small nowrap">{new Date(e.ts).toLocaleString()}</span> },
          { key: "w", head: "Actor", cell: (e) => <Truncate text={e.actor_email ?? "system"} />, meta: (e) => e.actor_email ?? "system" },
          { key: "r", head: "Resource", cell: (e) => <Truncate text={[e.resource_type, e.resource_id].filter(Boolean).join(":")} mono />, meta: (e) => <Truncate text={[e.resource_type, e.resource_id].filter(Boolean).join(":")} mono /> },
          { key: "s", head: "Scope", cell: (e) => <Truncate text={[e.environment, e.namespace].filter(Boolean).join("/")} mono />, meta: (e) => [e.environment, e.namespace].filter(Boolean).join("/") },
          { key: "o", head: "Outcome", wide: true, cell: (e) => <span className="small">{e.outcome}</span> },
          { key: "n", head: "#", wide: true, cell: (e) => <span className="mono small">{e.chain_seq}</span> },
        ]} />}
        <div className="row between small muted mt">
          <span>{events.length} records</span>
          {list.data?.events.length === PAGE && (before === undefined || before === PAGE) && <button className="ghost small" onClick={loadOlder}>Load older</button>}
        </div>
      </div>
      {open && <Drawer title={`Audit record ${open.chain_seq}`} onClose={() => setOpen(null)}>
        <dl className="kv small">
          <dt>Time</dt><dd>{new Date(open.ts).toLocaleString(undefined, { timeZoneName: "short" })}</dd><dt>Actor</dt><dd>{open.actor_email ?? "system"}</dd><dt>Action</dt><dd className="mono">{open.action}</dd>
          <dt>Resource</dt><dd className="mono">{[open.resource_type, open.resource_id].filter(Boolean).join(":")}</dd><dt>Scope</dt><dd className="mono">{[open.environment, open.namespace].filter(Boolean).join("/") || "—"}</dd>
          <dt>Decision</dt><dd>{open.decision}</dd><dt>Outcome</dt><dd>{open.outcome}</dd>
        </dl>
        <pre className="logbox">{JSON.stringify({ details: open.details, ip: open.ip, hash: open.hash }, null, 2)}</pre>
      </Drawer>}
    </>
  );
}
