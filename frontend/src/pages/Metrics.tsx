import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { api, qs } from "../api";
import { Badge, ErrorBox, LineChart, PageHead, type Scope, Time, useInterval, useLoad, useScopes, rowAction } from "../components";

interface Result {
  source: { environments: string[]; connector: string; language: string }; ok: boolean; error?: string; denied?: boolean; namespace?: string | null;
  series?: any[]; total_series?: number; truncated?: boolean; result_type?: string; step?: number; warnings?: string[]; query?: string; collected_at?: string;
}
interface Response { results: Result[]; errors: { environment: string; error: string }[]; note: string | null }

const WINDOWS = [15, 30, 60, 180, 360, 720, 1440, 10080];
const windowLabel = (m: number) => (m >= 1440 ? `last ${m / 1440} d` : m >= 60 ? `last ${m / 60} h` : `last ${m} min`);
const REFRESH = [0, 15, 30, 60, 300];

/** Searchable multi-select dropdown. Nothing selected means all the options. */
export function MultiSelect({ label, options, value, onChange, allLabel }: { label: string; options: { value: string; label?: string }[]; value: string[]; onChange: (v: string[]) => void; allLabel: string }) {
  const [q, setQ] = useState("");
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const away = (e: MouseEvent) => { if (!box.current?.contains(e.target as Node)) setOpen(false); };
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", away);
    document.addEventListener("keydown", esc);
    return () => { document.removeEventListener("mousedown", away); document.removeEventListener("keydown", esc); };
  }, [open]);
  const shown = options.filter((o) => (o.label ?? o.value).toLowerCase().includes(q.toLowerCase()));
  const name = (v: string) => options.find((o) => o.value === v)?.label ?? v;
  const text = !value.length ? allLabel : value.length === 1 ? name(value[0]) : `${value.length} selected`;
  return (
    <div className="ms" ref={box}>
      <span className="ms-label">{label}</span>
      <button type="button" className="ms-button" aria-haspopup="listbox" aria-expanded={open} aria-label={`${label}: ${text}`} onClick={() => setOpen(!open)}>
        <span>{text}</span><span aria-hidden>▾</span>
      </button>
      {open && <div className="ms-menu" role="listbox" aria-multiselectable aria-label={label}>
        {options.length > 6 && <input autoFocus aria-label={`Search ${label}`} placeholder="Search…" value={q} onChange={(e) => setQ(e.target.value)} />}
        <button type="button" role="option" aria-selected={!value.length} className={`ms-option ${!value.length ? "on" : ""}`} onClick={() => { onChange([]); setOpen(false); }}>{allLabel}</button>
        {shown.slice(0, 200).map((o) => {
          const on = value.includes(o.value);
          return <button type="button" role="option" aria-selected={on} key={o.value} className={`ms-option ${on ? "on" : ""}`}
            onClick={() => onChange(on ? value.filter((v) => v !== o.value) : [...value, o.value])}><span className="ms-check" aria-hidden>{on ? "✓" : ""}</span>{o.label ?? o.value}</button>;
        })}
        {!shown.length && <div className="small muted">No matches.</div>}
      </div>}
    </div>
  );
}

