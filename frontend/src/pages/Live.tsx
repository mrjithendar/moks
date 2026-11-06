import { useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, qs } from "../api";
import { useAuth } from "../auth";
import { Badge, type Col, DataTable, EnvNsFilter, ErrorBox, PageHead, type Multi, type Scope, SourceRef, Time, useLoad, useScopes } from "../components";
import { type Chip, FilterPanel, useUrlFilters } from "../filters";
import { multiStates, StateView } from "../states";
import { ResourceTable } from "../table";
import { CopyButton, Drawer, Empty, Identifier, type MenuItem, Skeleton, Truncate } from "../ui";
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
    <Drawer title={m.title} onClose={onClose}>
      <p className="small muted">In {m.env}/{m.ns}. This creates an action plan: MOKS shows the impact and validation first, production plans need a second person's approval unless an administrator requests them, and nothing changes until it is executed.</p>
      <PlanForm env={m.env} ns={m.ns} initial={m.initial} onDone={(id) => nav(`/actions/${id}`)} />
    </Drawer>
  );
}

const EXPLORER = { tab: "pods", env: "all", ns: "all", q: "", only: false };

export function Explorer() {
  const scopes = useScopes();
  const [f, set, clear] = useUrlFilters(EXPLORER);
  const tab = (TABS as readonly string[]).includes(f.tab) ? f.tab as Tab : "pods";
  return (
    <>
      <PageHead title="Kubernetes explorer" sub="Live, read-only view of every environment and namespace in your scope" />
      <div className="tabs">{TABS.map((t) => <button key={t} className={t === tab ? "active" : ""} onClick={() => set({ tab: t, only: false })}>{t}</button>)}</div>
      <ErrorBox error={scopes.error} onRetry={scopes.reload} />
      <ExplorerTab key={tab} tab={tab} scopes={scopes.data} f={f} set={set} clear={clear} onShowPods={(name) => set({ q: name, tab: "pods" })} />
    </>
  );
}

