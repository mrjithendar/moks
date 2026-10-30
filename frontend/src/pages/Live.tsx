import { useCallback, useEffect, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { api, qs } from "../api";
import { Badge, ErrorBox, LineChart, LogLines, PageHead, ScopePicker, SourceRef, Time, useLoad } from "../components";
import { EvidenceData } from "./Investigate";

interface Result<T = any> { summary: string; data: T; source: any; truncated: boolean; collected_at: string; duration_ms: number }

function useScope() {
  const [env, setEnv] = useState(localStorage.getItem("moks.env") ?? "");
  const [ns, setNs] = useState(localStorage.getItem("moks.ns") ?? "");
  const onEnv = useCallback((e: string) => { setEnv(e); localStorage.setItem("moks.env", e); }, []);
  const onNs = useCallback((n: string) => { setNs(n); localStorage.setItem("moks.ns", n); }, []);
  return { env, ns, onEnv, onNs };
}

const TABS = ["pods", "workloads", "services", "events", "ingresses", "nodes"] as const;

export function Explorer() {
  const scope = useScope();
  const [tab, setTab] = useState<(typeof TABS)[number]>("pods");
  const [pod, setPod] = useState<string | null>(null);
  const ready = scope.env && (tab === "nodes" || scope.ns);
  const r = useLoad<Result | null>(
    () => (ready ? api<Result>(`/explore/${scope.env}/${tab}${qs({ namespace: tab === "nodes" ? undefined : scope.ns, warnings_only: tab === "events" ? false : undefined })}`) : Promise.resolve(null)),
    [scope.env, scope.ns, tab],
  );
  return (
    <>
      <PageHead title="Kubernetes explorer" sub="Live, read-only view of resources in your scope">
        <ScopePicker {...scope} />
        <button className="ghost" onClick={r.reload} disabled={r.loading}>{r.loading ? "Loading…" : "Refresh"}</button>
      </PageHead>
      <div className="tabs">{TABS.map((t) => <button key={t} className={t === tab ? "active" : ""} onClick={() => setTab(t)}>{t}</button>)}</div>
      <ErrorBox error={r.error} />
      {r.data && (
        <div className="card">
          <div className="row between small muted" style={{ marginBottom: 8 }}>
            <span>{r.data.summary}{r.data.truncated && " (truncated)"}</span>
            <span>collected <Time iso={r.data.collected_at} /> · {r.data.duration_ms} ms</span>
          </div>
          {tab === "pods" && <PodTable pods={r.data.data} onOpen={setPod} />}
          {tab === "workloads" && <Generic rows={r.data.data} cols={["kind", "name", "ready", "desired", "revision", "images", "age"]} />}
          {tab === "services" && <ServiceTable rows={r.data.data} />}
          {tab === "events" && <Generic rows={r.data.data} cols={["type", "reason", "object", "message", "count", "lastSeen"]} />}
          {tab === "ingresses" && <Generic rows={r.data.data.flatMap((i: any) => i.rules.map((x: any) => ({ name: i.name, ...x })))} cols={["name", "host", "path", "service", "port"]} />}
          {tab === "nodes" && <Generic rows={r.data.data} cols={["name", "ready", "pressure", "unschedulable", "taints", "allocatable", "kubelet"]} />}
        </div>
      )}
      {pod && <PodDrawer env={scope.env} ns={scope.ns} pod={pod} onClose={() => setPod(null)} />}
    </>
  );
}

function PodTable({ pods, onOpen }: { pods: any[]; onOpen: (p: string) => void }) {
  if (!pods.length) return <div className="empty">No pods in this namespace.</div>;
  return (
    <table>
      <thead><tr><th>Pod</th><th>Phase</th><th>Ready</th><th>Restarts</th><th>Problems</th><th>Node</th><th>Age</th></tr></thead>
      <tbody>{pods.map((p) => (
        <tr key={p.name} className="clickable" onClick={() => onOpen(p.name)}>
          <td className="mono small">{p.name}</td>
          <td><Badge kind={p.phase === "Running" ? "ok" : p.phase === "Succeeded" ? "neutral" : "warn"}>{p.phase}</Badge></td>
          <td>{p.ready}</td>
          <td>{p.restarts > 0 ? <Badge kind="warn">{p.restarts}</Badge> : 0}</td>
          <td className="small">{p.problems.map((x: any) => `${x.reason ?? x.state}${x.exitCode != null ? ` (${x.exitCode})` : ""}`).join(", ")}</td>
          <td className="small muted">{p.node}</td><td className="small">{p.age}</td>
        </tr>
      ))}</tbody>
    </table>
  );
}

function ServiceTable({ rows }: { rows: any[] }) {
  return (
    <table>
      <thead><tr><th>Service</th><th>Selector</th><th>Ports</th><th>Ready endpoints</th><th>Matching pods</th></tr></thead>
      <tbody>{rows.map((s) => (
        <tr key={s.name}>
          <td className="mono small">{s.name}</td>
          <td className="mono small">{Object.entries(s.selector).map(([k, v]) => `${k}=${v}`).join(", ") || <span className="muted">none</span>}</td>
          <td className="small">{s.ports.map((p: any) => `${p.port}→${p.targetPort}`).join(", ")}</td>
          <td>{Object.keys(s.selector).length ? <Badge kind={s.endpoints.ready ? "ok" : "bad"}>{s.endpoints.ready}</Badge> : "—"}{s.endpoints.notReady > 0 && <Badge kind="warn">{s.endpoints.notReady} not ready</Badge>}</td>
          <td className="small">{s.matchingPodCount}{s.nearMissPods?.length > 0 && <div className="muted">near miss: {s.nearMissPods.map((n: any) => n.pod).join(", ")}</div>}</td>
        </tr>
      ))}</tbody>
    </table>
  );
}

function Generic({ rows, cols }: { rows: any[]; cols: string[] }) {
  if (!rows.length) return <div className="empty">Nothing here.</div>;
  const show = (v: any) => (v == null ? "—" : typeof v === "object" ? (Array.isArray(v) ? v.join(", ") : Object.entries(v).map(([k, x]) => `${k}=${x}`).join(" ")) : String(v));
  return (
    <table>
      <thead><tr>{cols.map((c) => <th key={c}>{c}</th>)}</tr></thead>
      <tbody>{rows.map((r, i) => <tr key={i}>{cols.map((c) => <td key={c} className="small">{show(r[c])}</td>)}</tr>)}</tbody>
    </table>
  );
}

function PodDrawer({ env, ns, pod, onClose }: { env: string; ns: string; pod: string; onClose: () => void }) {
  const [view, setView] = useState<"describe" | "logs" | "previous" | "resources">("describe");
  const r = useLoad<Result>(() => {
    if (view === "describe") return api(`/explore/${env}/pod${qs({ namespace: ns, pod })}`);
    if (view === "resources") return api(`/explore/${env}/resources${qs({ namespace: ns, pod_regex: pod.replace(/[.]/g, "\\."), minutes: 60 })}`);
    return api(`/explore/${env}/logs${qs({ namespace: ns, pod, previous: view === "previous", tail_lines: 300 })}`);
  }, [env, ns, pod, view]);
  return (
    <div className="drawer" role="dialog" aria-label={pod}>
      <div className="row between"><h2 className="mono">{pod}</h2><button className="ghost small" onClick={onClose}>Close</button></div>
      <div className="tabs">{(["describe", "logs", "previous", "resources"] as const).map((v) => (
        <button key={v} className={v === view ? "active" : ""} onClick={() => setView(v)}>{v === "previous" ? "previous logs" : v}</button>
      ))}</div>
      <ErrorBox error={r.error} />
      {r.loading && <div className="muted">Loading…</div>}
      {r.data && !r.loading && (
        <>
          <div className="small muted" style={{ marginBottom: 8 }}>{r.data.summary} · collected <Time iso={r.data.collected_at} /></div>
          <SourceRef source={r.data.source} />
          {view === "describe" ? <Describe d={r.data.data} /> : <EvidenceData tool={view === "resources" ? "prom_pod_resources" : "k8s_pod_logs"} data={r.data.data} />}
          <div style={{ marginTop: 12 }}><Link to={`/investigate?env=${env}&ns=${ns}&q=${encodeURIComponent(`Why is pod ${pod} unhealthy?`)}`}>Investigate this pod →</Link></div>
        </>
      )}
    </div>
  );
}

function Describe({ d }: { d: any }) {
  return (
    <div className="stack">
      {d.containers.map((c: any) => (
        <div key={c.name} className="card">
          <h2>{c.name}</h2>
          <dl className="kv small">
            <dt>Image</dt><dd className="mono">{c.image}</dd>
            <dt>State</dt><dd className="mono">{JSON.stringify(c.state)}</dd>
            {c.lastState && Object.keys(c.lastState).length > 0 && <><dt>Last state</dt><dd className="mono">{JSON.stringify(c.lastState)}</dd></>}
            <dt>Restarts</dt><dd>{c.restartCount}</dd>
            <dt>Resources</dt><dd className="mono">{JSON.stringify(c.resources)}</dd>
            <dt>Readiness</dt><dd className="mono">{c.readinessProbe ? JSON.stringify(c.readinessProbe) : "none"}</dd>
            <dt>Liveness</dt><dd className="mono">{c.livenessProbe ? JSON.stringify(c.livenessProbe) : "none"}</dd>
            <dt>Env</dt><dd className="mono">{c.env.map((e: any) => `${e.name}=${e.value ?? `<${e.from}>`}`).join("\n") || "—"}</dd>
          </dl>
        </div>
      ))}
      <h3>Conditions</h3>
      <Generic rows={d.conditions} cols={["type", "status", "reason", "message"]} />
      <h3>Events</h3>
      <Generic rows={d.events} cols={["type", "reason", "message", "count", "lastSeen"]} />
    </div>
  );
}

// ---- Metrics ----------------------------------------------------------------------------------

const EXAMPLES = [
  { label: "Scrape targets up", q: "up" },
  { label: "Pod memory (working set)", q: 'sum by (pod) (container_memory_working_set_bytes{namespace="$ns", container!=""})' },
  { label: "Pod CPU (cores)", q: 'sum by (pod) (rate(container_cpu_usage_seconds_total{namespace="$ns", container!=""}[5m]))' },
  { label: "HTTP 5xx ratio (roboshop)", q: 'sum by (service) (rate(roboshop_http_requests_total{status=~"5.."}[5m])) / sum by (service) (rate(roboshop_http_requests_total[5m]))' },
];

export function Metrics() {
  const scope = useScope();
  const [q, setQ] = useState("");
  const [minutes, setMinutes] = useState(60);
  const [res, setRes] = useState<Result | null>(null);
  const [error, setError] = useState<unknown>();
  const [busy, setBusy] = useState(false);
  const [alerts, setAlerts] = useState<Result | null>(null);
  useEffect(() => {
    if (scope.env) api<Result>(`/explore/${scope.env}/alerts${qs({ firing_only: false })}`).then(setAlerts, () => setAlerts(null));
  }, [scope.env]);

  async function run(e?: FormEvent) {
    e?.preventDefault();
    setBusy(true);
    setError(null);
    try {
      setRes(await api<Result>(`/explore/${scope.env}/promql${qs({ promql: q, mode: "range", minutes })}`));
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <PageHead title="Metrics" sub="Bounded PromQL. The exact query is always shown with its time range."><ScopePicker {...scope} /></PageHead>
      <form className="card stack" onSubmit={run}>
        <textarea rows={3} className="mono" value={q} onChange={(e) => setQ(e.target.value)} placeholder="PromQL, e.g. sum by (pod) (rate(container_cpu_usage_seconds_total[5m]))" />
        <div className="row">
          <select value={minutes} onChange={(e) => setMinutes(Number(e.target.value))}>
            {[15, 30, 60, 180, 360].map((m) => <option key={m} value={m}>last {m >= 60 ? `${m / 60} h` : `${m} min`}</option>)}
          </select>
          <button disabled={busy || !q || !scope.env}>{busy ? "Running…" : "Run query"}</button>
          <span className="small muted">Examples:</span>
          {EXAMPLES.map((x) => <button type="button" className="ghost small" key={x.label} onClick={() => setQ(x.q.replace("$ns", scope.ns || "default"))}>{x.label}</button>)}
        </div>
        <div className="small muted">Raw PromQL needs access to all namespaces in the environment; namespace-scoped users can use pod resource charts in the explorer.</div>
      </form>
      <ErrorBox error={error} />
      {res && (
        <div className="card" style={{ marginTop: 14 }}>
          <div className="row between small muted"><span>{res.summary}</span><span>{new Date(res.data.start).toLocaleString()} → {new Date(res.data.end).toLocaleString()} · step {res.data.step}s · collected <Time iso={res.collected_at} /></span></div>
          <SourceRef source={res.source} />
          {res.data.warnings?.length > 0 && <div className="alert warn small">{res.data.warnings.join("; ")}</div>}
          <LineChart series={res.data.series} unit={/bytes/.test(res.source?.query ?? "") ? "bytes" : undefined} />
        </div>
      )}
      {alerts && (
        <div className="card" style={{ marginTop: 14 }}>
          <h2>Alert rules</h2>
          <table>
            <thead><tr><th>Alert</th><th>State</th><th>Expression</th><th>Active</th></tr></thead>
            <tbody>{alerts.data.map((a: any) => (
              <tr key={a.group + a.name}>
                <td>{a.name}<div className="small muted">{a.annotations?.summary}</div></td>
                <td><Badge kind={a.state === "firing" ? "bad" : a.state === "pending" ? "warn" : "ok"}>{a.state}</Badge></td>
                <td><code className="small">{a.query}</code> <button className="ghost small" onClick={() => setQ(a.query)}>chart</button></td>
                <td className="small">{a.active.map((x: any) => Object.entries(x.labels).filter(([k]) => k !== "alertname").map(([k, v]) => `${k}=${v}`).join(" ")).join("; ")}</td>
              </tr>
            ))}</tbody>
          </table>
        </div>
      )}
    </>
  );
}

// ---- Logs -------------------------------------------------------------------------------------

export function Logs() {
  const scope = useScope();
  const [f, setF] = useState({ pod: "", contains: "", trace_id: "", request_id: "", errors_only: false, limit: 200, minutes: 60 });
  const [raw, setRaw] = useState("");
  const [useRaw, setUseRaw] = useState(false);
  const [res, setRes] = useState<Result | null>(null);
  const [error, setError] = useState<unknown>();
  const [busy, setBusy] = useState(false);

  async function run(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      setRes(useRaw
        ? await api<Result>(`/explore/${scope.env}/logql${qs({ logql: raw, limit: f.limit, minutes: f.minutes })}`)
        : await api<Result>(`/explore/${scope.env}/log-search${qs({ namespace: scope.ns, ...f })}`));
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }
  const set = (k: string, v: any) => setF((x) => ({ ...x, [k]: v }));
  return (
    <>
      <PageHead title="Logs" sub="Loki search by namespace, pod, text, trace ID or request ID"><ScopePicker {...scope} /></PageHead>
      <form className="card stack" onSubmit={run}>
        <label className="inline"><input type="checkbox" checked={useRaw} onChange={(e) => setUseRaw(e.target.checked)} /> Write LogQL</label>
        {useRaw ? (
          <textarea rows={2} className="mono" value={raw} onChange={(e) => setRaw(e.target.value)} placeholder={`{namespace="${scope.ns || "default"}"} |= "error"`} />
        ) : (
          <div className="row">
            <label>Pod (name or pattern)<input value={f.pod} onChange={(e) => set("pod", e.target.value)} placeholder="checkout-*" /></label>
            <label>Contains<input value={f.contains} onChange={(e) => set("contains", e.target.value)} placeholder="timeout" /></label>
            <label>Trace ID<input value={f.trace_id} onChange={(e) => set("trace_id", e.target.value)} className="mono" /></label>
            <label>Request ID<input value={f.request_id} onChange={(e) => set("request_id", e.target.value)} className="mono" /></label>
            <label className="inline"><input type="checkbox" checked={f.errors_only} onChange={(e) => set("errors_only", e.target.checked)} /> errors only</label>
          </div>
        )}
        <div className="row">
          <select value={f.minutes} onChange={(e) => set("minutes", Number(e.target.value))}>{[15, 60, 180, 360].map((m) => <option key={m} value={m}>last {m} min</option>)}</select>
          <select value={f.limit} onChange={(e) => set("limit", Number(e.target.value))}>{[50, 200, 500].map((m) => <option key={m} value={m}>{m} lines</option>)}</select>
          <button disabled={busy || !scope.env || (!useRaw && !scope.ns)}>{busy ? "Searching…" : "Search"}</button>
        </div>
      </form>
      <ErrorBox error={error} />
      {res && (
        <div className="card stack" style={{ marginTop: 14 }}>
          <div className="row between small muted"><span>{res.summary}</span><span>collected <Time iso={res.collected_at} /></span></div>
          <SourceRef source={res.source} />
          {res.data.lines ? <LogLines lines={res.data.lines} /> : <LineChart series={res.data.series ?? []} />}
        </div>
      )}
    </>
  );
}
