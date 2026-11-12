import { useState, type ReactNode } from "react";
import { api } from "../api";
import { useAuth } from "../auth";
import { Badge, type Col, DataTable, ErrorBox, fmtBytes, Loading, PageHead, Time, useInterval, useLoad, type Env } from "../components";
import { Drawer, Empty, Truncate } from "../ui";

/** PostgreSQL and Redis/Valkey clusters the engineer may read. Every view is one read-only tool call, checked and audited by the backend. */

type Tone = "ok" | "warn" | "bad" | "info" | "neutral";
export const TONE: Record<string, Tone> = { bad: "bad", warn: "warn", info: "info" };
export const VIEWS: Record<string, [string, string][]> = {
  postgres: [["overview", "Overview"], ["sessions", "Sessions"], ["locks", "Locks"], ["replication", "Replication"], ["maintenance", "Vacuum"], ["queries", "Top queries"]],
  redis: [["overview", "Overview"], ["slowlog", "Slowlog"], ["clients", "Clients"], ["keyspace", "Big keys"]],
};

/** A cluster's worst state, for its badge: unreachable or any bad finding is bad, then warn, then ok. */
export function health(c: any): Tone {
  if (c.reachable === false) return "bad";
  const sev = (c.findings ?? []).map((f: any) => f.severity);
  return sev.includes("bad") ? "bad" : sev.includes("warn") ? "warn" : c.findings ? "ok" : "neutral";
}

const secs = (s?: number | null) => (s == null ? "—" : s < 120 ? `${Math.round(s)} s` : s < 7200 ? `${Math.round(s / 60)} min` : `${(s / 3600).toFixed(1)} h`);
const bytes = (b?: number | null) => (b == null ? "—" : fmtBytes(b));
const pct = (v?: number | null) => (v == null ? "—" : `${v}%`);

function Findings({ items }: { items?: any[] }) {
  if (!items?.length) return <div className="small muted">No findings.</div>;
  return <ul className="stack small" style={{ margin: 0, paddingLeft: 16 }}>{items.map((f) => <li key={f.code}><Badge kind={TONE[f.severity] ?? "neutral"}>{f.severity}</Badge> {f.text}</li>)}</ul>;
}

function Table({ label, rows, cols }: { label: string; rows: any[]; cols: [string, (r: any) => ReactNode][] }) {
  const c: Col<any>[] = cols.map(([head, cell], i) => ({ key: String(i), head, cell, primary: i === 0 }));
  return <DataTable label={label} rows={rows ?? []} rowKey={(r) => JSON.stringify(r).slice(0, 200)} cols={c} empty={<div className="small muted">None.</div>} />;
}