function Results({ data, loading, unit }: { data?: Response; loading: boolean; unit?: "bytes" | "ratio" }) {
  const [view, setView] = useState<"chart" | "table">("chart");
  if (!data) return null;
  const ok = data.results.filter((r) => r.ok);
  return (
    <div className="stack" style={{ marginTop: 14 }}>
      {data.note && <div className="small muted">{data.note}</div>}
      <div className="card">
        <div className="row between">
          <div className="row small">
            <span>{ok.length} of {data.results.length} source(s) responded{loading ? " · refreshing…" : ""}</span>
            {data.results.map((r, i) => <Badge key={i} kind={r.ok ? "ok" : "bad"}>{r.source.environments.join(", ")}{r.namespace ? `/${r.namespace}` : ""} · {r.source.connector}</Badge>)}
          </div>
          <div className="tabs" style={{ margin: 0, border: 0 }}>{(["chart", "table"] as const).map((v) => <button type="button" key={v} className={view === v ? "active" : ""} onClick={() => setView(v)}>{v}</button>)}</div>
        </div>
        {[...data.errors.map((e) => `${e.environment}: ${e.error}`), ...data.results.filter((r) => !r.ok).map((r) => `${r.source.environments.join(", ")}: ${r.error}`)].map((m, i) => <div key={i} className="alert warn small" style={{ marginTop: 8 }}>{m}</div>)}
      </div>
      {ok.map((r, i) => {
        const instant = r.result_type !== "matrix";
        const empty = !r.series?.length;
        return (
          <div key={i} className="card stack">
            <div className="row between small">
              <strong>{r.source.environments.join(", ")}{r.namespace ? ` / ${r.namespace}` : ""}</strong>
              <span className="muted">{r.source.connector} · {r.source.language === "promql" ? "PromQL" : "CloudWatch Metrics"}{r.step ? ` · step ${r.step}s` : ""} · collected <Time iso={r.collected_at} /></span>
            </div>
            {empty ? <div className="empty">The query ran and returned no series. Check the labels and the time range; this does not show the value is zero.</div> :
              view === "chart" && !instant ? <LineChart series={r.series!} unit={unit} /> : <SeriesTable series={r.series!} />}
            {r.truncated && <div className="small muted">Showing the first {r.series?.length} of {r.total_series} series.</div>}
            {r.warnings?.map((w, n) => <div key={n} className="alert warn small">{w}</div>)}
            {r.query && <code className="small">{r.query}</code>}
          </div>
        );
      })}
    </div>
  );
}

function SeriesTable({ series }: { series: any[] }) {
  const rows = series.map((s) => {
    const value = s.value ?? s.values?.[s.values.length - 1];
    return { labels: Object.entries(s.metric ?? {}).map(([k, v]) => `${k}=${v}`).join(" ") || "(no labels)", value: value?.[1], at: value?.[0] };
  });
  return (
    <div className="table-wrap">
      <table>
        <thead><tr><th>Series</th><th>Latest value</th><th>At</th></tr></thead>
        <tbody>{rows.map((r, i) => <tr key={i}><td className="mono small">{r.labels}</td><td>{r.value ?? "—"}</td><td className="small muted">{r.at ? new Date(r.at * 1000).toLocaleTimeString() : ""}</td></tr>)}</tbody>
      </table>
    </div>
  );
}

function useRunner<B>(path: string) {
  const [data, setData] = useState<Response>();
  const [error, setError] = useState<unknown>();
  const [busy, setBusy] = useState(false);
  const ctl = useRef<AbortController | null>(null);
  async function run(body: B) {
    ctl.current?.abort(); // an obsolete request must not overwrite a newer result
    const c = new AbortController();
    ctl.current = c;
    setBusy(true);
    setError(null);
    try {
      const r = await api<Response>(path, { method: "POST", body: body as any, signal: c.signal });
      if (ctl.current === c) setData(r);
    } catch (e: any) {
      if (e?.name !== "AbortError" && ctl.current === c) setError(e);
    } finally {
      if (ctl.current === c) setBusy(false);
    }
  }
  const cancel = () => { ctl.current?.abort(); ctl.current = null; setBusy(false); };
  useEffect(() => () => ctl.current?.abort(), []);
  return { data, error, busy, run, cancel, clear: () => setData(undefined) };
}