function ExplorerTab({ tab, scopes, f, set, clear, onShowPods }: { tab: Tab; scopes?: Scope[]; f: typeof EXPLORER; set: (p: Partial<typeof EXPLORER>) => void; clear: () => void; onShowPods: (name: string) => void }) {
  const { can } = useAuth();
  const canManage = can("action.request");
  const nav = useNavigate();
  const [manage, setManage] = useState<Manage | null>(null);
  const [pod, setPod] = useState<any>(null);
  const r = useLoad(() => api<Multi>(`/multi/${tab}${qs({ environment: f.env, namespace: tab === "nodes" ? undefined : f.ns, warnings_only: tab === "events" ? false : undefined })}`), [tab, f.env, f.ns]);
  const rows = useMemo(() => {
    const items = r.data?.items ?? [];
    const flat = tab === "ingresses" ? items.flatMap((i: any) => i.rules.map((x: any) => ({ name: i.name, environment: i.environment, namespace: i.namespace, ...x }))) : items;
    return flat.filter((x: any) => has(x, f.q) && (!f.only || isProblem(tab, x)));
  }, [r.data, tab, f.q, f.only]);
  const problem = PROBLEM_LABEL[tab];
  const chips: Chip[] = [
    ...(f.env !== "all" ? [{ key: "e", label: f.env, onRemove: () => set({ env: "all", ns: "all" }) }] : []),
    ...(f.ns !== "all" && tab !== "nodes" ? [{ key: "n", label: `namespace ${f.ns}`, onRemove: () => set({ ns: "all" }) }] : []),
    ...(f.q ? [{ key: "q", label: `matches ${f.q}`, onRemove: () => set({ q: "" }) }] : []),
    ...(f.only && problem ? [{ key: "o", label: problem, onRemove: () => set({ only: false }) }] : []),
  ];
  const investigate = (env: string, ns: string, q: string) => nav(`/investigate?env=${env}&ns=${ns}&q=${encodeURIComponent(q)}`);
  return (
    <>
      <FilterPanel chips={chips} onClear={clear}
        search={<input type="search" className="grow" aria-label="Filter rows" value={f.q} onChange={(e) => set({ q: e.target.value })} placeholder="name, image, reason…" />}
        actions={<button type="button" className="ghost" onClick={r.reload} disabled={r.loading}>{r.loading ? "Loading…" : "Refresh"}</button>}
        advanced={<div className="filters">
          <EnvNsFilter scopes={scopes} env={f.env} ns={f.ns} onEnv={(env) => set({ env, ns: "all" })} onNs={(ns) => set({ ns })} noNamespace={tab === "nodes"} />
          {problem && <label className="inline"><input type="checkbox" checked={f.only} onChange={(e) => set({ only: e.target.checked })} /> {problem}</label>}
        </div>} />
      {!r.data && r.loading && <StateView kind="loading" title={`Loading ${tab}`} />}
      {multiStates({ data: r.data, error: r.error, reload: r.reload, what: tab })}
      {r.data && (
        <div className="card mt">
          <div className="row between small muted list-head">
            <span>{rows.length === r.data.items.length ? `${rows.length} ${tab}` : `${rows.length} of ${r.data.items.length} ${tab} shown`}{r.data.truncated?.length ? " (incomplete: see above)" : ""} across {plural(r.data.environments.length, "environment")}</span>
            <span>collected <Time iso={r.data.collected_at} /></span>
          </div>
          {!rows.length && (r.data.items.length
            ? <StateView kind="empty" title={`No ${tab} match the filters`} why={`${r.data.items.length} ${tab} were read, and the filters hide all of them.`} next={<button type="button" className="ghost small" onClick={clear}>Clear all filters</button>} />
            : <StateView kind="empty" title={`No ${tab} here`} why={f.ns !== "all" ? `Namespace ${f.ns} has no ${tab} right now.` : `None of the namespaces you can read has ${tab} right now.`} next="Choose another namespace or environment." />)}
          {rows.length > 0 && <>
          {tab === "pods" && <PodTable pods={rows} onOpen={setPod} onInvestigate={(p) => investigate(p.environment, p.namespace, `Why is pod ${p.name} unhealthy?`)} />}
          {tab === "workloads" && <WorkloadTable rows={rows} canManage={canManage} onManage={setManage} onShowPods={onShowPods} />}
          {tab === "services" && <ServiceTable rows={rows} canManage={canManage} onManage={setManage} />}
          {tab === "events" && <EventTable rows={rows} />}
          {tab === "ingresses" && <DataTable label="Ingresses" rows={rows} rowKey={(x: any) => `${x.environment}${x.namespace}${x.name}${x.host}${x.path}`} cols={[
            { key: "name", head: "Name", primary: true, cell: (x: any) => <Identifier name={x.name} /> },
            { key: "host", head: "Host", cell: (x: any) => <Truncate text={x.host ?? "—"} mono />, meta: (x: any) => `${x.host ?? ""}${x.path ?? ""}` },
            { key: "path", head: "Path", wide: true, cell: (x: any) => <span className="mono small">{x.path ?? "—"}</span> },
            { key: "backend", head: "Service", cell: (x: any) => <span className="mono small">{x.service}:{x.port}</span> },
            ...scopeCols<any>(),
          ]} />}
          {tab === "nodes" && <DataTable label="Nodes" rows={rows} rowKey={(x: any) => x.environment + x.name} cols={[
            { key: "name", head: "Name", primary: true, cell: (x: any) => <Identifier name={x.name} /> },
            { key: "ready", head: "Ready", status: true, cell: (x: any) => <Badge kind={x.ready && x.ready !== "False" ? "ok" : "bad"}>{String(x.ready)}</Badge> },
            { key: "env", head: "Environment", cell: (x: any) => x.environment },
            { key: "pressure", head: "Pressure", cell: (x: any) => show(x.pressure), meta: (x: any) => (x.pressure?.length ? show(x.pressure) : "") },
            { key: "unschedulable", head: "Unschedulable", cell: (x: any) => show(x.unschedulable), meta: (x: any) => (x.unschedulable ? "unschedulable" : "") },
            { key: "taints", head: "Taints", wide: true, cell: (x: any) => show(x.taints) },
            { key: "allocatable", head: "Allocatable", wide: true, cell: (x: any) => show(x.allocatable) },
            { key: "kubelet", head: "Kubelet", cell: (x: any) => show(x.kubelet) },
          ]} />}
          </>}
          {tab === "config-audit" && <ConfigAudit rows={rows} missing={r.data.missing_references ?? []} />}
        </div>
      )}
      {pod && <PodDrawer env={pod.environment} ns={pod.namespace} pod={pod.name} owner={pod.owner} canManage={canManage} onManage={(m) => { setPod(null); setManage(m); }} onClose={() => setPod(null)} />}
      {manage && <ManageDrawer m={manage} onClose={() => setManage(null)} />}
    </>
  );
}

