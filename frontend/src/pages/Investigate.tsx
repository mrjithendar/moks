import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { api, API } from "../api";
import { useAuth } from "../auth";
import { Badge, ErrorBox, LineChart, LogLines, PageHead, ScopePicker, SourceRef, StatusBadge, Time, fmtBytes, useLoad } from "../components";
import { PlanForm } from "./Ops";

const SUGGESTIONS = [
  "Why is checkout returning 502 errors?",
  "Show pods restarting in the last 30 minutes.",
  "Correlate increased latency with deployments and application logs.",
  "Which pods were OOMKilled and how close were they to their memory limits?",
  "Are any services missing endpoints?",
  "Search logs for errors associated with trace ID ",
];

export function NewInvestigation() {
  const { can } = useAuth();
  const nav = useNavigate();
  const [params] = useSearchParams();
  const [env, setEnv] = useState(params.get("env") ?? "");
  const [ns, setNs] = useState(params.get("ns") ?? "");
  const [q, setQ] = useState(params.get("q") ?? "");
  const [minutes, setMinutes] = useState<string>("");
  const [error, setError] = useState<unknown>();
  const [busy, setBusy] = useState(false);
  const onEnv = useCallback((e: string) => setEnv(e), []);
  const onNs = useCallback((n: string) => setNs(n), []);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const inv = await api<{ id: string }>("/investigations", {
        method: "POST", body: { question: q, environment: env, namespace: ns, minutes: minutes ? Number(minutes) : undefined, incident_id: params.get("incident") || undefined },
      });
      nav(`/investigations/${inv.id}`);
    } catch (err) {
      setError(err);
      setBusy(false);
    }
  }

  return (
    <>
      <PageHead title="Investigate" sub="Ask a question. MOKS collects live evidence with read-only tools, correlates it and cites every fact." />
      {!can("investigation.run") && <div className="alert info">You can read investigations but not start them (needs the Investigator role).</div>}
      <form className="card stack" onSubmit={submit}>
        <div className="row">
          <ScopePicker env={env} ns={ns} onEnv={onEnv} onNs={onNs} allowAll />
          <label>Time window
            <select value={minutes} onChange={(e) => setMinutes(e.target.value)}>
              <option value="">From question, else 1 hour</option>
              <option value="15">Last 15 minutes</option><option value="30">Last 30 minutes</option><option value="60">Last hour</option>
              <option value="180">Last 3 hours</option><option value="360">Last 6 hours</option>
            </select>
          </label>
        </div>
        <div className="chat-input">
          <textarea rows={3} required minLength={3} maxLength={2000} value={q} onChange={(e) => setQ(e.target.value)}
            placeholder="e.g. Why is checkout returning 502 errors?"
            onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) (e.currentTarget.form as HTMLFormElement).requestSubmit(); }} />
          <button disabled={busy || !env || !ns || !can("investigation.run")}>{busy ? "Starting…" : "Investigate"}</button>
        </div>
        <div className="suggestions">
          {SUGGESTIONS.map((s) => <button type="button" key={s} onClick={() => setQ(s)}>{s}</button>)}
        </div>
        <ErrorBox error={error} />
      </form>
      <div className="small muted" style={{ marginTop: 10 }}>
        Phase 1 is read-only: MOKS proposes remediation but executes nothing. Evidence and tool calls are recorded in the audit log.
      </div>
    </>
  );
}

export function InvestigationList() {
  const list = useLoad(() => api<any[]>("/investigations?limit=100"), []);
  return (
    <>
      <PageHead title="Investigations" sub="Saved investigations you are allowed to see"><Link className="btn" to="/investigate">New investigation</Link></PageHead>
      <ErrorBox error={list.error} />
      <div className="card">
        <table>
          <thead><tr><th>Question</th><th>Scope</th><th>Status</th><th>By</th><th>Tools</th><th>Created</th></tr></thead>
          <tbody>
            {list.data?.map((i) => (
              <tr key={i.id}>
                <td><Link to={`/investigations/${i.id}`}>{i.question}</Link>{i.summary && <div className="small muted">{i.summary}</div>}</td>
                <td className="mono small">{i.environment}/{i.namespace}</td>
                <td><StatusBadge status={i.status} /></td>
                <td className="small">{i.created_by}</td>
                <td>{i.tool_calls}</td>
                <td><Time iso={i.created_at} /></td>
              </tr>
            ))}
          </tbody>
        </table>
        {list.data && !list.data.length && <div className="empty">No investigations.</div>}
      </div>
    </>
  );
}

