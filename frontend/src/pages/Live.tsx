import { useMemo, useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, qs } from "../api";
import { useAuth } from "../auth";
import { Badge, EnvNsFilter, ErrorBox, LineChart, LogLines, PageHead, PartialErrors, type Multi, type Scope, SourceRef, Time, useInterval, useLoad, useScopes } from "../components";
import { EvidenceData } from "./Investigate";
import { PlanForm } from "./Ops";

interface Result<T = any> { summary: string; data: T; source: any; truncated: boolean; collected_at: string; duration_ms: number }

const has = (row: unknown, text: string) => !text || JSON.stringify(row).toLowerCase().includes(text.toLowerCase());

// ---- Kubernetes explorer ------------------------------------------------------------------------

const TABS = ["pods", "workloads", "services", "events", "ingresses", "nodes", "config-audit"] as const;
type Tab = (typeof TABS)[number];
const PROBLEM_LABEL: Partial<Record<Tab, string>> = { pods: "problems only", events: "warnings only", services: "no endpoints", "config-audit": "high severity only", nodes: "not ready" };

interface Manage { title: string; env: string; ns: string; initial: any }

/** What can be proposed for a Deployment. Each option opens a reviewable action plan. */
function deploymentActions(w: any): { label: string; action: string; params: any }[] {
  return [
    { label: "Rollout restart", action: "restart_deployment", params: {} },
    { label: "Scale", action: "scale_deployment", params: { replicas: w.desired ?? 1 } },
    { label: "Roll back", action: "rollback_deployment", params: { to_revision: Math.max(1, Number(w.revision ?? 2) - 1) } },
    { label: "Change resources", action: "patch_resources", params: { container: "", requests: { cpu: "100m", memory: "128Mi" }, limits: { memory: "256Mi" } } },
    { label: "Tune rollout settings", action: "apply_config_patch", params: { patch: { spec: { minReadySeconds: 0, revisionHistoryLimit: 10 } } } },
  ];
}

function ManageDrawer({ m, onClose }: { m: Manage; onClose: () => void }) {
  const nav = useNavigate();
  return (
    <div className="drawer" role="dialog" aria-label={m.title}>
      <div className="row between"><h2>{m.title}</h2><button className="ghost small" onClick={onClose}>Close</button></div>
      <p className="small muted">In {m.env}/{m.ns}. This creates an action plan: MOKS shows the impact and validation first, production plans need a second person's approval, and nothing changes until it is executed.</p>
      <PlanForm env={m.env} ns={m.ns} initial={m.initial} onDone={(id) => nav(`/actions/${id}`)} />
    </div>
  );
}

export function Explorer() {
  const scopes = useScopes();
  const [tab, setTab] = useState<Tab>("pods");
  const [filter, setFilter] = useState("");
  const [env, setEnv] = useState("all");
  const [ns, setNs] = useState("all");
  return (
    <>
      <PageHead title="Kubernetes explorer" sub="Live, read-only view of every environment and namespace in your scope" />
      <div className="tabs">{TABS.map((t) => <button key={t} className={t === tab ? "active" : ""} onClick={() => setTab(t)}>{t}</button>)}</div>
      <ErrorBox error={scopes.error} />
      <ExplorerTab key={tab} tab={tab} scopes={scopes.data} env={env} ns={ns} onEnv={setEnv} onNs={setNs} initialText={filter} onShowPods={(name) => { setFilter(name); setTab("pods"); }} />
    </>
  );
}