function PgView({ view, d }: { view: string; d: any }) {
  if (view === "overview") return (
    <div className="stack">
      <dl className="kv">
        <dt>Role</dt><dd>{d.server?.role} · {String(d.server?.version ?? "").split(" on ")[0]}</dd>
        <dt>Connections</dt><dd>{d.connections?.total} of {d.connections?.max} ({pct(d.connections?.used_pct)}), {d.idle_in_transaction} idle in transaction</dd>
        <dt>Blocked sessions</dt><dd>{d.blocked_sessions}</dd>
        <dt>Cache hit ratio</dt><dd>{d.cache_hit_ratio == null ? "—" : `${(d.cache_hit_ratio * 100).toFixed(1)}%`}</dd>
        <dt>Oldest xid age</dt><dd>{d.oldest_xid_age?.toLocaleString()}</dd>
      </dl>
      <Findings items={d.findings} />
      <h3>Connections by client</h3>
      <Table label="Connections by client" rows={d.connections?.by_source} cols={[["Application", (r) => r.application || "—"], ["User", (r) => r.user], ["Database", (r) => r.database], ["Connections", (r) => r.n], ["Idle in tx", (r) => r.idle_in_transaction]]} />
    </div>
  );
  if (view === "sessions") return <Table label="Sessions" rows={d} cols={[["PID", (r) => r.pid], ["State", (r) => r.state], ["Application", (r) => r.application || r.user], ["Transaction", (r) => secs(r.xact_age_s)], ["Waiting on", (r) => r.wait_event_type ? `${r.wait_event_type}/${r.wait_event}` : "—"], ["Query", (r) => <Truncate text={r.query ?? ""} mono lines={2} />]]} />;
  if (view === "locks") return (
    <div className="stack">
      {!d.root_blockers?.length && <Empty title="No lock waits." />}
      {d.root_blockers?.map((b: any) => (
        <div key={b.blocker.pid} className="card stack">
          <strong>pid {b.blocker.pid} holds up {b.waiting} session(s)</strong>
          <div className="small">{b.blocker.application || b.blocker.user} · {b.blocker.state} · transaction open {secs(b.blocker.xact_age_s)}</div>
          {b.blocker.query && <code className="small">{b.blocker.query}</code>}
          <div className="small muted">Waiting: {b.waiting_pids.join(", ")}</div>
        </div>
      ))}
    </div>
  );
  if (view === "replication") return d.role === "standby" ? (
    <dl className="kv"><dt>Role</dt><dd>standby</dd><dt>Receiver</dt><dd>{d.standby?.receiver_status ?? "not running"}</dd><dt>Last replay</dt><dd>{secs(d.standby?.replay_delay_s)} ago</dd><dt>Apply backlog</dt><dd>{bytes(d.standby?.apply_backlog_bytes)}</dd></dl>
  ) : (
    <div className="stack">
      <h3>Replicas</h3>
      <Table label="Replicas" rows={d.replicas} cols={[["Replica", (r) => r.application_name || r.client], ["State", (r) => `${r.state} (${r.sync_state})`], ["Replay lag", (r) => `${secs(r.replay_lag_s)} · ${bytes(r.replay_lag_bytes)}`]]} />
      <h3>Slots</h3>
      <Table label="Replication slots" rows={d.slots} cols={[["Slot", (r) => r.slot_name], ["Active", (r) => <Badge kind={r.active ? "ok" : "warn"}>{r.active ? "active" : "inactive"}</Badge>], ["WAL retained", (r) => bytes(r.retained_bytes)], ["Status", (r) => r.wal_status ?? "—"]]} />
    </div>
  );
  if (view === "maintenance") return (
    <div className="stack">
      <Table label="Databases" rows={d.databases} cols={[["Database", (r) => r.database], ["xid age", (r) => r.xid_age?.toLocaleString()], ["Size", (r) => bytes(r.size_bytes)], ["Deadlocks", (r) => r.deadlocks ?? "—"]]} />
      <h3>Tables with most dead rows</h3>
      <Table label="Tables" rows={d.tables} cols={[["Table", (r) => `${r.schema}.${r.table}`], ["Dead rows", (r) => `${r.dead_rows?.toLocaleString()} (${r.dead_pct}%)`], ["Last autovacuum", (r) => <Time iso={r.last_autovacuum} />], ["Size", (r) => bytes(r.total_bytes)]]} />
    </div>
  );
  if (view === "queries") return d.available === false ? <Empty title="pg_stat_statements is not installed.">{d.note}</Empty> : (
    <Table label="Top statements" rows={d.statements} cols={[["Query", (r) => <Truncate text={r.query ?? ""} mono lines={2} />], ["Calls", (r) => r.calls?.toLocaleString()], ["Total", (r) => `${r.total_ms?.toLocaleString()} ms`], ["Mean", (r) => `${r.mean_ms} ms`]]} />
  );
  return <pre className="small">{JSON.stringify(d, null, 2)}</pre>;
}

function RedisView({ view, d }: { view: string; d: any }) {
  if (view === "overview") return (
    <div className="stack">
      {d.cluster && <div className="small">cluster_state <Badge kind={d.cluster.cluster_state === "ok" ? "ok" : "bad"}>{d.cluster.cluster_state}</Badge> · {d.cluster.cluster_slots_ok} of 16384 slots ok</div>}
      <Findings items={d.findings} />
      <Table label="Nodes" rows={d.nodes} cols={[["Node", (r) => `${r.node} (${r.role ?? "?"})`], ["Memory", (r) => r.reachable ? `${bytes(r.memory?.used_bytes)} / ${r.memory?.max_bytes ? bytes(r.memory.max_bytes) : "no limit"} (${pct(r.memory?.used_pct)})` : <Badge kind="bad">unreachable</Badge>],
        ["Clients", (r) => r.clients ? `${r.clients.connected} / ${r.clients.maxclients}` : "—"], ["Hit ratio", (r) => r.stats?.hit_ratio == null ? "—" : `${(r.stats.hit_ratio * 100).toFixed(1)}%`],
        ["Evicted", (r) => r.stats?.evicted_keys_total ?? "—"], ["Persistence", (r) => r.persistence?.rdb_last_bgsave_status ?? "—"]]} />
    </div>
  );
  if (view === "slowlog") return <Table label="Slowlog" rows={d.slowlog} cols={[["Command", (r) => <span className="mono small">{r.command}</span>], ["Duration", (r) => `${(r.duration_us / 1000).toFixed(1)} ms`], ["Client", (r) => r.client_name || r.client], ["At", (r) => <Time iso={new Date(r.at * 1000).toISOString()} />]]} />;
  if (view === "clients") return (
    <div className="stack">
      <div className="small">{d.total} connections, {d.blocked} blocked</div>
      <Table label="Clients by source" rows={d.by_source} cols={[["Source", (r) => r.source], ["Connections", (r) => r.connections]]} />
    </div>
  );
  if (view === "keyspace") return (
    <div className="stack">
      <div className="small muted">{d.note} {d.names_hashed && "Key names are hashed."}</div>
      <Table label="Prefixes" rows={d.prefixes} cols={[["Prefix", (r) => r.prefix], ["Keys sampled", (r) => r.keys], ["Bytes", (r) => bytes(r.bytes)], ["Largest", (r) => bytes(r.largest)]]} />
      <h3>Largest keys</h3>
      <Table label="Largest keys" rows={d.top} cols={[["Key", (r) => <span className="mono small">{r.key}</span>], ["Bytes", (r) => bytes(r.bytes)]]} />
    </div>
  );
  return <pre className="small">{JSON.stringify(d, null, 2)}</pre>;
}