export function Metrics() {
  const scopes = useScopes();
  const sources = useLoad(() => api<any>("/metrics/sources"), []);
  const [mode, setMode] = useState<"explore" | "query" | "cloudwatch">("explore");
  const [envs, setEnvs] = useState<string[]>([]);
  const [query, setQuery] = useState("");
  const [picked, setPicked] = useState<string[]>([]);
  const options = (sources.data?.environments ?? []).map((e: any) => ({ value: e.environment, label: e.display_name }));
  const names = envs.length ? envs.join(",") : "all";
  const dataSources = (sources.data?.environments ?? []).filter((e: any) => !envs.length || envs.includes(e.environment))
    .flatMap((e: any) => e.data_sources.filter((d: any) => d.type === "prometheus").map((d: any) => ({ value: d.id, label: `${e.environment} · ${d.name}` })));
  const connectors = picked.filter((id) => dataSources.some((d: any) => d.value === id));
  const raw = (sources.data?.environments ?? []).filter((e: any) => !envs.length || envs.includes(e.environment)).some((e: any) => e.raw_query);
  const cw = (sources.data?.environments ?? []).filter((e: any) => e.data_sources.some((d: any) => d.type === "cloudwatch"));
  return (
    <>
      <PageHead title="Metrics" sub="Explore metrics through filters, or write PromQL yourself. All authorized environments by default." />
      <ErrorBox error={sources.error} />
      <div className="tabs">
        <button className={mode === "explore" ? "active" : ""} onClick={() => setMode("explore")}>Explore</button>
        <button className={mode === "query" ? "active" : ""} onClick={() => setMode("query")}>Query</button>
        {cw.length > 0 && <button className={mode === "cloudwatch" ? "active" : ""} onClick={() => setMode("cloudwatch")}>CloudWatch</button>}
      </div>
      {mode !== "cloudwatch" && <div className="card filters" style={{ marginBottom: 14 }}>
        <MultiSelect label="Environments" allLabel="All authorized environments" options={options} value={envs} onChange={setEnvs} />
        {dataSources.length > 1 && <MultiSelect label="Data sources" allLabel="All data sources" options={dataSources} value={connectors} onChange={setPicked} />}
      </div>}
      {mode === "explore" && <Explore environments={names} connectors={connectors} scopes={scopes.data} sources={sources.data} onQuery={(q) => { setQuery(q); setMode("query"); }} />}
      {mode === "query" && <QueryEditor environments={names} connectors={connectors} query={query} onQuery={setQuery} canRaw={raw} />}
      {mode === "cloudwatch" && <CloudWatch envs={cw} namespaces={sources.data?.cloudwatch_namespaces ?? {}} />}
    </>
  );
}