function scopeCols<T extends { environment: string; namespace: string }>(): Col<T>[] {
  return [
    { key: "environment", head: "Environment", cell: (x) => x.environment },
    { key: "namespace", head: "Namespace", cell: (x) => <Truncate text={x.namespace} mono />, meta: (x) => x.namespace },
  ];
}

function WorkloadTable({ rows, canManage, onManage, onShowPods }: { rows: any[]; canManage: boolean; onManage: (m: Manage) => void; onShowPods: (name: string) => void }) {
  const nav = useNavigate();
  return (
    <DataTable label="Workloads" rows={rows} rowKey={(w) => w.environment + w.namespace + w.kind + w.name}
      empty={<Empty title="No workloads match." />}
      menu={(w) => {
        const items: MenuItem[] = [{ label: "Copy name", run: () => { navigator.clipboard?.writeText(w.name); } }, { label: "View pods", run: () => onShowPods(w.name) },
          { label: "Investigate", run: () => nav(`/investigate?env=${w.environment}&ns=${w.namespace}&q=${encodeURIComponent(`Why is ${w.kind} ${w.name} unhealthy?`)}`) }];
        if (canManage && w.kind === "Deployment") items.push(...deploymentActions(w).map((a) => ({ label: a.label, run: () => onManage({ title: `${a.label}: ${w.name}`, env: w.environment, ns: w.namespace, initial: { action: a.action, target_kind: "Deployment", target_name: w.name, params: a.params } }) })));
        return { label: `Actions for ${w.name}`, items };
      }}
      cols={[
        { key: "name", head: "Name", primary: true, cell: (w) => <Identifier name={w.name} /> },
        { key: "ready", head: "Ready", status: true, cell: (w) => <Badge kind={(w.ready ?? 0) >= (w.desired ?? 0) ? "ok" : "warn"}>{w.ready ?? 0}/{w.desired ?? 0}</Badge> },
        { key: "kind", head: "Kind", cell: (w) => w.kind },
        ...scopeCols<any>(),
        { key: "revision", head: "Revision", wide: true, cell: (w) => w.revision ?? "—" },
        { key: "images", head: "Images", wide: true, cell: (w) => <Truncate text={(w.images ?? []).join(", ")} mono /> },
        { key: "age", head: "Age", cell: (w) => w.age },
      ]} />
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

/** What a pod's state means at a glance: a waiting reason (CrashLoopBackOff) wins over the phase (Running). */
export function podStatus(p: any): { tone: "ok" | "warn" | "bad" | "neutral"; text: string } {
  const now = p.problems.find((x: any) => x.state === "waiting" || x.state === "terminated" || x.state === "unschedulable");
  if (now) return { tone: "bad", text: now.reason ?? now.state };
  const [up, of] = String(p.ready ?? "").split("/").map(Number);
  if (p.phase === "Running" && of && up < of) return { tone: "warn", text: `Not ready (${p.ready})` };
  if (p.phase === "Running") return { tone: "ok", text: "Running" };
  return { tone: p.phase === "Succeeded" ? "neutral" : "warn", text: p.phase };
}

const plural = (n: number, one: string) => `${n} ${one}${n === 1 ? "" : "s"}`;

function PodTable({ pods, onOpen, onInvestigate }: { pods: any[]; onOpen: (p: any) => void; onInvestigate: (p: any) => void }) {
  const problems = (p: any) => p.problems.map((x: any) => `${x.reason ?? x.state}${x.exitCode != null ? ` (${x.exitCode})` : ""}`).join(", ");
  return (
    <ResourceTable label="Pods" rows={pods} rowKey={(p) => p.environment + p.namespace + p.name} name={(p) => p.name} what="pod name"
      status={podStatus} action={{ label: "Details", run: onOpen }}
      menu={(p) => [
        { label: "Copy name", run: () => { navigator.clipboard?.writeText(p.name); } },
        { label: "Investigate", run: () => onInvestigate(p) },
      ]}
      fields={[
        { key: "ns", head: "Namespace", cell: (p) => <Truncate text={p.namespace} mono /> },
        { key: "ready", head: "Ready", cell: (p) => p.ready },
        { key: "restarts", head: "Restarts", cell: (p) => (p.restarts > 0 ? <Badge kind="warn">{plural(p.restarts, "restart")}</Badge> : "0") },
        { key: "problems", head: "Last problem", cell: (p) => <Truncate text={problems(p) || "—"} /> },
        { key: "env", head: "Environment", cell: (p) => p.environment },
        { key: "node", head: "Node", cell: (p) => <span className="small muted">{p.node}</span> },
        { key: "age", head: "Age", cell: (p) => p.age },
      ]} />
  );
}

function EventTable({ rows }: { rows: any[] }) {
  return (
    <DataTable label="Events" rows={rows} rowKey={(x) => `${x.environment}${x.namespace}${x.object}${x.reason}${x.lastSeen}`} empty={<Empty title="No events match." />} cols={[
      { key: "reason", head: "Reason", primary: true, cell: (x) => x.reason },
      { key: "type", head: "Type", status: true, cell: (x) => <Badge kind={x.type === "Warning" ? "warn" : "neutral"}>{x.type}</Badge> },
      { key: "object", head: "Object", cell: (x) => <Truncate text={show(x.object)} mono />, meta: (x) => show(x.object) },
      { key: "message", head: "Message", cell: (x) => <Truncate text={x.message ?? ""} lines={2} />, meta: (x) => <Truncate text={x.message ?? ""} lines={2} /> },
      ...scopeCols<any>().slice(1),
      { key: "count", head: "Count", wide: true, cell: (x) => x.count },
      { key: "last", head: "Last seen", cell: (x) => x.lastSeen },
    ]} />
  );
}

function ConfigAudit({ rows, missing }: { rows: any[]; missing: any[] }) {
  return (
    <div className="stack">
      {missing.length > 0 && <div className="alert error small">{missing.map((m, i) => <div key={i}><span className="mono">{m.environment}/{m.namespace}</span> {m.object}: missing {m.kind} {m.name}{m.key ? ` key ${m.key}` : ""}</div>)}</div>}
      <DataTable label="Configuration audit" rows={rows} rowKey={(w) => w.environment + w.namespace + w.workload} empty={<Empty title="No workloads match." />} cols={[
        { key: "workload", head: "Workload", primary: true, cell: (w) => <Identifier name={w.workload} /> },
        ...scopeCols<any>(),
        { key: "issues", head: "Issues", cell: (w) => <span className="small stack tight">{w.issues.map((i: any, n: number) => <span key={n}><Badge kind={i.severity === "high" ? "bad" : i.severity === "medium" ? "warn" : "neutral"}>{i.severity}</Badge> {i.detail}</span>)}{!w.issues.length && <span className="muted">no issues</span>}</span> },
        { key: "refs", head: "ConfigMaps / Secrets referenced", wide: true, cell: (w) => <span className="small mono">{[...(w.references?.configmaps ?? []), ...(w.references?.secrets ?? [])].join(", ") || "—"}</span> },
      ]} />
    </div>
  );
}

const show = (v: any) => (v == null ? "—" : typeof v === "object" ? (Array.isArray(v) ? v.join(", ") : Object.entries(v).map(([k, x]) => `${k}=${x}`).join(" ")) : String(v));

function ServiceTable({ rows, canManage, onManage }: { rows: any[]; canManage: boolean; onManage: (m: Manage) => void }) {
  const selector = (s: any) => Object.entries(s.selector).map(([k, v]) => `${k}=${v}`).join(", ");
  return (
    <DataTable label="Services" rows={rows} rowKey={(s) => s.environment + s.namespace + s.name} empty={<Empty title="No services match." />}
      menu={(s) => ({ label: `Actions for service ${s.name}`, items: [
        { label: "Copy name", run: () => { navigator.clipboard?.writeText(s.name); } },
        ...(canManage ? [
          { label: "Edit selector", run: () => onManage({ title: `Edit selector: ${s.name}`, env: s.environment, ns: s.namespace, initial: { action: "apply_config_patch", target_kind: "Service", target_name: s.name, params: { patch: { spec: { selector: s.selector } } } } }) },
          { label: "Session affinity", run: () => onManage({ title: `Session affinity: ${s.name}`, env: s.environment, ns: s.namespace, initial: { action: "apply_config_patch", target_kind: "Service", target_name: s.name, params: { patch: { spec: { sessionAffinity: "ClientIP" } } } } }) },
        ] : []),
      ] })}
      cols={[
        { key: "name", head: "Service", primary: true, cell: (s) => <Identifier name={s.name} /> },
        { key: "ready", head: "Ready endpoints", status: true, cell: (s) => <>{Object.keys(s.selector).length ? <Badge kind={s.endpoints.ready ? "ok" : "bad"}>{s.endpoints.ready}</Badge> : "—"}{s.endpoints.notReady > 0 && <Badge kind="warn">{s.endpoints.notReady} not ready</Badge>}</> },
        ...scopeCols<any>(),
        { key: "selector", head: "Selector", wide: true, cell: (s) => <Truncate text={selector(s) || "none"} mono /> },
        { key: "ports", head: "Ports", cell: (s) => s.ports.map((p: any) => `${p.port}→${p.targetPort}`).join(", ") },
        { key: "matching", head: "Matching pods", wide: true, cell: (s) => <>{s.matchingPodCount}{s.nearMissPods?.length > 0 && <div className="small muted">near miss: {s.nearMissPods.map((n: any) => n.pod).join(", ")}</div>}</> },
      ]} />
  );
}

function Generic({ rows, cols }: { rows: any[]; cols: string[] }) {
  if (!rows.length) return <Empty title="Nothing here." />;
  return (
    <div className="table-wrap" role="region" aria-label={cols.join(", ")} tabIndex={0}>
      <table>
        <thead><tr>{cols.map((c) => <th key={c} scope="col">{c}</th>)}</tr></thead>
        <tbody>{rows.map((r, i) => <tr key={i}>{cols.map((c) => <td key={c} className="small">{show(r[c])}</td>)}</tr>)}</tbody>
      </table>
    </div>
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
    <Drawer title={pod} heading={<span className="mono name-full">{pod}</span>} onClose={onClose}>
      <div className="row between small muted list-head"><span className="mono">{env}/{ns}</span><CopyButton text={pod} label="Copy name" /></div>
      {canManage && owner && <div className="row list-head">
        <button className="ghost small" onClick={() => onManage({ title: `Delete pod: ${pod}`, env, ns, initial: { action: "delete_pod", target_kind: "Pod", target_name: pod, params: {} } })}>Delete pod (replaced by {owner})</button>
        {deployment && <button className="ghost small" onClick={() => onManage({ title: `Rollout restart: ${deployment}`, env, ns, initial: { action: "restart_deployment", target_kind: "Deployment", target_name: deployment, params: {} } })}>Rollout restart {deployment}</button>}
      </div>}
      <div className="tabs">{(["describe", "logs", "previous", "resources"] as const).map((v) => (
        <button key={v} className={v === view ? "active" : ""} onClick={() => setView(v)}>{v === "previous" ? "previous logs" : v}</button>
      ))}</div>
      <ErrorBox error={r.error} />
      {r.loading && <Skeleton rows={6} />}
      {r.data && !r.loading && (
        <>
          <div className="small muted" style={{ marginBottom: 8 }}>{r.data.summary} · collected <Time iso={r.data.collected_at} /></div>
          <SourceRef source={r.data.source} />
          {view === "describe" ? <Describe d={r.data.data} /> : <EvidenceData tool={view === "resources" ? "prom_pod_resources" : "k8s_pod_logs"} data={r.data.data} />}
          <div style={{ marginTop: 12 }}><Link to={`/investigate?env=${env}&ns=${ns}&q=${encodeURIComponent(`Why is pod ${pod} unhealthy?`)}`}>Investigate this pod →</Link></div>
        </>
      )}
    </Drawer>
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
