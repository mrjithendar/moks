import { type FormEvent, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import { EnvNsFilter, ErrorBox, levelOf, LineChart, Loading, LogLines, type Multi, PageHead, PartialErrors, type Scope, Time, useInterval, useScopes, useLoad } from "../components";
import { type Chip, FilterPanel, useUrlFilters } from "../filters";
import { CopyButton, Empty, Icon, Skeleton, useIsMobile } from "../ui";

const LEVELS = ["error", "warn", "info", "debug"];
const WINDOWS = [15, 30, 60, 180, 360, 720, 1440];
export const windowLabel = (m: number) => `last ${m >= 60 ? `${m / 60} h` : `${m} min`}`;
const LIMITS = [50, 200, 500];

/** Survives opening another page in this tab, without search text reaching the URL. clearLocalWork removes it. */
function useSessionText(key: string): [string, (v: string) => void] {
  const [v, setV] = useState(() => sessionStorage.getItem(`moks.${key}`) ?? "");
  return [v, (next) => { setV(next); if (next) sessionStorage.setItem(`moks.${key}`, next); else sessionStorage.removeItem(`moks.${key}`); }];
}

export function Logs() {
  const [{ view }, setView] = useUrlFilters({ view: "search" });
  const scopes = useScopes();
  return (
    <>
      <PageHead title="Logs" sub="Search logs across your environments, or write LogQL with help from AI." />
      <div className="tabs" role="tablist">
        {[["search", "Search"], ["logql", "LogQL"]].map(([k, l]) => <button key={k} type="button" role="tab" aria-selected={view === k} className={view === k ? "active" : ""} onClick={() => setView({ view: k })}>{l}</button>)}
      </div>
      <ErrorBox error={scopes.error} onRetry={scopes.reload} />
      {view === "logql" ? <LogQLWorkspace scopes={scopes.data} /> : <Search scopes={scopes.data} />}
    </>
  );
}

const SEARCH = { environment: "all", namespace: "all", level: "", pod: "", container: "", minutes: 60, limit: 200 };

function Search({ scopes }: { scopes?: Scope[] }) {
  const mobile = useIsMobile();
  const [url, setUrl, clearUrl] = useUrlFilters(SEARCH);
  const [f, setF] = useState(url); // what the panel shows; the URL holds what was applied
  const [contains, setContains] = useSessionText("logs.contains");
  const [trace, setTrace] = useSessionText("logs.trace");
  const [request, setRequest] = useSessionText("logs.request");
  const [secret, setSecret] = useState({ contains, trace, request });
  const [tick, setTick] = useState(0);
  const [live, setLive] = useState(false);
  const r = useLoad(() => api<Multi>("/multi/logs", { method: "POST", body: { ...url, contains: secret.contains, trace_id: secret.trace, request_id: secret.request } }), [JSON.stringify(url), JSON.stringify(secret), tick]);
  useInterval(() => { if (!r.loading) r.reload(); }, 5000, live);
  useEffect(() => { setF(url); }, [url]);
  const apply = () => { setUrl(f); setSecret({ contains, trace, request }); setTick((t) => t + 1); };
  const pick = (patch: Partial<typeof f>) => { const n = { ...f, ...patch }; setF(n); if (!mobile) setUrl(n); }; // a phone applies once, from the sheet
  const typed = (k: keyof typeof f) => (e: { target: { value: string } }) => setF({ ...f, [k]: e.target.value });
  const clear = () => { clearUrl(); setF(SEARCH); setContains(""); setTrace(""); setRequest(""); setSecret({ contains: "", trace: "", request: "" }); };
  const counts = (key: (row: any) => string) => {
    const c: Record<string, number> = {};
    for (const row of r.data?.items ?? []) { const k = key(row); if (k) c[k] = (c[k] ?? 0) + 1; }
    return Object.entries(c).sort((a, b) => b[1] - a[1]);
  };
  const drop = (patch: Partial<typeof f>) => { setUrl(patch); setF({ ...f, ...patch }); };
  const chips: Chip[] = [
    ...(url.environment !== "all" ? [{ key: "env", label: url.environment, onRemove: () => drop({ environment: "all", namespace: "all" }) }] : []),
    ...(url.namespace !== "all" ? [{ key: "ns", label: `namespace ${url.namespace}`, onRemove: () => drop({ namespace: "all" }) }] : []),
    ...(url.level ? [{ key: "lvl", label: `level ${url.level}`, onRemove: () => drop({ level: "" }) }] : []),
    ...(url.pod ? [{ key: "pod", label: `pod ${url.pod}`, onRemove: () => drop({ pod: "" }) }] : []),
    ...(url.container ? [{ key: "ct", label: `container ${url.container}`, onRemove: () => drop({ container: "" }) }] : []),
    ...(secret.contains ? [{ key: "tx", label: `contains ${secret.contains}`, onRemove: () => { setContains(""); setSecret({ ...secret, contains: "" }); } }] : []),
    ...(secret.trace ? [{ key: "tr", label: "trace ID set", onRemove: () => { setTrace(""); setSecret({ ...secret, trace: "" }); } }] : []),
    ...(secret.request ? [{ key: "rq", label: "request ID set", onRemove: () => { setRequest(""); setSecret({ ...secret, request: "" }); } }] : []),
  ];
  const scope = `${f.environment === "all" ? "All environments" : f.environment}, ${f.namespace === "all" ? "all namespaces" : f.namespace}, ${windowLabel(f.minutes)}, up to ${f.limit} lines`;
  const envs = <>
    <EnvNsFilter scopes={scopes} env={f.environment} ns={f.namespace} onEnv={(e) => pick({ environment: e, namespace: "all" })} onNs={(n) => pick({ namespace: n })} />
    <label>Level<select aria-label="Log level" value={f.level} onChange={(e) => pick({ level: e.target.value })}><option value="">Any level</option>{LEVELS.map((l) => <option key={l}>{l}</option>)}</select></label>
    <label>Time<select value={f.minutes} onChange={(e) => pick({ minutes: Number(e.target.value) })}>{WINDOWS.map((m) => <option key={m} value={m}>{windowLabel(m)}</option>)}</select></label>
    <label>Lines<select value={f.limit} onChange={(e) => pick({ limit: Number(e.target.value) })}>{LIMITS.map((m) => <option key={m}>{m}</option>)}</select></label>
    <label>Contains<input value={contains} onChange={(e) => setContains(e.target.value)} placeholder="timeout" /></label>
  </>;
  const more = <div className="filters">
    <label>Pod<input value={f.pod} onChange={typed("pod")} placeholder="checkout-*" /></label>
    <label>Container<input value={f.container} onChange={typed("container")} /></label>
    <label>Trace ID<input className="mono" value={trace} onChange={(e) => setTrace(e.target.value)} /></label>
    <label>Request ID<input className="mono" value={request} onChange={(e) => setRequest(e.target.value)} /></label>
  </div>;
  return (
    <>
      <form onSubmit={(e) => { e.preventDefault(); apply(); }}>
        <FilterPanel chips={chips} onClear={clear} scope={scope} onApply={apply} applying={r.loading && !live} applyLabel="Search" advanced={more}
          actions={<label className="inline"><input type="checkbox" checked={live} onChange={(e) => setLive(e.target.checked)} /> Refresh every 5 s</label>}>{envs}</FilterPanel>
      </form>
      <Loading load={r} rows={8} />
      {r.data && (
        <div className="card stack mt">
          <div className="row between small muted">
            <span>{r.data.items.length} lines{r.data.items.length ? ` · ${counts((x) => x.environment).map(([k, n]) => `${k}: ${n}`).join(", ")}` : ""}{r.data.collected_at && <> · collected <Time iso={r.data.collected_at} /></>}</span>
            <span className="row">{counts(levelOf).map(([k, n]) => <button type="button" key={k} className="ghost small" onClick={() => drop({ level: k })}>{k} {n}</button>)}</span>
          </div>
          {r.data.items.length >= url.limit && <div className="small muted">Showing the first {url.limit} lines. Narrow the filters to see more.</div>}
          <PartialErrors errors={r.data.errors} />
          {r.data.items.length ? <LogLines lines={r.data.items} tall /> : <Empty title="No log lines match.">Widen the time range or clear a filter. No lines does not prove nothing happened if a source above failed.</Empty>}
          {Object.entries(r.data.queries).slice(0, 3).map(([k, q]) => <div key={k} className="small muted query-line"><span>{k}:</span> <code>{q}</code></div>)}
        </div>
      )}
    </>
  );
}

interface Sources { environments: { environment: string; display_name: string; restricted: boolean; max_minutes: number; data_sources: { id: string; name: string; last_test_ok: boolean | null }[] }[]; generation_available: boolean }
interface Draft { query: string; explanation: string; query_type: "log" | "metric"; required_labels: string[]; assumptions: string[]; suggested_minutes: number; warnings: string[]; syntax_checked: boolean; model: string; scope: { environment: string; connector: string; namespace: string | null } }
interface Run { summary: string; data: any; kind: "log" | "metric"; environment: string; connector: string; limit: number; limit_reached: boolean; truncated: boolean; collected_at: string; duration_ms: number; source: { query?: string } }

const LOGQL = { environment: "", connector: "", namespace: "", pod: "", container: "", minutes: 60, limit: 200 };

/** A cancellable POST: a second call replaces the first, and an abandoned answer is never shown. */
export function useRequest<T>() {
  const [busy, setBusy] = useState(false);
  const ctl = useRef<AbortController | null>(null);
  async function run(path: string, body: any): Promise<T | undefined> {
    ctl.current?.abort();
    const c = new AbortController();
    ctl.current = c;
    setBusy(true);
    try {
      return await api<T>(path, { method: "POST", body, signal: c.signal });
    } catch (e: any) {
      if (e?.name === "AbortError") return undefined;
      throw e;
    } finally {
      if (ctl.current === c) { ctl.current = null; setBusy(false); }
    }
  }
  const cancel = () => { ctl.current?.abort(); ctl.current = null; setBusy(false); };
  useEffect(() => () => ctl.current?.abort(), []);
  return { busy, run, cancel };
}

export function LogQLWorkspace({ scopes }: { scopes?: Scope[] }) {
  const sources = useLoad(() => api<Sources>("/logs/sources"), []);
  const [f, set] = useUrlFilters(LOGQL);
  const [query, setQueryText] = useSessionText("logql");
  const [previous, setPrevious] = useState<string | null>(null);
  const [prompt, setPrompt] = useState("");
  const [draft, setDraft] = useState<Draft | null>(null);
  const [genError, setGenError] = useState<unknown>();
  const [result, setResult] = useState<Run | null>(null);
  const [runError, setRunError] = useState<unknown>();
  const gen = useRequest<Draft>();
  const exec = useRequest<Run>();
  const envs = sources.data?.environments ?? [];
  const env = envs.find((e) => e.environment === f.environment) ?? envs[0];
  const target = env?.environment ?? "";
  const data = env?.data_sources ?? [];
  const connector = data.some((d) => d.id === f.connector) ? f.connector : data[0]?.id ?? "";
  const namespaces = useMemo(() => scopes?.find((s) => s.environment === target)?.namespaces ?? [], [scopes, target]);
  const needsNamespace = !!env?.restricted && !f.namespace;
  const noLoki = !!env && !data.length;

  function setQuery(q: string) { setQueryText(q); setDraft((d) => (d && d.query !== q ? { ...d, query: "" } : d)); }
  async function generate(e: FormEvent) {
    e.preventDefault();
    if (gen.busy) return;
    setGenError(null);
    try {
      const d = await gen.run("/logs/generate", { environment: target, connector: connector || undefined, namespace: f.namespace || undefined, pod: f.pod || undefined, container: f.container || undefined, minutes: f.minutes, prompt });
      if (!d) return;
      setDraft(d);
      if (d.query) {
        if (query && query !== d.query) setPrevious(query);
        setQueryText(d.query);
        set({ minutes: d.suggested_minutes });
      }
    } catch (err) { setGenError(err); }
  }
  async function run(e?: FormEvent) {
    e?.preventDefault();
    if (exec.busy || !query.trim()) return;
    setRunError(null);
    try {
      const r = await exec.run("/logs/query", { environment: target, connector: connector || undefined, logql: query, minutes: f.minutes, limit: f.limit });
      if (r) setResult(r);
    } catch (err) { setRunError(err); }
  }
  const unavailable = sources.data && !sources.data.generation_available;
  const chips: Chip[] = [
    ...(target ? [{ key: "env", label: env?.display_name ?? target }] : []),
    ...(data.length > 1 ? [{ key: "ds", label: data.find((d) => d.id === connector)?.name ?? "" }] : []),
    ...(f.namespace ? [{ key: "ns", label: `namespace ${f.namespace}`, onRemove: () => set({ namespace: "" }) }] : []),
    ...(f.pod ? [{ key: "pod", label: `pod ${f.pod}`, onRemove: () => set({ pod: "" }) }] : []),
    ...(f.container ? [{ key: "ct", label: `container ${f.container}`, onRemove: () => set({ container: "" }) }] : []),
    { key: "win", label: windowLabel(f.minutes) },
  ];
  return (
    <>
      <Loading load={sources} />
      <FilterPanel label="Scope" chips={chips} onClear={() => set({ namespace: "", pod: "", container: "", minutes: 60, limit: 200 })} scope={`Queries ${env?.display_name ?? "…"}, ${f.namespace || "all namespaces"}, ${windowLabel(f.minutes)}, up to ${f.limit} lines`}
        advanced={<div className="filters">
          <label>Pod<input value={f.pod} onChange={(e) => set({ pod: e.target.value })} placeholder="checkout-*" /></label>
          <label>Container<input value={f.container} onChange={(e) => set({ container: e.target.value })} /></label>
          <label>Lines<select value={f.limit} onChange={(e) => set({ limit: Number(e.target.value) })}>{LIMITS.map((m) => <option key={m}>{m}</option>)}</select></label>
        </div>}>
          <label>Environment<select value={target} onChange={(e) => set({ environment: e.target.value, connector: "", namespace: "" })}>{envs.map((e) => <option key={e.environment} value={e.environment}>{e.display_name}</option>)}</select></label>
          <label>Loki data source<select value={connector} onChange={(e) => set({ connector: e.target.value })} disabled={!data.length}>{data.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}</select></label>
          <label>Namespace<select value={f.namespace} onChange={(e) => set({ namespace: e.target.value })}>{!env?.restricted && <option value="">All namespaces</option>}{env?.restricted && <option value="">Choose a namespace</option>}{namespaces.map((n) => <option key={n}>{n}</option>)}</select></label>
          <label>Time<select value={f.minutes} onChange={(e) => set({ minutes: Number(e.target.value) })}>{[...new Set([...WINDOWS, f.minutes])].sort((a, b) => a - b).map((m) => <option key={m} value={m}>{windowLabel(m)}</option>)}</select></label>
      </FilterPanel>
      <div className="card stack mt">
        {noLoki && <div className="alert warn small">{env.display_name} has no Loki data source. Add one under Connectors.</div>}
        {env?.restricted && <div className="small muted">Your access covers some namespaces here, so every query must name one with an exact match.</div>}
        <details className="ai-box" open>
          <summary><Icon name="spark2" size={16} /> Generate LogQL with AI</summary>
          <form className="stack" onSubmit={generate}>
            <label>Describe what you want<textarea rows={2} maxLength={1000} value={prompt} onChange={(e) => setPrompt(e.target.value)} placeholder="Find checkout errors from the last 30 minutes"
              onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) (e.currentTarget.form as HTMLFormElement).requestSubmit(); }} /></label>
            <div className="row">
              <button className="ghost" disabled={gen.busy || prompt.trim().length < 5 || noLoki || needsNamespace || !!unavailable}>{gen.busy ? "Generating…" : "Generate"}</button>
              {gen.busy && <button type="button" className="ghost" onClick={gen.cancel}>Cancel</button>}
              <span className="small muted">{unavailable ? "No active chat model. An administrator can add one under LLM providers." : needsNamespace ? "Choose a namespace first." : "MOKS sends your request and the label names it found, never log lines. Nothing runs until you press Run."}</span>
            </div>
          </form>
          <ErrorBox error={genError} />
          {draft && <div className="draft small stack" role="status">
            {draft.explanation && <div>{draft.explanation}</div>}
            {(draft.required_labels.length > 0 || draft.assumptions.length > 0) && <div className="muted">{draft.required_labels.length > 0 && <>Needs labels <span className="mono">{draft.required_labels.join(", ")}</span>. </>}{draft.assumptions.join(" ")}</div>}
            {draft.warnings.map((w, i) => <div key={i} className="alert warn small">{w}</div>)}
            {draft.query && <div className="muted">{draft.query_type === "metric" ? "Metric query" : "Log query"}, {windowLabel(draft.suggested_minutes)}. Drafted by {draft.model}{draft.syntax_checked ? ", syntax checked by Loki" : ""}. Nothing has run: review it, then press Run.</div>}
          </div>}
        </details>
        <form className="stack" onSubmit={run}>
          <label>LogQL<textarea className="mono" rows={3} spellCheck={false} maxLength={2000} value={query} onChange={(e) => setQuery(e.target.value)} placeholder='{namespace="default"} |= "error"'
            onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) run(); }} /></label>
          <div className="row">
            <button disabled={exec.busy || !query.trim() || noLoki}>{exec.busy ? "Running…" : "Run"}</button>
            {exec.busy && <button type="button" className="ghost" onClick={exec.cancel}>Cancel</button>}
            <CopyButton text={query} className="ghost" />
            <button type="button" className="ghost" onClick={() => { setQueryText(""); setResult(null); setDraft(null); setRunError(null); setPrevious(null); }}>Clear</button>
            {previous !== null && <button type="button" className="ghost" onClick={() => { setQueryText(previous); setPrevious(null); }}>Restore previous query</button>}
          </div>
        </form>
      </div>
      <ErrorBox error={runError} />
      {exec.busy && !result && <div className="card mt"><Skeleton rows={6} label="Running query" /></div>}
      {result && <LogQLResult r={result} />}
    </>
  );
}