function Explore({ environments, connectors, scopes, sources, onQuery }: { environments: string; connectors: string[]; scopes?: Scope[]; sources: any; onQuery: (q: string) => void }) {
  const [search, setSearch] = useState("");
  const [metric, setMetric] = useState("");
  const [resource, setResource] = useState("");
  const [namespaces, setNamespaces] = useState<string[]>([]);
  const [filters, setFilters] = useState<Record<string, string[]>>({});
  const [f, setF] = useState({ aggregate: "sum", by: [] as string[], rate: "", rate_window: "5m", rank: "", n: 10, instant: false, minutes: 60, step: "" });
  const [refresh, setRefresh] = useState(0);
  const ids = connectors.join(",");
  const catalog = useLoad(() => api<any>(`/metrics/catalog${qs({ environments, connectors: ids, limit: 5000 })}`), [environments, ids]);
  const namespace1 = namespaces.length === 1 ? namespaces[0] : undefined;
  const labels = useLoad(() => (metric ? api<any>(`/metrics/labels${qs({ metric, environments, connectors: ids, namespace: namespace1 })}`) : Promise.resolve(null)), [metric, environments, ids, namespace1]);
  const allNamespaces = [...new Set((scopes ?? []).flatMap((s) => s.namespaces))].sort();
  const resourceLabels: string[] = sources?.resource_labels?.[resource] ?? [];
  const found: string[] = (labels.data?.labels ?? []).filter((l: string) => l !== "namespace");
  const relevant = resource && resourceLabels.length ? found.filter((l) => resourceLabels.includes(l)) : found;
  const items = useMemo(() => {
    const need = search.toLowerCase();
    const hit = (catalog.data?.items ?? []).filter((m: any) => m.name.toLowerCase().includes(need));
    return hit.slice(0, 200);
  }, [catalog.data, search]);
  const info = (catalog.data?.items ?? []).find((m: any) => m.name === metric);
  const r = useRunner<any>("/metrics/explore");
  const body = () => ({
    metric, environments, connectors, namespaces, aggregate: f.aggregate, by: f.by, rate: f.rate === "" ? undefined : f.rate === "true", rate_window: f.rate_window,
    instant: f.instant, minutes: f.minutes, step: f.step ? Number(f.step) : undefined,
    labels: Object.fromEntries(Object.entries(filters).filter(([, v]) => v.length).map(([k, v]) => [k, v.length === 1 ? v[0] : `~${v.map((x) => x.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|")}`])),
    ...(f.rank ? { [f.rank]: f.n } : {}),
  });
  useEffect(() => { if (metric) r.run(body()); }, [metric, environments, ids, JSON.stringify(namespaces), JSON.stringify(filters), JSON.stringify(f)]); // eslint-disable-line react-hooks/exhaustive-deps
  useInterval(() => { if (metric && !r.busy) r.run(body()); }, refresh * 1000, refresh > 0);
  const set = (k: keyof typeof f, v: any) => setF({ ...f, [k]: v });
  return (
    <>
      <div className="card filters">
        <label>Resource type
          <select value={resource} onChange={(e) => setResource(e.target.value)}>
            <option value="">Any</option>
            {sources?.environments && [...new Set<string>(sources.environments.flatMap((x: any) => x.data_sources.filter((d: any) => d.type === "prometheus").flatMap((d: any) => d.resource_types)))].map((t) => <option key={t}>{t}</option>)}
          </select>
        </label>
        <MultiSelect label="Namespaces" allLabel="All namespaces" options={allNamespaces.map((n) => ({ value: n }))} value={namespaces} onChange={setNamespaces} />
        <label>Metric<input value={search} onChange={(e) => setSearch(e.target.value)} placeholder={`Search ${catalog.data?.total ?? ""} metrics`} /></label>
      </div>
      <ErrorBox error={catalog.error} />
      {catalog.data?.errors?.map((e: any, i: number) => <div key={i} className="alert warn small" style={{ marginTop: 8 }}>{e.environment}: {e.error}</div>)}
      <div className="grid metrics-layout" style={{ marginTop: 14 }}>
        <div className="card metric-list">
          <table>
            <tbody>{items.map((m: any) => (
              <tr key={m.name} className={`clickable ${metric === m.name ? "selected" : ""}`} {...rowAction(() => { setMetric(m.name); setFilters({}); })}>
                <td className="mono small">{m.name}<div className="muted">{[m.type, m.unit].filter(Boolean).join(" · ")}{m.environments.length > 1 ? ` · ${m.environments.length} environments` : ""}</div></td>
              </tr>
            ))}</tbody>
          </table>
          {!items.length && !catalog.loading && <div className="empty">No metrics match.</div>}
          {catalog.data?.total > items.length && <div className="small muted">Showing 200 of {catalog.data.total}. Search to narrow.</div>}
        </div>
        <div>
          {!metric ? <div className="card empty">Pick a metric to chart it.</div> : <>
            <div className="card stack">
              <div className="row between"><strong className="mono">{metric}</strong>
                <button type="button" className="ghost small" onClick={() => { const q = r.data?.results.find((x) => x.query)?.query; if (q) onQuery(q); }} disabled={!r.data}>Open in Query</button></div>
              {info?.help && <div className="small muted">{info.help}</div>}
              <div className="filters">
                {relevant.slice(0, 8).map((l) => <LabelFilter key={l} metric={metric} label={l} environments={environments} connectors={ids} namespace={namespace1} value={filters[l] ?? []} onChange={(v) => setFilters({ ...filters, [l]: v })} />)}
              </div>
              {labels.data?.errors?.length > 0 && <div className="small muted">Label values need a single namespace for your access level.</div>}
              <div className="filters">
                <label>Aggregate<select value={f.aggregate} onChange={(e) => set("aggregate", e.target.value)}>{["sum", "avg", "max", "min", "count", "none"].map((a) => <option key={a}>{a}</option>)}</select></label>
                <MultiSelect label="Group by" allLabel="None" options={found.map((l) => ({ value: l }))} value={f.by} onChange={(v) => set("by", v.slice(0, 5))} />
                <label>Rate<select value={f.rate} onChange={(e) => set("rate", e.target.value)}><option value="">Auto (counters)</option><option value="true">Always</option><option value="false">Never</option></select></label>
                <label>Rate window<select value={f.rate_window} onChange={(e) => set("rate_window", e.target.value)}>{["1m", "5m", "15m", "1h"].map((w) => <option key={w}>{w}</option>)}</select></label>
                <label>Rank<select value={f.rank} onChange={(e) => set("rank", e.target.value)}><option value="">All series</option><option value="topk">Top N</option><option value="bottomk">Bottom N</option></select></label>
                {f.rank && <label>N<input type="number" min={1} max={100} value={f.n} onChange={(e) => set("n", Number(e.target.value))} /></label>}
                <label>Type<select value={String(f.instant)} onChange={(e) => set("instant", e.target.value === "true")}><option value="false">Range</option><option value="true">Instant</option></select></label>
                <label>Time<select value={f.minutes} onChange={(e) => set("minutes", Number(e.target.value))}>{WINDOWS.map((m) => <option key={m} value={m}>{windowLabel(m)}</option>)}</select></label>
                <label>Step (s)<input type="number" min={5} max={3600} value={f.step} placeholder="auto" onChange={(e) => set("step", e.target.value)} /></label>
                <label>Refresh<select value={refresh} onChange={(e) => setRefresh(Number(e.target.value))}>{REFRESH.map((s) => <option key={s} value={s}>{s ? `every ${s}s` : "paused"}</option>)}</select></label>
              </div>
            </div>
            <ErrorBox error={r.error} />
            <Results data={r.data} loading={r.busy} unit={/bytes/.test(metric) ? "bytes" : undefined} />
          </>}
        </div>
      </div>
    </>
  );
}