function ExplorerTab({ tab, scopes, env, ns, onEnv, onNs, initialText, onShowPods }: { tab: Tab; scopes?: Scope[]; env: string; ns: string; onEnv: (e: string) => void; onNs: (n: string) => void; initialText: string; onShowPods: (name: string) => void }) {
  const { can } = useAuth();
  const canManage = can("action.request");
  const [manage, setManage] = useState<Manage | null>(null);
  const [text, setText] = useState(tab === "pods" ? initialText : "");
  const [only, setOnly] = useState(false);
  const [pod, setPod] = useState<any>(null);
  const r = useLoad(() => api<Multi>(`/multi/${tab}${qs({ environment: env, namespace: tab === "nodes" ? undefined : ns, warnings_only: tab === "events" ? false : undefined })}`), [tab, env, ns]);
  const rows = useMemo(() => {
    const items = r.data?.items ?? [];
    const flat = tab === "ingresses" ? items.flatMap((i: any) => i.rules.map((x: any) => ({ name: i.name, environment: i.environment, namespace: i.namespace, ...x }))) : items;
    return flat.filter((x: any) => has(x, text) && (!only || isProblem(tab, x)));
  }, [r.data, tab, text, only]);
  const scopeCols = ["environment", "namespace"];
  return (
    <>
      <form className="card filters" onSubmit={(e) => e.preventDefault()}>
        <EnvNsFilter scopes={scopes} env={env} ns={ns} onEnv={onEnv} onNs={onNs} noNamespace={tab === "nodes"} />
        <label>Filter<input value={text} onChange={(e) => setText(e.target.value)} placeholder="name, image, reason…" /></label>
        {PROBLEM_LABEL[tab] && <label className="inline"><input type="checkbox" checked={only} onChange={(e) => setOnly(e.target.checked)} /> {PROBLEM_LABEL[tab]}</label>}
        <button type="button" className="ghost" onClick={r.reload} disabled={r.loading}>{r.loading ? "Loading…" : "Refresh"}</button>
      </form>
      <ErrorBox error={r.error} />
      {r.data && (
        <div className="card" style={{ marginTop: 14 }}>
          <div className="row between small muted" style={{ marginBottom: 8 }}>
            <span>{rows.length} of {tab === "ingresses" ? "all" : r.data.items.length} {tab} across {r.data.environments.length} environment(s)</span>
            <span>collected <Time iso={r.data.collected_at} /></span>
          </div>
          <PartialErrors errors={r.data.errors} />
          <div className="table-wrap">
            {tab === "pods" && <PodTable pods={rows} onOpen={setPod} />}
            {tab === "workloads" && <WorkloadTable rows={rows} canManage={canManage} onManage={setManage} onShowPods={onShowPods} />}
            {tab === "services" && <ServiceTable rows={rows} canManage={canManage} onManage={setManage} />}
            {tab === "events" && <Generic rows={rows} cols={[...scopeCols, "type", "reason", "object", "message", "count", "lastSeen"]} />}
            {tab === "ingresses" && <Generic rows={rows} cols={[...scopeCols, "name", "host", "path", "service", "port"]} />}
            {tab === "nodes" && <Generic rows={rows} cols={["environment", "name", "ready", "pressure", "unschedulable", "taints", "allocatable", "kubelet"]} />}
            {tab === "config-audit" && <ConfigAudit rows={rows} missing={r.data.missing_references ?? []} />}
          </div>
        </div>
      )}
      {pod && <PodDrawer env={pod.environment} ns={pod.namespace} pod={pod.name} owner={pod.owner} canManage={canManage} onManage={(m) => { setPod(null); setManage(m); }} onClose={() => setPod(null)} />}
      {manage && <ManageDrawer m={manage} onClose={() => setManage(null)} />}
    </>
  );
}

function ManageMenu({ label, options, onPick }: { label: string; options: { label: string; run: () => void }[]; onPick?: () => void }) {
  return (
    <select aria-label={`Manage ${label}`} className="manage" value="" onClick={(e) => e.stopPropagation()}
      onChange={(e) => { options.find((o) => o.label === e.target.value)?.run(); onPick?.(); }}>
      <option value="">Manage…</option>
      {options.map((o) => <option key={o.label}>{o.label}</option>)}
    </select>
  );
}

function WorkloadTable({ rows, canManage, onManage, onShowPods }: { rows: any[]; canManage: boolean; onManage: (m: Manage) => void; onShowPods: (name: string) => void }) {
  const nav = useNavigate();
  if (!rows.length) return <div className="empty">Nothing here.</div>;
  return (
    <table>
      <thead><tr><th>Environment</th><th>Namespace</th><th>Kind</th><th>Name</th><th>Ready</th><th>Revision</th><th>Images</th><th>Age</th><th /></tr></thead>
      <tbody>{rows.map((w) => {
        const ask = () => nav(`/investigate?env=${w.environment}&ns=${w.namespace}&q=${encodeURIComponent(`Why is ${w.kind} ${w.name} unhealthy?`)}`);
        const options = [{ label: "View pods", run: () => onShowPods(w.name) }, { label: "Investigate", run: ask }];
        if (canManage && w.kind === "Deployment") {
          options.push(...deploymentActions(w).map((a) => ({ label: a.label, run: () => onManage({ title: `${a.label}: ${w.name}`, env: w.environment, ns: w.namespace, initial: { action: a.action, target_kind: "Deployment", target_name: w.name, params: a.params } }) })));
        }
        return (
          <tr key={w.environment + w.namespace + w.kind + w.name}>
            <td className="small">{w.environment}</td><td className="mono small">{w.namespace}</td><td className="small">{w.kind}</td><td className="mono small">{w.name}</td>
            <td><Badge kind={(w.ready ?? 0) >= (w.desired ?? 0) ? "ok" : "warn"}>{w.ready ?? 0}/{w.desired ?? 0}</Badge></td>
            <td className="small">{w.revision ?? "—"}</td><td className="small mono">{(w.images ?? []).join(", ")}</td><td className="small">{w.age}</td>
            <td><ManageMenu label={w.name} options={options} /></td>
          </tr>
        );
      })}</tbody>
    </table>
  );
}

