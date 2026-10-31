import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { useAuth } from "../auth";
import { Badge, DataTable, ErrorBox, Loading, PageHead, Stat, StatusBadge, Time, useInterval, useLoad } from "../components";
import { Empty, Identifier, Skeleton, Truncate } from "../ui";

const REFRESH_MS = 5000;

interface Conn { id: string; type: string; name: string; enabled: boolean; last_test_ok: boolean | null; last_test_at: string | null; last_test_message: string | null }
interface EnvOverview { name: string; display_name: string; kind: string; is_production: boolean; active_investigations: number; connectors: Conn[]; grafana_url?: string | null }

export default function Overview() {
  const { me, can } = useAuth();
  const ov = useLoad(() => api<{ environments: EnvOverview[] }>("/overview"), []);
  const recent = useLoad(() => api<any[]>("/investigations?limit=8"), []);
  const backups = useLoad(() => (can("audit.read") ? api<any>("/admin/backups").catch(() => null) : Promise.resolve(null)), []);
  const [live, setLive] = useState(true);
  useInterval(() => {
    if (!ov.loading) ov.reload();
    if (!recent.loading) recent.reload();
  }, REFRESH_MS, live);
  return (
    <>
      <PageHead title="Overview" sub="Live environment health, connector status and recent investigations">
        <label className="inline"><input type="checkbox" checked={live} onChange={(e) => setLive(e.target.checked)} /> Refresh every 5 s</label>
      </PageHead>
      <Loading load={ov} rows={4} />
      {backups.data?.overdue && (
        <div className="alert error" style={{ marginBottom: 14 }}>
          {backups.data.last_success_at ? <>The last good database backup finished <Time iso={backups.data.last_success_at} />.</> : "No database backup has ever succeeded."}
          {" "}Data written since then would be lost in a disaster.{backups.data.runs[0]?.error && ` Latest error: ${backups.data.runs[0].error}`}
        </div>
      )}
      {me && !me.roles.length && (
        <div className="alert info" style={{ marginBottom: 14 }}>
          You are signed in as {me.email} but have no roles yet. Ask a MOKS administrator to grant access.
        </div>
      )}
      <div className="grid cols-2">
        {ov.data?.environments.map((e) => <EnvCard key={e.name} env={e} live={live} />)}
        {ov.data && !ov.data.environments.length && <div className="card"><Empty title="No environments are visible to you.">Ask an administrator for a role in one.</Empty></div>}
      </div>
      <div className="card" style={{ marginTop: 14 }}>
        <div className="row between"><h2>Recent investigations</h2><Link to="/investigations">All investigations →</Link></div>
        {recent.loading && !recent.data && <Skeleton rows={4} />}
        {recent.data && <DataTable label="Recent investigations" rows={recent.data} rowKey={(i) => i.id}
          empty={<Empty title="No investigations yet."><Link to="/investigate">Start one.</Link></Empty>}
          cols={[
            { key: "q", head: "Question", primary: true, cell: (i) => <Link className="clamp-2" title={i.question} to={`/investigations/${i.id}`}>{i.question}</Link> },
            { key: "status", head: "Status", status: true, cell: (i) => <StatusBadge status={i.status} /> },
            { key: "scope", head: "Scope", cell: (i) => <Truncate text={`${i.environment}/${i.namespace}`} mono />, meta: (i) => `${i.environment}/${i.namespace}` },
            { key: "at", head: "Started", cell: (i) => <Time iso={i.created_at} /> },
          ]} />}
      </div>
    </>
  );
}

function EnvCard({ env, live }: { env: EnvOverview; live: boolean }) {
  const [health, setHealth] = useState<any>();
  const [error, setError] = useState<unknown>();
  const inFlight = useRef(false);

  async function refresh() {
    if (inFlight.current) return; // a slow cluster must not stack requests
    inFlight.current = true;
    try {
      setHealth(await api(`/overview/${env.name}/health`));
      setError(null);
    } catch (e) {
      setError(e);
    } finally {
      inFlight.current = false;
    }
  }
  useEffect(() => { refresh(); }, [env.name]); // eslint-disable-line react-hooks/exhaustive-deps
  useInterval(refresh, REFRESH_MS, live);

  const firing = health?.alerts?.reduce((n: number, r: any) => n + r.active.filter((a: any) => a.state === "firing").length, 0) ?? null;
  const nodesReady = health?.nodes ? `${health.nodes.filter((n: any) => n.ready).length}/${health.nodes.length}` : null;
  return (
    <div className="card stack">
      <div className="row between">
        <div>
          <h2 style={{ marginBottom: 2 }}>{env.display_name}</h2>
          <div className="row small muted">
            <span className="mono">{env.name}</span><Badge>{env.kind}</Badge>
            {env.is_production && <Badge kind="warn">production</Badge>}
            {env.active_investigations > 0 && <Badge kind="info">{env.active_investigations} running</Badge>}
          </div>
        </div>
        <div className="row">
          {env.grafana_url && <a href={env.grafana_url} target="_blank" rel="noreferrer noopener" className="small">Grafana ↗</a>}
          {live && <span className="live-dot" title="Refreshes every 5 seconds">live</span>}
        </div>
      </div>
      <div>
        <h3>Connectors</h3>
        <div className="row">
          {env.connectors.map((c) => (
            <span key={c.id} title={`${c.last_test_message ?? "never tested"}${c.last_test_at ? ` (${new Date(c.last_test_at).toLocaleString()})` : ""}`}>
              <Badge kind={!c.enabled ? "neutral" : c.last_test_ok === true ? "ok" : c.last_test_ok === false ? "bad" : "neutral"}>
                {c.type}{c.enabled ? "" : " (off)"}{c.last_test_ok === null && c.enabled ? " · untested" : ""}
              </Badge>
            </span>
          ))}
          {!env.connectors.length && <span className="muted small">none configured</span>}
        </div>
      </div>
      <ErrorBox error={error} />
      {!health && !error && <Skeleton rows={2} label="Checking live health" />}
      {health && (
        <div className="stack">
          <div className="stat-row">
            {nodesReady && <Stat value={nodesReady} label="nodes ready" />}
            <Stat value={health.unhealthy_pods.length} label="pods needing attention (last hour)" />
            {firing !== null && <Stat value={firing} label="firing alerts" />}
          </div>
          <div className="small muted">collected <Time iso={health.collected_at} /></div>
          {health.unhealthy_pods.length > 0 && <DataTable label={`Pods needing attention in ${env.display_name}`} rows={health.unhealthy_pods.slice(0, 8)} rowKey={(p: any) => p.namespace + p.name} cols={[
            { key: "pod", head: "Pod", primary: true, cell: (p: any) => <Identifier name={p.name} /> },
            { key: "ns", head: "Namespace", cell: (p: any) => <Truncate text={p.namespace} mono />, meta: (p: any) => p.namespace },
            { key: "ready", head: "Ready", cell: (p: any) => p.ready, meta: (p: any) => `${p.ready} ready` },
            { key: "restarts", head: "Restarts", cell: (p: any) => p.restarts, meta: (p: any) => `${p.restarts} restarts` },
            { key: "problem", head: "Problem", cell: (p: any) => p.problems.map((x: any) => x.reason).filter(Boolean).join(", ") || p.phase },
          ]} />}
          {Object.keys(health.errors).length > 0 && (
            <div className="alert warn small">Partial data: {Object.entries(health.errors).map(([k, v]) => `${k}: ${v}`).join("; ")}</div>
          )}
        </div>
      )}
    </div>
  );
}