interface Ev { type: string; seq: number; at: string; [k: string]: any }
const TERMINAL = new Set(["completed", "failed", "cancelled"]);

export function InvestigationView() {
  const { id } = useParams();
  const detail = useLoad(() => api<any>(`/investigations/${id}`), [id]);
  const [events, setEvents] = useState<Ev[]>([]);
  const [open, setOpen] = useState<string | null>(null);
  const [live, setLive] = useState(false);
  const [propose, setPropose] = useState<any>(null);
  const nav = useNavigate();
  const { can } = useAuth();
  const reloadRef = useRef(detail.reload);
  reloadRef.current = detail.reload;

  useEffect(() => {
    if (!id) return;
    // Server-Sent Events: progress for running investigations, and the full replay for finished ones.
    const es = new EventSource(`${API}/investigations/${id}/events`);
    setLive(true);
    const seen = new Set<number>();
    const onMsg = (m: MessageEvent) => {
      const ev: Ev = JSON.parse(m.data);
      if (seen.has(ev.seq)) return;
      seen.add(ev.seq);
      setEvents((xs) => [...xs, ev]);
      if (["tool_result", "report", "done"].includes(ev.type)) reloadRef.current();
    };
    for (const t of ["queued", "scope", "phase", "skills", "tool_call", "tool_result", "llm_usage", "note", "report", "error", "cancelled", "done"]) es.addEventListener(t, onMsg);
    es.addEventListener("end", () => { es.close(); setLive(false); });
    es.addEventListener("timeout", () => { es.close(); setLive(false); });
    es.onerror = () => { if (es.readyState === EventSource.CLOSED) setLive(false); };
    return () => { es.close(); setLive(false); };
  }, [id]);

  async function cancel() {
    await api(`/investigations/${id}/cancel`, { method: "POST" });
  }

  const inv = detail.data;
  const evidence: any[] = inv?.evidence ?? [];
  const report = inv?.report;
  const scopeEv = events.find((e) => e.type === "scope");
  const opened = evidence.find((e) => e.ref === open);

  return (
    <>
      <ErrorBox error={detail.error} />
      {inv && (
        <>
          <PageHead title={inv.question} sub={
            <span className="row small">
              <span className="mono">{inv.environment}/{inv.namespace}</span>
              <span>window {new Date(inv.time_from).toLocaleTimeString()}–{new Date(inv.time_to).toLocaleTimeString()}</span>
              <StatusBadge status={inv.status} />
              {live && !TERMINAL.has(inv.status) && <Badge kind="info">live</Badge>}
              <Badge>{scopeEv?.mode === "llm" ? `LLM: ${scopeEv.llm}` : report?.generated_by === "rules" ? "rule-based" : inv.mode}</Badge>
              <span>{inv.tool_calls} tool calls</span>
              {inv.tokens_in > 0 && <span>{inv.tokens_in + inv.tokens_out} tokens · ${Number(inv.cost_usd).toFixed(4)}</span>}
              <span>by {inv.created_by}</span>
            </span>
          }>
            {!TERMINAL.has(inv.status) && <button className="danger" onClick={cancel}>Cancel</button>}
            <Link className="btn ghost" style={{ border: "1px solid var(--border)", color: "var(--text)", background: "transparent" }}
              to={`/investigate?env=${inv.environment}&ns=${inv.namespace}&q=${encodeURIComponent(inv.question)}`}>Re-run</Link>
          </PageHead>
          {inv.error && <div className="alert error" style={{ marginBottom: 12 }}>{inv.error}</div>}
          <div className="grid" style={{ gridTemplateColumns: "minmax(0, 2fr) minmax(300px, 1fr)", alignItems: "start" }}>
            <div className="stack">
              {report ? <Report report={report} onCite={setOpen} onPropose={can("action.request") && inv.namespace !== "*" ? setPropose : undefined} /> : (
                <div className="card empty">{TERMINAL.has(inv.status) ? "No report was produced." : "Collecting evidence…"}</div>
              )}
              {report?.findings?.length > 0 && <Findings findings={report.findings} onCite={setOpen} />}
            </div>
            <div className="stack">
              <div className="card">
                <h2>Progress</h2>
                <Timeline events={events} />
              </div>
              <div className="card">
                <h2>Evidence ({evidence.length})</h2>
                <table>
                  <tbody>
                    {evidence.map((e) => (
                      <tr key={e.ref} className="clickable" onClick={() => setOpen(e.ref)}>
                        <td className="mono small">{e.ref}</td>
                        <td><div className="mono small">{e.tool}</div><div className="small muted">{e.summary}</div></td>
                        <td>{e.ok ? <Badge kind="ok">ok</Badge> : <Badge kind="bad">failed</Badge>}<div className="small muted"><Time iso={e.collected_at} /></div></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          </div>
          {opened && <EvidenceDrawer e={opened} grafana={inv.grafana_url} onClose={() => setOpen(null)} />}
          {propose && <div className="drawer" role="dialog" aria-label="Propose action"><div className="row between"><h2>Propose an action plan</h2>
            <button className="ghost small" onClick={() => setPropose(null)}>Close</button></div>
            <p className="small muted">Proposal from the report: {propose.action}</p>
            <PlanForm env={inv.environment} ns={inv.namespace} initial={{ ...(propose.fromSkill ? { action: propose.action, target_kind: propose.target_kind ?? "Deployment",
              params: propose.params ?? {} } : {}),
              target_name: propose.fromSkill ? propose.target_name ?? "" : (propose.target || "").split("/").pop()?.split(" ")[0] ?? "",
              reason: propose.fromSkill ? `${propose.note}\n\nFinding: ${propose.finding} (skill ${propose.skill}). From investigation: ${inv.question}` : `${propose.action}\n\nFrom investigation: ${inv.question}`,
              investigation_id: inv.id, incident_id: inv.incident_id ?? undefined,
              evidence: (report?.observed_facts ?? []).slice(0, 5).map((f: any) => ({ ref: f.evidence.join(","), summary: f.statement })) }}
              onDone={(id) => nav(`/actions/${id}`)} /></div>}
        </>
      )}
    </>
  );
}

function Cites({ refs, onCite }: { refs: string[]; onCite: (r: string) => void }) {
  return <>{refs.map((r) => <button type="button" key={r} className="cite" onClick={() => onCite(r)}>{r}</button>)}</>;
}

const CONF: Record<string, "bad" | "warn" | "neutral"> = { high: "bad", medium: "warn", low: "neutral" };

function Report({ report, onCite, onPropose }: { report: any; onCite: (r: string) => void; onPropose?: (r: any) => void }) {
  return (
    <div className="card">
      <div className="row between">
        <h2>Findings</h2>
        <span className="small muted">{report.generated_by === "llm" ? "Written by the LLM from cited evidence" : "Rule-based report"}</span>
      </div>
      <p style={{ marginTop: 0 }}>{report.summary}</p>
      <div className="report-section facts">
        <h3>Observed facts</h3>
        {report.observed_facts.length ? <ul>{report.observed_facts.map((f: any, i: number) => <li key={i}>{f.statement}<Cites refs={f.evidence} onCite={onCite} /></li>)}</ul>
          : <div className="muted small">No facts with supporting evidence.</div>}
      </div>
      <div className="report-section causes">
        <h3>Suspected causes</h3>
        {report.suspected_causes.length ? (
          <ul>{report.suspected_causes.map((c: any, i: number) => (
            <li key={i}><Badge kind={CONF[c.confidence]}>{c.confidence} confidence</Badge> {c.cause}<Cites refs={c.evidence} onCite={onCite} />
              {c.reasoning && <div className="small muted">{c.reasoning}</div>}
              {!c.verified && <div className="small muted">Not verified.</div>}</li>
          ))}</ul>
        ) : <div className="muted small">No cause suggested.</div>}
      </div>
      {report.missing_evidence.length > 0 && (
        <div className="report-section missing"><h3>Missing evidence</h3><ul>{report.missing_evidence.map((m: string, i: number) => <li key={i}>{m}</li>)}</ul></div>
      )}
      {report.next_steps.length > 0 && (
        <div className="report-section steps"><h3>Recommended next steps</h3><ul>{report.next_steps.map((m: string, i: number) => <li key={i}>{m}</li>)}</ul></div>
      )}
      {report.skills?.length > 0 && (
        <div className="small" style={{ marginBottom: 12 }}>Skills applied: {report.skills.map((s: any) => (
          <Link key={s.name} to="/skills" title={s.reasons.join("; ")}><Badge kind="info">{s.name}</Badge></Link>))}</div>
      )}
      {report.skill_actions?.length > 0 && (
        <div className="report-section remediation">
          <h3>Suggested actions (from skills)</h3>
          <ul>{report.skill_actions.map((a: any, i: number) => (
            <li key={i}><span className="mono">{a.action}</span>{a.target_name && <span className="mono small"> · {a.target_kind}/{a.target_name}</span>}
              <span className="small muted"> — {a.note} (finding: {a.finding}; skill {a.skill})</span>
              {onPropose && <button className="ghost small" style={{ marginLeft: 8 }} onClick={() => onPropose({ ...a, fromSkill: true })}>Propose as action plan</button>}</li>
          ))}</ul>
        </div>
      )}
      {report.proposed_remediation.length > 0 && (
        <div className="report-section remediation">
          <h3>Proposed remediation</h3>
          <ul>{report.proposed_remediation.map((r: any, i: number) => (
            <li key={i}><strong>{r.action}</strong>{r.target && <span className="mono small"> · {r.target}</span>}
              {onPropose && <button className="ghost small" style={{ marginLeft: 8 }} onClick={() => onPropose(r)}>Propose as action plan</button>}
              <div className="small muted">{[r.expected_impact && `Impact: ${r.expected_impact}`, r.risk && `Risk: ${r.risk}`, r.rollback && `Rollback: ${r.rollback}`].filter(Boolean).join(" · ")}</div>
            </li>
          ))}</ul>
          <div className="small muted">Proposals only: nothing was changed. Turn one into a reviewed action plan to execute it (approval required in production).</div>
        </div>
      )}
      {report.notes?.length > 0 && <details><summary className="small">Investigation notes ({report.notes.length})</summary><ul className="small">{report.notes.map((n: string, i: number) => <li key={i}>{n}</li>)}</ul></details>}
      {report.validation?.uncited_facts_dropped > 0 && <div className="small muted">{report.validation.uncited_facts_dropped} uncited statement(s) were removed by citation checks.</div>}
    </div>
  );
}

const SEV: Record<string, "bad" | "warn" | "neutral"> = { high: "bad", medium: "warn", low: "neutral" };

function Findings({ findings, onCite }: { findings: any[]; onCite: (r: string) => void }) {
  return (
    <div className="card">
      <h2>Automatic checks ({findings.length})</h2>
      <table><tbody>
        {findings.map((f, i) => (
          <tr key={i}>
            <td><Badge kind={SEV[f.severity]}>{f.severity}</Badge></td>
            <td><div>{f.title}<Cites refs={f.evidence ?? []} onCite={onCite} /></div><div className="small muted">{f.detail}</div></td>
            <td className="mono small">{f.namespace}</td>
          </tr>
        ))}
      </tbody></table>
    </div>
  );
}

function Timeline({ events }: { events: Ev[] }) {
  if (!events.length) return <div className="muted small">Waiting for the worker…</div>;
  return (
    <ul className="timeline">
      {events.filter((e) => e.type !== "llm_usage").map((e) => {
        let dot = "", text: React.ReactNode = e.type;
        if (e.type === "queued") text = `Queued (window ${e.window_minutes} min)`;
        if (e.type === "scope") { text = <>Scope: {e.environment}/{e.namespace}; connectors {e.connectors.join(", ") || "none"}{Object.keys(e.unavailable_connectors ?? {}).length ? ` (unavailable: ${Object.keys(e.unavailable_connectors).join(", ")})` : ""}</>; }
        if (e.type === "phase") { text = e.label; dot = "run"; }
        if (e.type === "tool_call") text = <><span className="mono">{e.tool}</span> {e.args?.namespace && <span className="muted">{e.args.namespace}</span>}{e.purpose && <span className="muted"> — {e.purpose}</span>}</>;
        if (e.type === "tool_result") { dot = e.denied ? "bad" : e.ok ? "ok" : "warn"; text = <>{e.ref && <span className="cite">{e.ref}</span>} {e.denied ? <strong>Denied: </strong> : null}{e.summary} <span className="muted">({e.duration_ms} ms)</span></>; }
        if (e.type === "note") { dot = "warn"; text = e.message; }
        if (e.type === "skills") { dot = "ok"; text = <>Skills selected: {e.skills.map((s: any) => s.name).join(", ")}</>; }
        if (e.type === "report") { dot = "ok"; text = "Report ready"; }
        if (e.type === "error") { dot = "bad"; text = e.message; }
        if (e.type === "cancelled") { dot = "warn"; text = "Cancelled"; }
        if (e.type === "done") { dot = e.status === "completed" ? "ok" : "bad"; text = `Finished: ${e.status}`; }
        return <li key={e.seq}><span className="small muted">{new Date(e.at).toLocaleTimeString()}</span><span className={`dot ${dot}`} /><span>{text}</span></li>;
      })}
    </ul>
  );
}

export function EvidenceDrawer({ e, onClose, grafana }: { e: any; onClose: () => void; grafana?: string | null }) {
  useEffect(() => {
    const h = (k: KeyboardEvent) => k.key === "Escape" && onClose();
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [onClose]);
  return (
    <div className="drawer" role="dialog" aria-label={`Evidence ${e.ref}`}>
      <div className="row between"><h2>{e.ref} · <span className="mono">{e.tool}</span></h2><button className="ghost small" onClick={onClose}>Close</button></div>
      <dl className="kv">
        <dt>Summary</dt><dd>{e.summary}</dd>
        <dt>Collected</dt><dd>{new Date(e.collected_at).toLocaleString()} ({e.duration_ms} ms)</dd>
        <dt>Scope</dt><dd className="mono small">{e.scope.environment}/{e.scope.namespace ?? "cluster"} · {new Date(e.scope.from).toLocaleTimeString()}–{new Date(e.scope.to).toLocaleTimeString()}</dd>
        <dt>Arguments</dt><dd className="mono small">{JSON.stringify(e.args)}</dd>
        {e.truncated && <><dt>Note</dt><dd>Result truncated to the size limit.</dd></>}
        {e.error && <><dt>Error</dt><dd className="mono small">{e.error}</dd></>}
      </dl>
      <div style={{ margin: "10px 0" }}><SourceRef source={e.source} />{grafana && <a className="small" href={grafana} target="_blank" rel="noreferrer noopener">Open Grafana ↗</a>}</div>
      {e.ok && <EvidenceData tool={e.tool} data={e.data} />}
    </div>
  );
}

export function EvidenceData({ tool, data }: { tool: string; data: any }) {
  if (data == null) return null;
  if (tool === "k8s_pod_logs") return <LogLines lines={(data.lines ?? []).map((l: string) => ({ line: l }))} />;
  if (tool.startsWith("loki_") && data.lines) return <LogLines lines={data.lines} />;
  if ((tool === "prom_query" || tool.startsWith("loki_")) && data.series) {
    return data.series[0]?.values ? <LineChart series={data.series} /> : <pre>{JSON.stringify(data.series, null, 1)}</pre>;
  }
  if (tool === "prom_pod_resources") {
    return (
      <div className="stack">
        {Object.entries(data.results ?? {}).map(([k, v]: [string, any]) => (
          <div key={k}><h3>{k.replace(/_/g, " ")}</h3>{v.error ? <div className="alert warn small">{v.error}</div> :
            <LineChart series={v.series} unit={k.includes("bytes") ? "bytes" : k.includes("ratio") ? "ratio" : "cores"} />}</div>
        ))}
      </div>
    );
  }
  return <pre className="logbox">{JSON.stringify(data, null, 2)}</pre>;
}

export { fmtBytes };
