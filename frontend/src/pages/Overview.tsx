import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { useAuth } from "../auth";
import { Badge, ErrorBox, PageHead, StatusBadge, Time, useLoad } from "../components";

interface Conn { id: string; type: string; name: string; enabled: boolean; last_test_ok: boolean | null; last_test_at: string | null; last_test_message: string | null }
interface EnvOverview { name: string; display_name: string; kind: string; is_production: boolean; active_investigations: number; connectors: Conn[]; grafana_url?: string | null }

export default function Overview() {
  const { me } = useAuth();
  const ov = useLoad(() => api<{ environments: EnvOverview[] }>("/overview"), []);
  const recent = useLoad(() => api<any[]>("/investigations?limit=8"), []);
  return (
    <>
      <PageHead title="Overview" sub="Environment health, connector status and recent investigations" />
      <ErrorBox error={ov.error} />
      {me && !me.roles.length && (
        <div className="alert info" style={{ marginBottom: 14 }}>
          You are signed in as {me.email} but have no roles yet. Ask a MOKS administrator to grant access.
        </div>
      )}
      <div className="grid cols-2">
        {ov.data?.environments.map((e) => <EnvCard key={e.name} env={e} />)}
        {ov.data && !ov.data.environments.length && <div className="card empty">No environments are visible to you.</div>}
      </div>
      <div className="card" style={{ marginTop: 14 }}>
        <div className="row between"><h2>Recent investigations</h2><Link to="/investigations">All investigations →</Link></div>
        <table>
          <thead><tr><th>Question</th><th>Scope</th><th>Status</th><th>Started</th></tr></thead>
          <tbody>
            {recent.data?.map((i) => (
              <tr key={i.id}>
                <td><Link to={`/investigations/${i.id}`}>{i.question}</Link></td>
                <td className="mono small">{i.environment}/{i.namespace}</td>
                <td><StatusBadge status={i.status} /></td>
                <td><Time iso={i.created_at} /></td>
              </tr>
            ))}
          </tbody>
        </table>
        {recent.data && !recent.data.length && <div className="empty">No investigations yet. <Link to="/investigate">Start one.</Link></div>}
      </div>
    </>
  );
}

function EnvCard({ env }: { env: EnvOverview }) {
  const [health, setHealth] = useState<any>();
  const [error, setError] = useState<unknown>();
  const [busy, setBusy] = useState(false);

  async function refresh() {
    setBusy(true);
    setError(null);
    try {
      setHealth(await api(`/overview/${env.name}/health`));
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }

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
          <button className="ghost small" onClick={refresh} disabled={busy}>{busy ? "Checking…" : "Check live health"}</button>
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
      {health && (
        <div className="stack">
          <div className="row">
            {nodesReady && <div><div className="stat">{nodesReady}</div><div className="small muted">nodes ready</div></div>}
            <div style={{ marginLeft: 20 }}><div className="stat">{health.unhealthy_pods.length}</div><div className="small muted">pods needing attention (last hour)</div></div>
            {firing !== null && <div style={{ marginLeft: 20 }}><div className="stat">{firing}</div><div className="small muted">firing alerts</div></div>}
            <div className="right small muted">collected <Time iso={health.collected_at} /></div>
          </div>
          {health.unhealthy_pods.length > 0 && (
            <table>
              <thead><tr><th>Pod</th><th>Ready</th><th>Restarts</th><th>Problem</th></tr></thead>
              <tbody>
                {health.unhealthy_pods.slice(0, 8).map((p: any) => (
                  <tr key={p.namespace + p.name}>
                    <td className="mono small">{p.namespace}/{p.name}</td><td>{p.ready}</td><td>{p.restarts}</td>
                    <td className="small">{p.problems.map((x: any) => x.reason).filter(Boolean).join(", ") || p.phase}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {Object.keys(health.errors).length > 0 && (
            <div className="alert warn small">Partial data: {Object.entries(health.errors).map(([k, v]) => `${k}: ${v}`).join("; ")}</div>
          )}
        </div>
      )}
    </div>
  );
}