function isProblem(tab: Tab, x: any): boolean {
  if (tab === "pods") return x.problems?.length > 0 || x.restarts > 0;
  if (tab === "events") return x.type === "Warning";
  if (tab === "services") return Object.keys(x.selector ?? {}).length > 0 && !x.endpoints?.ready;
  if (tab === "config-audit") return x.issues?.some((i: any) => i.severity === "high");
  if (tab === "nodes") return !x.ready || x.ready === "False";
  return true;
}

function PodTable({ pods, onOpen }: { pods: any[]; onOpen: (p: any) => void }) {
  if (!pods.length) return <div className="empty">No pods match.</div>;
  return (
    <table>
      <thead><tr><th>Environment</th><th>Namespace</th><th>Pod</th><th>Phase</th><th>Ready</th><th>Restarts</th><th>Problems</th><th>Node</th><th>Age</th></tr></thead>
      <tbody>{pods.map((p) => (
        <tr key={p.environment + p.namespace + p.name} className="clickable" onClick={() => onOpen(p)}>
          <td className="small">{p.environment}</td><td className="mono small">{p.namespace}</td>
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

function ConfigAudit({ rows, missing }: { rows: any[]; missing: any[] }) {
  return (
    <div className="stack">
      {missing.length > 0 && <div className="alert error small">{missing.map((m, i) => <div key={i}><span className="mono">{m.environment}/{m.namespace}</span> {m.object}: missing {m.kind} {m.name}{m.key ? ` key ${m.key}` : ""}</div>)}</div>}
      {!rows.length ? <div className="empty">No workloads match.</div> : (
        <table>
          <thead><tr><th>Environment</th><th>Namespace</th><th>Workload</th><th>Issues</th><th>ConfigMaps / Secrets referenced</th></tr></thead>
          <tbody>{rows.map((w) => (
            <tr key={w.environment + w.namespace + w.workload}>
              <td className="small">{w.environment}</td><td className="mono small">{w.namespace}</td><td className="mono small">{w.workload}</td>
              <td className="small">{w.issues.map((i: any, n: number) => <div key={n}><Badge kind={i.severity === "high" ? "bad" : i.severity === "medium" ? "warn" : "neutral"}>{i.severity}</Badge> {i.detail}</div>)}{!w.issues.length && <span className="muted">none</span>}</td>
              <td className="small mono">{[...(w.references?.configmaps ?? []), ...(w.references?.secrets ?? [])].join(", ") || "—"}</td>
            </tr>
          ))}</tbody>
        </table>
      )}
    </div>
  );
}

function ServiceTable({ rows, canManage, onManage }: { rows: any[]; canManage: boolean; onManage: (m: Manage) => void }) {
  return (
    <table>
      <thead><tr><th>Environment</th><th>Namespace</th><th>Service</th><th>Selector</th><th>Ports</th><th>Ready endpoints</th><th>Matching pods</th><th /></tr></thead>
      <tbody>{rows.map((s) => (
        <tr key={s.environment + s.namespace + s.name}>
          <td className="small">{s.environment}</td><td className="mono small">{s.namespace}</td>
          <td className="mono small">{s.name}</td>
          <td className="mono small">{Object.entries(s.selector).map(([k, v]) => `${k}=${v}`).join(", ") || <span className="muted">none</span>}</td>
          <td className="small">{s.ports.map((p: any) => `${p.port}→${p.targetPort}`).join(", ")}</td>
          <td>{Object.keys(s.selector).length ? <Badge kind={s.endpoints.ready ? "ok" : "bad"}>{s.endpoints.ready}</Badge> : "—"}{s.endpoints.notReady > 0 && <Badge kind="warn">{s.endpoints.notReady} not ready</Badge>}</td>
          <td className="small">{s.matchingPodCount}{s.nearMissPods?.length > 0 && <div className="muted">near miss: {s.nearMissPods.map((n: any) => n.pod).join(", ")}</div>}</td>
          <td>{canManage && <ManageMenu label={s.name} options={[{ label: "Edit selector", run: () => onManage({ title: `Edit selector: ${s.name}`, env: s.environment, ns: s.namespace,
            initial: { action: "apply_config_patch", target_kind: "Service", target_name: s.name, params: { patch: { spec: { selector: s.selector } } } } }) },
            { label: "Session affinity", run: () => onManage({ title: `Session affinity: ${s.name}`, env: s.environment, ns: s.namespace,
              initial: { action: "apply_config_patch", target_kind: "Service", target_name: s.name, params: { patch: { spec: { sessionAffinity: "ClientIP" } } } } }) }]} />}</td>
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

function PodDrawer({ env, ns, pod, owner, canManage, onManage, onClose }: { env: string; ns: string; pod: string; owner?: string | null; canManage: boolean; onManage: (m: Manage) => void; onClose: () => void }) {
  const deployment = owner?.startsWith("ReplicaSet/") ? owner.slice("ReplicaSet/".length).replace(/-[^-]+$/, "") : null;
  const [view, setView] = useState<"describe" | "logs" | "previous" | "resources">("describe");
  const r = useLoad<Result>(() => {
    if (view === "describe") return api(`/explore/${env}/pod${qs({ namespace: ns, pod })}`);
    if (view === "resources") return api(`/explore/${env}/resources${qs({ namespace: ns, pod_regex: pod.replace(/[.]/g, "\\."), minutes: 60 })}`);
    return api(`/explore/${env}/logs${qs({ namespace: ns, pod, previous: view === "previous", tail_lines: 300 })}`);
  }, [env, ns, pod, view]);
  return (
    <div className="drawer" role="dialog" aria-label={pod}>
      <div className="row between"><h2 className="mono">{pod}</h2><button className="ghost small" onClick={onClose}>Close</button></div>
      {canManage && owner && <div className="row" style={{ marginBottom: 10 }}>
        <button className="ghost small" onClick={() => onManage({ title: `Delete pod: ${pod}`, env, ns, initial: { action: "delete_pod", target_kind: "Pod", target_name: pod, params: {} } })}>Delete pod (replaced by {owner})</button>
        {deployment && <button className="ghost small" onClick={() => onManage({ title: `Rollout restart: ${deployment}`, env, ns, initial: { action: "restart_deployment", target_kind: "Deployment", target_name: deployment, params: {} } })}>Rollout restart {deployment}</button>}
      </div>}
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

const windowLabel = (m: number) => `last ${m >= 60 ? `${m / 60} h` : `${m} min`}`;

// ---- Logs -------------------------------------------------------------------------------------

const LEVELS = ["error", "warn", "info", "debug"];

function levelOf(row: any): string {
  const v = (row.labels?.level ?? /\blevel\W{0,3}(\w+)/i.exec(row.line)?.[1] ?? "").toLowerCase();
  if (/err|fatal|crit|panic/.test(v)) return "error";
  if (/warn/.test(v)) return "warn";
  return /info|debug|trace/.test(v) ? v : "";
}

export function Logs() {
  const scopes = useScopes();
  const blank = { environment: "all", namespace: "all", pod: "", container: "", level: "", contains: "", trace_id: "", request_id: "", minutes: 60, limit: 200 };
  const [f, setF] = useState(blank);
  const [applied, setApplied] = useState(blank);
  const [live, setLive] = useState(false);
  const [open, setOpen] = useState<number | null>(null);
  const r = useLoad(() => api<Multi>(`/multi/logs${qs(applied)}`), [applied]);
  useInterval(() => { if (!r.loading) r.reload(); }, 5000, live);
  const apply = (patch: Partial<typeof f>) => { const n = { ...f, ...patch }; setF(n); setApplied(n); setOpen(null); };
  const set = (k: keyof typeof f) => (e: { target: { value: string } }) => setF({ ...f, [k]: e.target.value });
  const counts = (key: (row: any) => string) => {
    const c: Record<string, number> = {};
    for (const row of r.data?.items ?? []) { const k = key(row); if (k) c[k] = (c[k] ?? 0) + 1; }
    return Object.entries(c).sort((a, b) => b[1] - a[1]);
  };
  return (
    <>
      <PageHead title="Logs" sub="Logs from every environment and namespace, newest first. Narrow them with the filters.">
        <label className="inline"><input type="checkbox" checked={live} onChange={(e) => setLive(e.target.checked)} /> Refresh every 5 s</label>
      </PageHead>
      <ErrorBox error={scopes.error} />
      <form className="card filters" onSubmit={(e) => { e.preventDefault(); apply({}); }}>
        <EnvNsFilter scopes={scopes.data} env={f.environment} ns={f.namespace} onEnv={(e) => apply({ environment: e, namespace: "all" })} onNs={(n) => apply({ namespace: n })} />
        <label>Level
          <select value={f.level} onChange={(e) => apply({ level: e.target.value })}><option value="">Any level</option>{LEVELS.map((l) => <option key={l}>{l}</option>)}</select>
        </label>
        <label>Pod<input value={f.pod} onChange={set("pod")} placeholder="checkout-*" /></label>
        <label>Container<input value={f.container} onChange={set("container")} /></label>
        <label>Contains<input value={f.contains} onChange={set("contains")} placeholder="timeout" /></label>
        <label>Trace ID<input className="mono" value={f.trace_id} onChange={set("trace_id")} /></label>
        <label>Request ID<input className="mono" value={f.request_id} onChange={set("request_id")} /></label>
        <label>Time<select value={f.minutes} onChange={(e) => apply({ minutes: Number(e.target.value) })}>{[15, 60, 180, 360, 1440].map((m) => <option key={m} value={m}>{windowLabel(m)}</option>)}</select></label>
        <label>Lines<select value={f.limit} onChange={(e) => apply({ limit: Number(e.target.value) })}>{[50, 200, 500].map((m) => <option key={m}>{m}</option>)}</select></label>
        <button disabled={r.loading}>{r.loading && !live ? "Searching…" : "Search"}</button>
        <button type="button" className="ghost" onClick={() => { setF(blank); setApplied(blank); }}>Clear</button>
      </form>
      <ErrorBox error={r.error} />
      {r.data && (
        <div className="card stack" style={{ marginTop: 14 }}>
          <div className="row between small muted">
            <span>{r.data.items.length} lines · {counts((x) => x.environment).map(([k, n]) => `${k}: ${n}`).join(", ")}</span>
            <span className="row">{counts(levelOf).map(([k, n]) => <button type="button" key={k} className="ghost small" onClick={() => apply({ level: k })}>{k} {n}</button>)}</span>
          </div>
          <PartialErrors errors={r.data.errors} />
          {!r.data.items.length ? <div className="empty">No log lines match.</div> : (
            <div className="logbox" style={{ maxHeight: 620 }}>
              {r.data.items.map((l: any, i: number) => {
                const lvl = levelOf(l);
                const where = [l.environment, l.labels?.namespace, l.labels?.pod].filter(Boolean).join("/");
                return (
                  <div key={i} className={`line clickable ${lvl === "error" ? "err" : ""} ${open === i ? "open" : ""}`} onClick={() => setOpen(open === i ? null : i)}>
                    {l.ts && <span className="ts">{new Date(l.ts).toLocaleTimeString()}</span>}
                    <span className={`lvl ${lvl}`}>{lvl}</span><span className="ts">{where}</span>{l.line}
                    {open === i && <div className="small muted" style={{ marginTop: 4 }}>{l.ts && <div>{new Date(l.ts).toLocaleString()}</div>}{Object.entries(l.labels ?? {}).map(([k, v]) => <div key={k}>{k}={String(v)}</div>)}</div>}
                  </div>
                );
              })}
            </div>
          )}
          {Object.entries(r.data.queries).slice(0, 3).map(([k, q]) => <div key={k} className="small muted">{k}: <code>{q}</code></div>)}
        </div>
      )}
      <RawLogQL scopes={scopes.data} />
    </>
  );
}

function RawLogQL({ scopes }: { scopes?: Scope[] }) {
  const [env, setEnv] = useState("");
  const [q, setQ] = useState("");
  const [res, setRes] = useState<Result | null>(null);
  const [error, setError] = useState<unknown>();
  const target = env || scopes?.[0]?.environment || "";
  async function run(e: FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      setRes(await api<Result>(`/explore/${target}/logql${qs({ logql: q, limit: 200, minutes: 60 })}`));
    } catch (err) {
      setError(err);
    }
  }
  return (
    <details className="card block" style={{ marginTop: 14 }}>
      <summary>Write LogQL</summary>
      <form className="stack" style={{ marginTop: 10 }} onSubmit={run}>
        <textarea rows={2} className="mono" value={q} onChange={(e) => setQ(e.target.value)} placeholder='{namespace="default"} |= "error"' />
        <div className="filters">
          <label>Environment<select value={target} onChange={(e) => setEnv(e.target.value)}>{scopes?.map((s) => <option key={s.environment} value={s.environment}>{s.display_name}</option>)}</select></label>
          <button disabled={!q || !target}>Run</button>
        </div>
      </form>
      <ErrorBox error={error} />
      {res && <div className="stack" style={{ marginTop: 10 }}>
        <SourceRef source={res.source} />
        {res.data.lines ? <LogLines lines={res.data.lines} /> : <LineChart series={res.data.series ?? []} />}
      </div>}
    </details>
  );
}