function LabelFilter({ metric, label, environments, connectors, namespace, value, onChange }: { metric: string; label: string; environments: string; connectors: string; namespace?: string; value: string[]; onChange: (v: string[]) => void }) {
  const v = useLoad(() => api<any>(`/metrics/labels${qs({ metric, environments, connectors, namespace, label })}`), [metric, label, environments, connectors, namespace]);
  return <MultiSelect label={label} allLabel="Any" options={(v.data?.values ?? []).map((x: string) => ({ value: x }))} value={value} onChange={onChange} />;
}

function QueryEditor({ environments, connectors, query, onQuery, canRaw }: { environments: string; connectors: string[]; query: string; onQuery: (q: string) => void; canRaw: boolean }) {
  const [form, setForm] = useState({ mode: "range", minutes: 60, step: "" });
  const [prompt, setPrompt] = useState("");
  const [draft, setDraft] = useState<any>(null);
  const [genError, setGenError] = useState<unknown>();
  const [generating, setGenerating] = useState(false);
  const [copied, setCopied] = useState(false);
  const [saveName, setSaveName] = useState("");
  const [shared, setShared] = useState(false);
  const r = useRunner<any>("/metrics/query");
  const ids = connectors.join(",");
  const templates = useLoad(() => api<any>(`/metrics/templates${qs({ environments, connectors: ids })}`), [environments, ids]);
  const saved = useLoad(() => api<any>("/metrics/saved"), []);
  const history = useLoad(() => api<any>("/metrics/history"), [r.data]);
  const catalog = useLoad(() => api<any>(`/metrics/catalog${qs({ environments, connectors: ids, limit: 5000 })}`), [environments, ids]);
  const token = /[a-zA-Z_:][a-zA-Z0-9_:]*$/.exec(query)?.[0] ?? "";
  const suggestions = token.length >= 2 ? (catalog.data?.items ?? []).filter((m: any) => m.name.startsWith(token) && m.name !== token).slice(0, 8) : [];
  async function generate() {
    setGenerating(true);
    setGenError(null);
    try {
      const d = await api<any>("/metrics/generate", { method: "POST", body: { prompt, environments, connectors } });
      setDraft(d);
      if (d.query) onQuery(d.query);
      setForm((x) => ({ ...x, minutes: WINDOWS.includes(d.suggested_minutes) ? d.suggested_minutes : x.minutes, step: d.suggested_step_seconds ? String(d.suggested_step_seconds) : x.step }));
    } catch (e) {
      setGenError(e);
    } finally {
      setGenerating(false);
    }
  }
  function run(e?: FormEvent) {
    e?.preventDefault();
    r.run({ query, mode: form.mode, environments, connectors, minutes: form.minutes, step: form.step ? Number(form.step) : undefined });
  }
  const areas = [...new Set<string>((templates.data?.templates ?? []).map((t: any) => t.area))];
  return (
    <>
      {!canRaw && <div className="alert info small" style={{ marginBottom: 12 }}>Writing PromQL needs metrics.raw_query for all namespaces of an environment, because raw queries cannot be confined to one namespace. Use Explore, or ask an administrator.</div>}
      <div className="card stack">
        <div className="filters">
          <label style={{ flex: 1 }}>Describe the query<input value={prompt} onChange={(e) => setPrompt(e.target.value)} placeholder="Show the top 10 pods by CPU usage over the last hour" /></label>
          <button type="button" className="ghost" disabled={generating || prompt.length < 5 || !canRaw} onClick={generate}>{generating ? "Generating…" : "Generate query"}</button>
        </div>
        <ErrorBox error={genError} />
        {draft && <div className="small stack">
          {draft.explanation && <div>{draft.explanation}</div>}
          {draft.assumptions.length > 0 && <div className="muted">Assumes: {draft.assumptions.join("; ")}</div>}
          {draft.cautions.map((c: string, i: number) => <div key={i} className="alert warn small">{c}</div>)}
          <div className="muted">Drafted by {draft.model}. Nothing has run: review it, then press Run.</div>
        </div>}
        <form className="stack" onSubmit={run}>
          <label>PromQL<textarea rows={4} className="mono" spellCheck={false} value={query} onChange={(e) => onQuery(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) run(); }} placeholder="sum by (pod) (rate(container_cpu_usage_seconds_total[5m]))" /></label>
          {suggestions.length > 0 && <div className="row small">{suggestions.map((m: any) => (
            <button type="button" key={m.name} className="ghost small mono" title={m.help} onClick={() => onQuery(query.slice(0, query.length - token.length) + m.name)}>{m.name}</button>))}</div>}
          <div className="filters">
            <label>Type<select value={form.mode} onChange={(e) => setForm({ ...form, mode: e.target.value })}><option value="range">Range</option><option value="instant">Instant</option></select></label>
            <label>Time<select value={form.minutes} onChange={(e) => setForm({ ...form, minutes: Number(e.target.value) })}>{WINDOWS.map((m) => <option key={m} value={m}>{windowLabel(m)}</option>)}</select></label>
            {form.mode === "range" && <label>Step (s)<input type="number" min={5} max={3600} value={form.step} placeholder="auto" onChange={(e) => setForm({ ...form, step: e.target.value })} /></label>}
            <button disabled={r.busy || !query.trim() || !canRaw}>{r.busy ? "Running…" : "Run"}</button>
            {r.busy && <button type="button" className="danger" onClick={r.cancel}>Cancel</button>}
            <button type="button" className="ghost" onClick={() => onQuery(query.replace(/[ \t]+/g, " ").replace(/\s*\n\s*/g, " ").trim())}>Format</button>
            <button type="button" className="ghost" onClick={async () => { await navigator.clipboard.writeText(query); setCopied(true); setTimeout(() => setCopied(false), 1500); }}>{copied ? "Copied" : "Copy"}</button>
            <button type="button" className="ghost" onClick={() => { onQuery(""); r.clear(); setDraft(null); }}>Clear</button>
          </div>
        </form>
      </div>
      <ErrorBox error={r.error} />
      <Results data={r.data} loading={r.busy} unit={/bytes/.test(query) ? "bytes" : undefined} />
      <div className="grid cols-2" style={{ marginTop: 14, alignItems: "start" }}>
        <div className="card stack">
          <h2>Templates</h2>
          {areas.map((a) => <div key={a}><h3>{a}</h3>
            {templates.data.templates.filter((t: any) => t.area === a).map((t: any) => (
              <div key={t.id} className="row between small">
                {t.runnable ? <button type="button" className="ghost small" onClick={() => onQuery(t.query)}>{t.title}</button> : <span className="muted">{t.title}</span>}
                {!t.runnable && <span className="muted" title={t.missing.join(", ")}>needs {t.missing[0]}{t.missing.length > 1 ? ` +${t.missing.length - 1}` : ""}</span>}
              </div>
            ))}</div>)}
        </div>
        <div className="stack">
          <div className="card stack">
            <h2>Saved queries</h2>
            {saved.data?.items.map((s: any) => (
              <div key={s.id} className="row between small">
                <button type="button" className="ghost small" onClick={() => onQuery(s.query)}>{s.name}</button>
                <span className="muted">{s.shared ? `shared by ${s.owner}` : "private"}</span>
                {s.mine && <button type="button" className="ghost small" onClick={async () => { await api(`/metrics/saved/${s.id}`, { method: "DELETE" }); saved.reload(); }}>Delete</button>}
              </div>
            ))}
            {saved.data && !saved.data.items.length && <div className="small muted">Nothing saved yet.</div>}
            <div className="filters">
              <label>Save as<input value={saveName} onChange={(e) => setSaveName(e.target.value)} /></label>
              <label className="inline"><input type="checkbox" checked={shared} onChange={(e) => setShared(e.target.checked)} /> share</label>
              <button type="button" className="ghost" disabled={!saveName || !query} onClick={async () => {
                await api("/metrics/saved", { method: "POST", body: { name: saveName, query, shared, environments: environments === "all" ? [] : environments.split(",") } });
                setSaveName(""); saved.reload();
              }}>Save</button>
            </div>
          </div>
          <div className="card stack">
            <h2>History</h2>
            {history.data?.items.slice(0, 10).map((h: any, i: number) => <button type="button" key={i} className="ghost small mono" style={{ textAlign: "left" }} title={new Date(h.at).toLocaleString()} onClick={() => onQuery(h.query)}>{h.query.slice(0, 90)}</button>)}
            {history.data && !history.data.items.length && <div className="small muted">Queries you run appear here.</div>}
          </div>
        </div>
      </div>
    </>
  );
}