function LogQLResult({ r }: { r: Run }) {
  const lines: any[] | undefined = r.data.lines;
  const series: any[] = r.data.series ?? [];
  const total: number = r.data.total_series ?? series.length;
  return (
    <div className="card stack mt">
      <div className="row between">
        <h2>{r.kind === "metric" ? "Metric result" : "Log result"}</h2>
        <span className="small muted">{r.connector} · {r.environment} · collected <Time iso={r.collected_at} /> · {r.duration_ms} ms</span>
      </div>
      <div className="small muted">{new Date(r.data.start).toLocaleString()} to {new Date(r.data.end).toLocaleString()}</div>
      {r.limit_reached && <div className="alert warn small">Stopped at the limit of {r.limit} lines, so older lines are not shown.</div>}
      {r.truncated && <div className="alert warn small">The result was cut to the size limit, so it is incomplete.</div>}
      {total > series.length && <div className="alert warn small">Showing {series.length} of {total} series.</div>}
      {r.kind === "log"
        ? (lines?.length ? <LogLines lines={lines} label="Log result lines" tall /> : <Empty title="The query ran and returned no lines.">Check the labels and the time range.</Empty>)
        : (series.length ? <><LineChart series={series} /><MetricTable series={series} /></> : <Empty title="The query ran and returned no series.">This does not show the value is zero.</Empty>)}
    </div>
  );
}

function MetricTable({ series }: { series: any[] }) {
  return (
    <div className="table-wrap" role="region" aria-label="Metric series" tabIndex={0}>
      <table>
        <thead><tr><th scope="col">Series</th><th scope="col">Latest value</th></tr></thead>
        <tbody>{series.map((s, i) => <tr key={i}><td className="mono small">{Object.entries(s.metric ?? {}).map(([k, v]) => `${k}=${v}`).join(" ") || "(no labels)"}</td><td>{s.values?.[s.values.length - 1]?.[1] ?? "—"}</td></tr>)}</tbody>
      </table>
    </div>
  );
}