function ClusterDrawer({ env, c, onClose }: { env: string; c: any; onClose: () => void }) {
  const { can } = useAuth();
  const [view, setView] = useState("overview");
  const r = useLoad(() => api<any>(`/datastores/${env}/${c.kind}/${c.name}/${view}`), [env, c.kind, c.name, view]);
  const q = encodeURIComponent(`Investigate ${c.kind === "postgres" ? "PostgreSQL" : "Redis"} cluster ${c.name}: ${(c.findings ?? []).map((f: any) => f.text).join(" ") || "is it healthy?"}`);
  return (
    <Drawer title={c.name} heading={<span className="small muted">{c.kind} · {c.namespace ?? "no namespace"} · <span className="mono">{c.host}:{c.port}</span></span>} onClose={onClose}>
      <div className="row wrap" role="tablist" aria-label="Views">
        {VIEWS[c.kind].map(([k, label]) => <button key={k} role="tab" aria-selected={view === k} className={view === k ? "small" : "ghost small"} onClick={() => setView(k)}>{label}</button>)}
      </div>
      <Loading load={r} rows={6} />
      {r.data && <div className="stack">
        <div className="small muted">{r.data.summary} · read <Time iso={r.data.collected_at} />{r.data.truncated && " · truncated"}</div>
        {c.kind === "postgres" ? <PgView view={view} d={r.data.data} /> : <RedisView view={view} d={r.data.data} />}
      </div>}
      {can("investigation.run") && <a className="btn ghost" href={`${import.meta.env.BASE_URL}investigate?env=${env}&ns=${c.namespace ?? "*"}&q=${q}`}>Investigate with MOKS</a>}
    </Drawer>
  );
}

export function DataStores() {
  const envs = useLoad(() => api<Env[]>("/environments"), []);
  const [picked, setPicked] = useState("");
  const env = picked || envs.data?.[0]?.name || "";
  const list = useLoad(() => (env ? api<any>(`/datastores/${env}`) : Promise.resolve(null)), [env]);
  const [open, setOpen] = useState<any>(null);
  useInterval(() => { if (!list.loading) list.reload(); }, 30000);
  const rows: any[] = list.data?.data ?? [];
  return (
    <>
      <PageHead title="Databases & caches" sub="PostgreSQL and Redis/Valkey clusters you may read, from their own statistics. Read-only: credentials and key values never leave the connector.">
        <label>Environment<select value={env} onChange={(e) => setPicked(e.target.value)}>{envs.data?.map((e) => <option key={e.name} value={e.name}>{e.display_name || e.name}</option>)}</select></label>
      </PageHead>
      <ErrorBox error={envs.error} />
      <div className="card">
        <Loading load={list} rows={4} />
        {list.data && <DataTable label="Clusters" rows={rows} rowKey={(c) => c.name} onRow={(c) => c.kind && setOpen(c)}
          empty={<Empty title="No clusters here.">An administrator adds PostgreSQL and Redis clusters under Connectors (Discover databases finds them).</Empty>}
          cols={[
            { key: "n", head: "Cluster", primary: true, cell: (c) => <strong>{c.name}</strong> },
            { key: "h", head: "Health", status: true, cell: (c) => <Badge kind={health(c)}>{c.reachable === false ? "unreachable" : health(c) === "ok" ? "healthy" : health(c) === "neutral" ? "unknown" : `${(c.findings ?? []).filter((f: any) => f.severity !== "info").length} finding(s)`}</Badge> },
            { key: "k", head: "Kind", cell: (c) => c.kind === "postgres" ? "PostgreSQL" : c.kind === "redis" ? `Redis (${c.mode ?? "standalone"})` : "—" },
            { key: "ns", head: "Namespace", cell: (c) => <span className="mono small">{c.namespace ?? "—"}</span> },
            { key: "f", head: "Findings", wide: true, cell: (c) => c.error ? <span className="small muted">{c.error}</span> : <Findings items={(c.findings ?? []).filter((f: any) => f.severity !== "info")} /> },
          ]} />}
      </div>
      {open && <ClusterDrawer env={env} c={open} onClose={() => setOpen(null)} />}
    </>
  );
}