function CloudWatch({ envs, namespaces }: { envs: any[]; namespaces: Record<string, string> }) {
  const [env, setEnv] = useState(envs[0]?.environment ?? "");
  const [resource, setResource] = useState(Object.keys(namespaces)[0] ?? "");
  const [ns, setNs] = useState("");
  const [pick, setPick] = useState<any>(null);
  const [stat, setStat] = useState("Average");
  const [minutes, setMinutes] = useState(60);
  const namespace = ns || namespaces[resource];
  const list = useLoad(() => (env ? api<any>(`/metrics/cloudwatch/metrics${qs({ environment: env, namespace })}`) : Promise.resolve(null)), [env, namespace]);
  const r = useRunner<any>("/metrics/cloudwatch/query");
  useEffect(() => { if (pick) r.run({ environment: env, namespace: pick.namespace, metric: pick.metric, dimensions: pick.dimensions, stat, minutes }); }, [pick, stat, minutes]); // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <>
      <div className="alert info small" style={{ marginBottom: 12 }}>CloudWatch has its own query model. These are CloudWatch metrics, not PromQL.</div>
      <div className="card filters">
        <label>Environment<select value={env} onChange={(e) => setEnv(e.target.value)}>{envs.map((e) => <option key={e.environment} value={e.environment}>{e.display_name}</option>)}</select></label>
        <label>Resource<select value={resource} onChange={(e) => { setResource(e.target.value); setNs(""); }}>{Object.keys(namespaces).map((k) => <option key={k}>{k}</option>)}</select></label>
        <label>Other namespace<input value={ns} onChange={(e) => setNs(e.target.value)} placeholder={namespace} /></label>
        <label>Statistic<select value={stat} onChange={(e) => setStat(e.target.value)}>{["Average", "Sum", "Maximum", "Minimum", "SampleCount", "p50", "p90", "p95", "p99"].map((s) => <option key={s}>{s}</option>)}</select></label>
        <label>Time<select value={minutes} onChange={(e) => setMinutes(Number(e.target.value))}>{WINDOWS.map((m) => <option key={m} value={m}>{windowLabel(m)}</option>)}</select></label>
      </div>
      <ErrorBox error={list.error ?? r.error} />
      <div className="grid metrics-layout" style={{ marginTop: 14 }}>
        <div className="card metric-list"><table><tbody>{list.data?.items.map((m: any, i: number) => (
          <tr key={i} className="clickable" {...rowAction(() => setPick(m))}><td className="mono small">{m.metric}<div className="muted">{Object.entries(m.dimensions).map(([k, v]) => `${k}=${v}`).join(" ")}</div></td></tr>
        ))}</tbody></table>{list.data && !list.data.items.length && <div className="empty">No metrics found.</div>}</div>
        <div>{pick ? <Results data={r.data} loading={r.busy} /> : <div className="card empty">Pick a metric.</div>}</div>
      </div>
    </>
  );
}
