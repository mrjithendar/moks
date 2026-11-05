import { useEffect, useState, type FormEvent } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { api } from "../api";
import { useAuth } from "../auth";
import { Badge, ErrorBox, LineChart, LogLines, PageHead, SourceRef, StatusBadge, fmtBytes, useLoad } from "../components";
import { type Ev, useInvestigationEvents } from "../investigationStream";
import { Markdown } from "../markdown";
import { CopyButton, Dialog, Drawer, Truncate } from "../ui";
import { proposalPlan } from "./Conversations";
import { PlanForm } from "./Ops";
import { Judge, SimilarIncidents, SkillRecommendations } from "./MLParts";

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
  const [q, setQ] = useState(params.get("q") ?? "");
  const [minutes, setMinutes] = useState<string>("");
  const [error, setError] = useState<unknown>();
  const [busy, setBusy] = useState(false);
  const [everywhere, setEverywhere] = useState(false);
  const config = useLoad(() => api<{ llm: { provider: string; model: string } | null; skills: string[] }>("/investigate/config"), []);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      if (everywhere) {
        const g = await api<{ investigations: { id: string }[] }>("/investigations/group", { method: "POST", body: { question: q, minutes: minutes ? Number(minutes) : undefined } });
        nav(g.investigations.length === 1 ? `/conversations/${g.investigations[0].id}` : "/investigations");
        return;
      }
      // Scope comes from the question (or a deep link from the explorer); the backend resolves the rest.
      const inv = await api<{ id: string; conversation_id: string }>("/investigations", {
        method: "POST", body: { question: q, environment: params.get("env") || undefined, namespace: params.get("ns") || undefined,
          minutes: minutes ? Number(minutes) : undefined, incident_id: params.get("incident") || undefined },
      });
      nav(`/conversations/${inv.conversation_id}`);
    } catch (err) {
      setError(err);
      setBusy(false);
    }
  }

  return (
    <>
      <PageHead title="Investigate" sub="Ask a question. MOKS finds the environment and workload, runs the matching SRE skills, and cites every fact." />
      {!can("investigation.run") && <div className="alert info">You can read investigations but not start them (needs the Investigator role).</div>}
      {config.data && !config.data.llm && (
        <div className="alert warn small" style={{ marginBottom: 12 }}>
          No active chat model: reports are rule-based. {can("admin.llm") && <Link to="/admin/llm">Activate an LLM provider →</Link>}
        </div>
      )}
      <form className="card stack" onSubmit={submit}>
        <div className="row between">
          <label>Time window
            <select value={minutes} onChange={(e) => setMinutes(e.target.value)}>
              <option value="">From question, else 1 hour</option>
              <option value="15">Last 15 minutes</option><option value="30">Last 30 minutes</option><option value="60">Last hour</option>
              <option value="180">Last 3 hours</option><option value="360">Last 6 hours</option><option value="1440">Last 24 hours</option>
            </select>
          </label>
          {config.data && (
            <div className="row small muted">
              {config.data.llm && <span>LLM <Badge kind="ok">{config.data.llm.provider} / {config.data.llm.model}</Badge></span>}
              <span><Link to="/skills">{config.data.skills.length} SRE skills</Link> available</span>
            </div>
          )}
        </div>
        <div className="chat-input">
          <textarea aria-label="Question" rows={3} required minLength={3} maxLength={2000} value={q} onChange={(e) => setQ(e.target.value)}
            placeholder="e.g. Why is checkout returning 502 errors?"
            onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) (e.currentTarget.form as HTMLFormElement).requestSubmit(); }} />
          <button disabled={busy || !can("investigation.run")}>{busy ? "Starting…" : "Investigate"}</button>
        </div>
        {!params.get("env") && <label className="inline"><input type="checkbox" checked={everywhere} onChange={(e) => setEverywhere(e.target.checked)} /> Check all my environments (one investigation each)</label>}
        <div className="suggestions">
          {SUGGESTIONS.map((s) => <button type="button" key={s} onClick={() => setQ(s)}>{s}</button>)}
        </div>
        <ErrorBox error={error} />
      </form>
      <div className="small muted" style={{ marginTop: 10 }}>
        Investigations are read-only. Remediation runs only through an approved action plan. Evidence and tool calls are recorded in the audit log.
      </div>
    </>
  );
}

const TERMINAL = new Set(["completed", "failed", "cancelled"]);
const STAGES: [string, string][] = [["triage", "Triage"], ["evidence", "Evidence"], ["correlation", "Correlation"], ["hypotheses", "Hypotheses"], ["verification", "Verification"], ["report", "Report"]];

function Stages({ current, done }: { current?: string; done: boolean }) {
  const reached = STAGES.findIndex(([k]) => k === (current === "skill" || current === "followup" ? "evidence" : current === "knowledge" ? "triage" : current));
  return (
    <ol className="stages" aria-label="Investigation stages">
      {STAGES.map(([k, label], i) => <li key={k} className={done || i < reached ? "done" : i === reached ? "now" : ""} aria-current={i === reached && !done ? "step" : undefined}>{label}</li>)}
    </ol>
  );
}

function FollowUp({ id }: { id: string }) {
  const nav = useNavigate();
  const [q, setQ] = useState("");
  const [error, setError] = useState<unknown>();
  async function ask(e: FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      const r = await api<{ conversation_id: string }>(`/investigations/${id}/follow-up`, { method: "POST", body: { question: q } });
      setQ("");
      nav(`/conversations/${r.conversation_id}`);
    } catch (err) {
      setError(err);
    }
  }
  return (
    <form className="card stack" onSubmit={ask}>
      <h2>Ask a follow-up</h2>
      <div className="filters"><label style={{ flex: 1 }}>Question<input value={q} minLength={3} maxLength={2000} onChange={(e) => setQ(e.target.value)} placeholder="Same scope and time window as this investigation" /></label><button disabled={q.length < 3}>Ask</button></div>
      <ErrorBox error={error} />
    </form>
  );
}

function Coverage({ report }: { report: any }) {
  const c = report.coverage;
  if (!c) return null;
  return (
    <div className="card">
      <h2>What this report saw</h2>
      <div className="row small">{Object.entries(c.sources as Record<string, any>).map(([k, v]) => <Badge key={k} kind={v.failed ? "warn" : "ok"}>{k}: {v.ok} ok{v.failed ? `, ${v.failed} failed` : ""}</Badge>)}</div>
      {Object.entries(c.unavailable ?? {}).map(([k, v]) => <div key={k} className="alert warn small" style={{ marginTop: 6 }}>{k}: {String(v)}</div>)}
      {c.budget_reached && <div className="alert warn small" style={{ marginTop: 6 }}>The tool budget of {c.tool_budget} calls ran out, so evidence may be incomplete.</div>}
      <div className="small muted" style={{ marginTop: 6 }}>{c.tool_calls} of {c.tool_budget} tool calls used.</div>
    </div>
  );
}

function Relationships({ data, primary }: { data?: { nodes: any[]; edges: any[] }; primary?: string | null }) {
  if (!data?.nodes.length) return null;
  const roots = data.nodes.filter((n) => !n.id.startsWith("Pod/"));
  return (
    <div className="card">
      <h2>Observed resources</h2>
      {primary && <div className="small" style={{ marginBottom: 6 }}>Primary: <span className="mono">{primary}</span></div>}
      <ul className="small">{roots.slice(0, 20).map((n) => {
        const kids = data.edges.filter((e) => e.from === n.id);
        return <li key={n.id}><span className="mono">{n.id}</span> {n.healthy === false && <Badge kind="bad">unhealthy</Badge>}
          {kids.length > 0 && <div className="muted mono">{kids.slice(0, 5).map((e) => `${e.kind} ${e.to.replace("Pod/", "")}`).join(", ")}{kids.length > 5 ? ` +${kids.length - 5}` : ""}</div>}</li>;
      })}</ul>
      <div className="small muted">From ownerReferences and selectors seen in this investigation.</div>
    </div>
  );
}

export function InvestigationView() {
  const { id } = useParams();
  const detail = useLoad(() => api<any>(`/investigations/${id}`), [id]);
  const [open, setOpen] = useState<string | null>(null);
  const [propose, setPropose] = useState<any>(null);
  const nav = useNavigate();
  const { can } = useAuth();
  const { events, stream } = useInvestigationEvents(id, detail.reload);
  useEffect(() => { setOpen(null); setPropose(null); }, [id]);

  async function cancel() {
    await api(`/investigations/${id}/cancel`, { method: "POST" });
  }

  const inv = detail.data?.id === id ? detail.data : undefined; // never show the previous route's data
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
              {!TERMINAL.has(inv.status) && <Badge kind={stream === "live" ? "info" : stream === "failed" ? "bad" : "warn"}>{stream === "live" ? "live" : stream === "failed" ? "updates stopped" : stream === "ended" ? "updates ended" : stream}</Badge>}
              <Badge>{scopeEv?.mode === "llm" ? `LLM: ${scopeEv.llm}` : report?.generated_by === "rules" ? "rule-based" : inv.mode}</Badge>
              <span>{inv.tool_calls} tool calls</span>
              {inv.tokens_in > 0 && <span>{inv.tokens_in + inv.tokens_out} tokens · ${Number(inv.cost_usd).toFixed(4)}</span>}
              <span>by {inv.created_by}</span>
            </span>
          }>
            {!TERMINAL.has(inv.status) && <button className="danger" onClick={cancel}>Cancel</button>}
            <Link className="btn ghost"
              to={`/investigate?env=${inv.environment}&ns=${inv.namespace}&q=${encodeURIComponent(inv.question)}`}>Re-run</Link>
            {inv.conversation_id && <Link className="btn" to={`/conversations/${inv.conversation_id}`}>Open conversation</Link>}
          </PageHead>
          {inv.error && <div className="alert error" style={{ marginBottom: 12 }}>{inv.error}</div>}
          <div className="grid inv-layout">
            <div className="stack">
              <Stages current={inv.stage} done={TERMINAL.has(inv.status)} />
              {inv.parent_id && <div className="small">Follow-up of <Link to={`/investigations/${inv.parent_id}`}>an earlier investigation</Link>.</div>}
              {inv.trace_id && TERMINAL.has(inv.status) && (
                <div className="small muted">Trace <code>{inv.trace_id}</code> <CopyButton text={inv.trace_id} label="Copy trace id" /> finds this run in Jaeger and its log lines in Loki.</div>
              )}
              {report ? <Report report={report} evidence={evidence} onCite={setOpen} onPropose={can("action.request") && inv.namespace !== "*" ? setPropose : undefined} investigationId={inv.id} /> : (
                <div className="card empty">{TERMINAL.has(inv.status) ? "No report was produced." : "Collecting evidence…"}</div>
              )}
              {report?.findings?.length > 0 && <Findings findings={report.findings} onCite={setOpen} />}
              {inv.actions?.length > 0 && <div className="card"><h2>Executed actions and validated recovery</h2>
                <table><tbody>{inv.actions.map((a: any) => <tr key={a.id}><td><Link to={`/actions/${a.id}`}>{a.action}</Link> <span className="mono small">{a.target}</span></td>
                  <td><Badge kind={a.status === "succeeded" ? "ok" : a.status === "reconciliation_required" ? "bad" : "neutral"}>{a.status}</Badge></td>
                  <td className="small">{a.validated ? "recovery validated" : "not validated"}</td></tr>)}</tbody></table></div>}
              {inv.follow_ups?.length > 0 && <div className="card"><h2>Follow-ups</h2>{inv.follow_ups.map((f: any) => <div key={f.id}><StatusBadge status={f.status} /> <Link to={`/investigations/${f.id}`}>{f.question}</Link></div>)}</div>}
              {TERMINAL.has(inv.status) && can("investigation.run") && <FollowUp id={inv.id} />}
            </div>
            <div className="stack">
              {report && <Coverage report={report} />}
              {report && <Relationships data={report.relationships} primary={report.primary_resource} />}
              <div className="card">
                <h2>Progress</h2>
                <Timeline events={events} />
              </div>
              <div className="card">
                <h2>Evidence ({evidence.length})</h2>
                {evidence.map((e) => (
                  <button type="button" key={e.ref} className="ev-row" onClick={() => setOpen(e.ref)}>
                    <div className="row tight"><span className="cite nocite">{e.ref}</span><Truncate text={e.tool} mono /><EvidenceKind kind={e.kind} ok={e.ok} /></div>
                    <div className="small muted clamp-2">{e.summary}</div>
                  </button>
                ))}
              </div>
            </div>
          </div>
          {opened && <EvidenceDrawer e={opened} grafana={inv.grafana_url} onClose={() => setOpen(null)} />}
          {report && can("investigation.run") && <div className="mt"><ProposalForm investigation={inv.id} question={inv.question} evidence={evidence} findings={[...new Set<string>((report.findings ?? []).map((f: any) => f.kind))]} /></div>}
          {propose && <Dialog title="Propose an action plan" wide onClose={() => setPropose(null)}>
            <p className="small muted">Proposal from the report: {propose.action}</p>
            <PlanForm env={inv.environment} ns={inv.namespace} initial={proposalPlan(inv, propose)} onDone={(id) => nav(`/actions/${id}`)} /></Dialog>}
        </>
      )}
    </>
  );
}

/** A draft for someone else to review. Nothing here publishes a skill or a runbook. */
function ProposalForm({ investigation, question, evidence, findings }: { investigation: string; question: string; evidence: any[]; findings: string[] }) {
  const usable = evidence.filter((e) => e.kind === "observation" || e.kind === "incomplete");
  const [form, setForm] = useState({ kind: "runbook", title: "", body: "", owner: "", refs: [] as string[] });
  const [state, setState] = useState<{ saved?: string; error?: string }>({});
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    try {
      const r = await api<any>("/proposals", { method: "POST", body: { investigation_id: investigation, kind: form.kind, title: form.title, body: form.body, owner: form.owner, evidence: form.refs, regression_fixture: { question, findings } } });
      setState({ saved: `Saved as draft v${r.version}. It needs a review by someone else.` });
    } catch (err: any) {
      setState({ error: err.message });
    }
  };
  return (
    <details className="card"><summary>Propose a runbook or skill update</summary>
      <form className="stack" onSubmit={submit}>
        <p className="small muted">Saved as a redacted draft with this question as its regression case. Someone else reviews it, approval makes a draft, and it changes nothing until that draft is published.</p>
        <label>Type<select value={form.kind} onChange={(e) => setForm({ ...form, kind: e.target.value })}><option value="runbook">Runbook</option><option value="skill">Skill (a complete SKILL.md)</option></select></label>
        <label>Title<input required minLength={5} maxLength={200} value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })} /></label>
        <label>Owner<input required minLength={2} maxLength={200} value={form.owner} onChange={(e) => setForm({ ...form, owner: e.target.value })} placeholder="team-payments" /></label>
        <label>What should change<textarea required minLength={20} rows={5} value={form.body} onChange={(e) => setForm({ ...form, body: e.target.value })} /></label>
        <fieldset><legend className="small">Evidence (successful live results only)</legend>
          <div className="row small">{usable.map((e) => <label key={e.ref} className="inline"><input type="checkbox" checked={form.refs.includes(e.ref)}
            onChange={(ev) => setForm({ ...form, refs: ev.target.checked ? [...form.refs, e.ref] : form.refs.filter((r) => r !== e.ref) })} /> {e.ref}</label>)}</div>
        </fieldset>
        {state.error && <div className="alert error small">{state.error}</div>}
        {state.saved && <div className="alert ok small">{state.saved}</div>}
        <div><button type="submit" disabled={!form.refs.length}>Save draft</button></div>
      </form>
    </details>
  );
}

const EVIDENCE_KINDS: Record<string, { label: string; tone: "ok" | "warn" | "bad" | "neutral" | "info" }> = {
  observation: { label: "observed", tone: "ok" },
  incomplete: { label: "partial result", tone: "warn" },
  tool_error: { label: "collection failed", tone: "bad" },
  access_denied: { label: "access denied", tone: "bad" },
  documentation: { label: "documentation", tone: "info" },
  historical: { label: "historical", tone: "neutral" },
};

export function EvidenceKind({ kind, ok }: { kind?: string; ok: boolean }) {
  const k = EVIDENCE_KINDS[kind ?? (ok ? "observation" : "tool_error")] ?? EVIDENCE_KINDS.observation;
  return <Badge kind={k.tone}>{k.label}</Badge>;
}

const FACT_KINDS: Record<string, string> = { collection: "about collection", documentation: "from documentation", historical: "earlier observation" };
const VERIFIED: [string, string, string][] = [
  ["configuration_defect", "Verified configuration defects", "A check read the wrong setting from the object itself."],
  ["observed_failure", "Observed failures", "These happened. Why they happened is still open, so see the suspected causes."],
  ["observed_condition", "Observed conditions", "Reported by the system itself. This shows the condition, not its cause, and nothing about the running workloads."],
];

const RESPONSE_LABEL: Record<string, string> = {
  deployment_guidance: "Deployment guidance", repository_answer: "Repository answer", execution_plan: "Change request",
  conceptual: "Explanation", troubleshooting: "Troubleshooting", health_check: "Health check",
};
const GUIDED = new Set(["deployment_guidance", "repository_answer", "execution_plan"]);
const SECTION_TITLES: [string, string][] = [["prerequisites", "Prerequisites"], ["steps", "Steps"], ["verification", "Verification"], ["rollback", "Rollback"]];
const GAP_LABEL: Record<string, string> = { missing_source: "no source", retrieval_failure: "retrieval failed", insufficient_evidence: "not in the sources", unresolved: "unresolved" };

function Routing({ report }: { report: any }) {
  const r = report.request;
  if (!r) return null;
  const target = r.targets?.[0]?.mention;
  const where = [r.repository && `repository ${r.repository}`, r.namespaces?.length && `namespace ${r.namespaces.join(", ")}`, r.environment && `environment ${r.environment}`].filter(Boolean).join(" · ");
  return <div className="small muted routing">Answered as <Badge kind="info">{RESPONSE_LABEL[report.response_type] ?? report.response_type}</Badge>
    {target && <> about <strong>{target}</strong></>}{where && <> ({where})</>}
    {report.relevance && report.relevance.status !== "passed" && <> · <Badge kind={report.relevance.status === "failed" ? "bad" : "warn"}>relevance check {report.relevance.status}</Badge></>}</div>;
}

function GuidanceItems({ items, onCite }: { items: any[]; onCite: (r: string) => void }) {
  return <ol>{items.map((x: any, i: number) => <li key={i}>
    {x.command ? <><code className="mono">{x.command}</code>{x.note || x.text !== `Run \`${x.command}\`` ? <span className="small"> — {x.note ?? x.text}</span> : null}</> : <Markdown text={x.text} />}
    <Cites refs={x.sources ?? []} onCite={onCite} />{x.path && <span className="small muted"> {x.path}</span>}{x.general && <> <Badge kind="info">general guidance</Badge></>}
    {x.placeholders?.length > 0 && <div className="small muted">Placeholder: {x.placeholders.join(", ")}</div>}</li>)}</ol>;
}

export function Answer({ report, onCite }: { report: any; onCite: (r: string) => void }) {
  const a = report.answer ?? {};
  const ex = a.execution;
  return <div className="answer stack tight">
    {ex && <div className="alert warn small"><strong>Nothing was changed.</strong> {ex.note}
      <ul>{ex.authorization?.map((s: string, i: number) => <li key={i}>{s}</li>)}</ul>
      {ex.actions?.length > 0 && <div>Allowlisted actions that may apply: {ex.actions.map((x: any) => <Badge key={x.action} kind="neutral">{x.title}</Badge>)}</div>}</div>}
    {a.overview?.length > 0 && <div className="rs static"><Markdown text={a.overview[0].text} /><Cites refs={a.overview[0].sources} onCite={onCite} /> <span className="small muted">{a.overview[0].path}</span></div>}
    {a.explanation && <div className="rs static"><Markdown text={a.explanation} />{a.cited?.length > 0 && <Cites refs={a.cited} onCite={onCite} />}
      {a.examples?.length > 0 && <ul className="small">{a.examples.map((x: string, i: number) => <li key={i}><Markdown text={x} /></li>)}</ul>}</div>}
    {SECTION_TITLES.map(([key, title]) => a[key]?.length > 0 && <Section key={key} title={title} count={a[key].length}><GuidanceItems items={a[key]} onCite={onCite} /></Section>)}
    {a.configuration?.length > 0 && <Section title="Configuration found" count={a.configuration.length} open={false}>
      <ul className="small">{a.configuration.map((c: any, i: number) => <li key={i}><span className="mono">{c.path}</span> — {c.detail}<Cites refs={c.sources ?? []} onCite={onCite} /></li>)}</ul></Section>}
    {a.question_for_user && <div className="alert info small"><strong>To go further:</strong> {a.question_for_user}</div>}
    {a.gaps?.length > 0 && <div className="rs missing"><strong>What the sources do not establish</strong><ul>{a.gaps.map((g: any, i: number) => <li key={i}><Badge kind="neutral">{GAP_LABEL[g.kind] ?? g.kind}</Badge> {g.detail}{g.evidence?.length > 0 && <Cites refs={g.evidence} onCite={onCite} />}</li>)}</ul></div>}
    {a.conflicts?.length > 0 && <div className="rs missing"><strong>Sources disagree or may be stale</strong><ul>{a.conflicts.map((c: any, i: number) => <li key={i}>{c.detail}<Cites refs={c.sources ?? []} onCite={onCite} /></li>)}</ul></div>}
    {a.assumptions?.length > 0 && <div className="small muted"><strong>Assumptions:</strong> {a.assumptions.join(" ")}</div>}
    {a.sources?.length > 0 && <Section title="Sources considered" count={a.sources.length} open={false}>
      <ul className="small">{a.sources.map((x: any, i: number) => <li key={i}>{x.ref && <Cites refs={[x.ref]} onCite={onCite} />} <span className="mono">{x.repository ? `${x.repository}:` : ""}{x.path}</span> <Badge kind={x.used ? "ok" : "neutral"}>{x.used ? "used" : "not used"}</Badge> <span className="muted">{x.why}</span></li>)}</ul></Section>}
  </div>;
}

const KNOWLEDGE_STATUS: Record<string, [("ok" | "warn" | "bad" | "neutral"), string]> = {
  retrieved: ["ok", "retrieved"], partial: ["warn", "partly failed"], empty: ["neutral", "nothing relevant"], failed: ["bad", "search failed"], not_attempted: ["neutral", "not searched"],
};
const AUTHORITY: Record<string, [("ok" | "info" | "warn" | "neutral"), string]> = {
  target: ["ok", "about the target"], dependency: ["info", "declared dependency"], shared: ["info", "general guidance"], unverified: ["warn", "relationship not established"], unrelated: ["neutral", "other service"], low_relevance: ["neutral", "low relevance"],
};
const EMPTY_KNOWLEDGE: Record<string, string> = {
  empty: "MOKS searched the knowledge index and found no passage you may read that matches this request. The answer rests on live evidence only.",
  failed: "The knowledge search failed, so documented procedures could not be checked. The answer rests on live evidence only.",
  not_attempted: "The knowledge index was not searched for this request.",
};
const safeLink = (url?: string | null) => (url && /^https?:\/\//i.test(url) ? url : undefined);

const PASSAGE_STATE: Record<string, [("ok" | "info" | "warn" | "neutral"), string]> = {
  cited: ["ok", "cited"], supplied: ["info", "given to the model, not cited"], eligible: ["neutral", "eligible, not supplied"], retrieved: ["neutral", "retrieved only"],
};

/** Reports from before passage ids (knowledge version 1) have only used/provided; derive the state the same way the backend does. */
export function passageState(p: any): string {
  if (p.state) return p.state;
  if (p.used) return "cited";
  if (p.provided) return "supplied";
  return ["target", "dependency", "shared", "unverified"].includes(p.authority) ? "eligible" : "retrieved";
}

function Passage({ p, onCite, investigationId }: { p: any; onCite: (r: string) => void; investigationId?: string }) {
  const [tone, label] = AUTHORITY[p.authority] ?? ["neutral", p.authority];
  const state = passageState(p);
  const [stone, slabel] = PASSAGE_STATE[state] ?? ["neutral", state];
  const lines = p.lines?.[0] ? `L${p.lines[0]}${p.lines[1] && p.lines[1] !== p.lines[0] ? `-${p.lines[1]}` : ""}` : "";
  const where = `${p.repository ? `${p.repository}:` : ""}${p.path}`;
  const href = safeLink(p.url);
  return <li className="passage">
    <div className="row tight wrap"><Cites refs={[p.id ?? p.ref]} onCite={onCite} /><Badge kind={tone}>{label}</Badge><Badge kind={stone}>{slabel}</Badge>
      {p.cited_by === "moks" && <span className="small muted">added by MOKS</span>}
      {p.freshness?.state && p.freshness.state !== "current" && <Badge kind="warn">{p.freshness.state}</Badge>}
      {href ? <a className="mono small" href={href} target="_blank" rel="noreferrer noopener">{where} {lines}</a> : <span className="mono small">{where} {lines}</span>}
      {p.revision && <span className="mono small muted">@ {String(p.revision).slice(0, 10)}</span>}</div>
    {p.heading && <div className="small">{p.heading}</div>}
    {p.excerpt && <div className="small muted excerpt">{p.excerpt}</div>}
    <div className="small muted">{p.excluded ? `Not used: ${p.excluded}` : p.basis?.join("; ")}{p.freshness?.last_checked ? ` · checked ${new Date(p.freshness.last_checked).toLocaleDateString()}` : ""}</div>
    {p.chunk_id && <Judge kind="passage" target={p.chunk_id} investigationId={investigationId} options={[["relevant", "Relevant"], ["irrelevant", "Not relevant"], ["outdated", "Outdated"]]} />}
  </li>;
}

/** What was searched, what came back and why each passage was or was not used. Documentation, never current state. */
export function KnowledgeUsed({ k, onCite, investigationId }: { k?: any; onCite: (r: string) => void; investigationId?: string }) {
  if (!k) return null;
  const [tone, label] = KNOWLEDGE_STATUS[k.status] ?? ["neutral", k.status];
  const passages = k.passages ?? [];
  const used = passages.filter((p: any) => passageState(p) === "cited");
  const rest = passages.filter((p: any) => passageState(p) !== "cited");
  const c = k.counts ?? {};
  const scope = k.scope ?? {};
  return <details className="rs knowledge" open={k.status === "failed"}>
    <summary>Knowledge used<span className="count">{c.cited ?? c.used ?? 0} of {c.retrieved ?? 0}</span> <Badge kind={tone}>{label}</Badge></summary>
    <div className="small muted">{k.note}</div>
    {c.eligible !== undefined && <div className="small" aria-label="Passage counts">Retrieved {c.retrieved} · eligible {c.eligible} · given to the model {c.supplied} · cited {c.cited}</div>}
    <div className="small">Searched {scope.environment ? <>environment <span className="mono">{scope.environment}</span></> : "all environments you can read"}
      {scope.namespace ? <>, namespace <span className="mono">{scope.namespace}</span></> : ", every namespace you can read"}
      {scope.target && <>, about <strong>{scope.target}</strong></>}{scope.repository && <> (repository <span className="mono">{scope.repository}</span>)</>}
      {scope.history_used && ", with this conversation's earlier turns"}.</div>
    {!used.length && EMPTY_KNOWLEDGE[k.status] && <div className="alert info small">{EMPTY_KNOWLEDGE[k.status]}</div>}
    {k.reasons?.length > 0 && <ul className="small">{k.reasons.map((r: string, i: number) => <li key={i}>{r}</li>)}</ul>}
    {k.citations?.rejected?.length > 0 && <div className="small muted">Citations not accepted: {k.citations.rejected.map((r: any) => `${r.id} (${r.reason})`).join("; ")}</div>}
    {k.hints?.length > 0 && <div className="small"><strong>Why the index may hold less than expected:</strong><ul>{k.hints.map((h: string, i: number) => <li key={i}>{h}</li>)}</ul></div>}
    {k.queries?.length > 0 && <details className="small"><summary>Queries ({k.queries.length})</summary>
      <ul>{k.queries.map((q: any, i: number) => <li key={i}><span className="mono">{q.query}</span> <Badge kind={q.status === "ok" ? "neutral" : q.status === "failed" ? "bad" : "warn"}>{q.stage} · {q.status === "ok" ? `${q.results} result(s)` : q.status.replace("_", " ")}</Badge>
        {q.mode && <span className="muted"> {q.mode}{q.fallback ? ` (${q.fallback})` : ""}</span>}{q.reused && <span className="muted"> · reused after a restart</span>}{q.reason && <div className="muted">{q.reason}</div>}</li>)}</ul></details>}
    {used.length > 0 && <><div className="small"><strong>Cited in this answer</strong></div><ul className="passages">{used.map((p: any) => <Passage key={p.chunk_id} p={p} onCite={onCite} investigationId={investigationId} />)}</ul></>}
    {rest.length > 0 && <details className="small"><summary>Retrieved, not cited ({rest.length})</summary><ul className="passages">{rest.map((p: any) => <Passage key={p.chunk_id} p={p} onCite={onCite} />)}</ul></details>}
  </details>;
}

/** E3.2 is passage 2 of the search stored as evidence E3: the button names the passage and opens the evidence. */
export function Cites({ refs, onCite }: { refs: string[]; onCite: (r: string) => void }) {
  return <>{refs.map((r) => <button type="button" key={r} className="cite" onClick={() => onCite(r.replace(/\.\d+$/, ""))}>{r}</button>)}</>;
}

const CONF: Record<string, "bad" | "warn" | "neutral"> = { high: "bad", medium: "warn", low: "neutral" };

function Section({ title, count, open = true, children }: { title: string; count?: number; open?: boolean; children: React.ReactNode }) {
  return <details className="rs" open={open}><summary>{title}{count !== undefined && <span className="count">{count}</span>}</summary>{children}</details>;
}

export function Report({ report, evidence = [], hideSummary, onCite, onPropose, investigationId }: { report: any; evidence?: any[]; hideSummary?: boolean; onCite: (r: string) => void; onPropose?: (r: any) => void; investigationId?: string }) {
  if (GUIDED.has(report.response_type) || report.response_type === "conceptual") {
    return <div className="report">
      <Routing report={report} />
      {!hideSummary && <div className="rs static"><h3>Answer</h3><Markdown text={report.summary ?? ""} /><div className="small muted">{report.generated_by === "llm" ? "Organized by the LLM; every command is checked against the cited sources" : "Extracted from the sources by rules"}</div></div>}
      <Answer report={report} onCite={onCite} />
      <KnowledgeUsed k={report.knowledge} onCite={onCite} investigationId={investigationId} />
      {report.observed_facts?.length > 0 && <Section title="Current state" count={report.observed_facts.length}><ul>{report.observed_facts.map((f: any, i: number) => <li key={i}>{f.statement}<Cites refs={f.evidence} onCite={onCite} /></li>)}</ul></Section>}
      {evidence.length > 0 && <Section title="Supporting evidence" count={evidence.length} open={false}>
        <ul>{evidence.slice(0, 40).map((e) => <li key={e.ref}><button type="button" className="cite" style={{ marginLeft: 0 }} onClick={() => onCite(e.ref)}>{e.ref}</button> <span className="small">{e.summary}</span></li>)}</ul>
      </Section>}
      {report.notes?.length > 0 && <details><summary className="small">Notes ({report.notes.length})</summary><ul className="small">{report.notes.map((n: string, i: number) => <li key={i}>{n}</li>)}</ul></details>}
    </div>;
  }
  return (
    <div className="report">
      <Routing report={report} />
      {!hideSummary && <div className="rs static"><h3>Summary</h3><Markdown text={report.summary ?? ""} /><div className="small muted">{report.generated_by === "llm" ? "Written by the LLM from cited evidence" : "Rule-based report"}</div></div>}
      {report.missing_evidence.length > 0 && <div className="rs missing"><strong>Missing evidence</strong><ul>{report.missing_evidence.map((m: string, i: number) => <li key={i}>{m}</li>)}</ul></div>}
      <Section title="Observed facts" count={report.observed_facts.length}>
        {report.observed_facts.length ? <ul>{report.observed_facts.map((f: any, i: number) => <li key={i}>{FACT_KINDS[f.kind] && <><Badge kind="neutral">{FACT_KINDS[f.kind]}</Badge> </>}{f.statement}<Cites refs={f.evidence} onCite={onCite} />
          {f.qualifiers?.map((q: string) => <div key={q} className="small muted">{q}</div>)}</li>)}</ul>
          : <div className="muted small">No facts with supporting evidence.</div>}
      </Section>
      {(report.conflicts ?? []).length > 0 && <div className="rs missing"><strong>Sources disagree</strong><ul>{report.conflicts.map((c: any, i: number) => <li key={i}>{c.detail}<Cites refs={c.evidence} onCite={onCite} /></li>)}</ul></div>}
      {VERIFIED.map(([category, title, note]) => {
        const rows = (report.verified_findings ?? report.verified_causes?.map((c: any) => ({ ...c, category: "configuration_defect" })) ?? []).filter((v: any) => v.category === category);
        return rows.length > 0 && <Section key={category} title={title} count={rows.length}>
          <div className="small muted">{note}</div>
          <ul>{rows.map((c: any, i: number) => <li key={i}>{c.cause}<Cites refs={c.evidence} onCite={onCite} /><div className="small muted">{[c.check, c.target && `target ${c.target}`, c.result, c.at].filter(Boolean).join(" · ")}</div></li>)}</ul>
        </Section>;
      })}
      {report.shared_findings?.length > 0 && <Section title="Shared infrastructure" count={report.shared_findings.length}>
        <div className="small muted">Outside the target, included because the target fails for the same reason.</div>
        <ul>{report.shared_findings.map((f: any, i: number) => <li key={i}>{f.title}<Cites refs={f.evidence ?? []} onCite={onCite} /><div className="small muted">{f.detail}</div></li>)}</ul></Section>}
      {report.excluded?.length > 0 && <div className="small muted">Left out as unrelated to this request: {report.excluded.map((x: any) => `${x.count} ${x.reason}`).join("; ")} (still in the evidence).</div>}
      <Section title="Suspected causes" count={report.suspected_causes.length}>
        {report.suspected_causes.length ? (
          <ul>{report.suspected_causes.map((c: any, i: number) => (
            <li key={i}><Badge kind={CONF[c.confidence]}>{c.confidence} confidence</Badge> {c.cause}<Cites refs={c.evidence} onCite={onCite} />
              {c.contradicting?.length > 0 && <div className="small muted">Evidence against this: <Cites refs={c.contradicting} onCite={onCite} /></div>}
              {c.reasoning && <div className="small muted">{c.reasoning}</div>}
              <div className="small muted">{c.origin === "demoted_fact" ? "Drafted as a fact, but the evidence could not show it. Treat it as a hypothesis." : "Suspected, not verified."}</div></li>
          ))}</ul>
        ) : <div className="muted small">No cause suggested.</div>}
      </Section>
      {evidence.length > 0 && <Section title="Supporting evidence" count={evidence.length} open={false}>
        <ul>{evidence.slice(0, 40).map((e) => <li key={e.ref}><button type="button" className="cite" style={{ marginLeft: 0 }} onClick={() => onCite(e.ref)}>{e.ref}</button> <span className="small">{e.summary}</span></li>)}</ul>
      </Section>}
      {report.next_steps.length > 0 && <Section title="Recommended next steps" count={report.next_steps.length}>
        <ul>{report.next_steps.map((m: string, i: number) => <li key={i}><Markdown text={m} /></li>)}</ul></Section>}
      <KnowledgeUsed k={report.knowledge} onCite={onCite} />
      {report.skill_actions?.length > 0 && <Section title="Suggested actions from skills" count={report.skill_actions.length} open={false}>
        <ul>{report.skill_actions.map((a: any, i: number) => (
          <li key={i}><span className="mono">{a.action}</span>{a.target_name && <span className="mono small"> · {a.target_kind}/{a.target_name}</span>}
            <span className="small muted"> — {a.note} (finding: {a.finding}; skill {a.skill})</span>
            {a.evidence_complete === false && <Badge kind="warn">skill evidence incomplete</Badge>}
            {onPropose && <button type="button" className="ghost small" style={{ marginLeft: 8 }} onClick={() => onPropose({ ...a, fromSkill: true })}>Propose as action plan</button>}</li>
        ))}</ul>
      </Section>}
      {report.proposed_remediation.length > 0 && <Section title="Proposed remediation" count={report.proposed_remediation.length}>
        <ul>{report.proposed_remediation.map((r: any, i: number) => (
          <li key={i}><strong>{r.action}</strong>{r.target && <span className="mono small"> · {r.target}</span>}
            {onPropose && <button type="button" className="ghost small" style={{ marginLeft: 8 }} onClick={() => onPropose(r)}>Propose as action plan</button>}
            <div className="small muted">{[r.expected_impact && `Impact: ${r.expected_impact}`, r.risk && `Risk: ${r.risk}`, r.rollback && `Rollback: ${r.rollback}`].filter(Boolean).join(" · ")}</div>
          </li>
        ))}</ul>
        <div className="small muted proposal-note">Proposals only: nothing was changed. Turn one into a reviewed action plan to execute it (approval required in production).</div>
      </Section>}
      {report.withheld_findings?.length > 0 && <div className="alert warn small">Not reported as verified, because required evidence is missing: {report.withheld_findings.map((w: any) => `${w.cause} (${w.reason})`).join("; ")}</div>}
      {report.skill_runs?.length > 0 && <Section title="What each skill did" count={report.skill_runs.length} open={false}>
        {report.skill_runs.map((r: any) => <div key={r.skill} className="stack tight skill-run">
          <div className="small"><strong>{r.skill}</strong> <span className="muted">v{r.version} · <span className="mono">{r.digest?.slice(0, 12)}</span> · {r.reasons.join("; ")}</span></div>
          {r.outcome && <div><Badge kind={r.outcome === "complete" ? "ok" : r.outcome === "not_applicable" ? "neutral" : "warn"}>{r.outcome.replace("_", " ")}</Badge></div>}
          {r.not_run && <div className="small muted">Did not run: {r.not_run}</div>}
          {r.stopped && <div className="alert warn small">Stopped during this run: {r.stopped}{r.escalation ? `. ${r.escalation}` : ""}</div>}
          {r.gaps?.map((g: string) => <div key={g} className="alert warn small">{g}</div>)}
          <ul className="small">{r.steps.map((s: any, i: number) => <li key={i}><span className="mono">{s.tool}</span> <Badge kind={s.status === "executed" ? "ok" : s.status === "failed" ? "bad" : "neutral"}>{s.status}</Badge>{s.evidence.length > 0 && <Cites refs={[...new Set<string>(s.evidence)]} onCite={onCite} />} <span className="muted">{s.reason ?? s.errors[0] ?? ""}</span></li>)}</ul>
          {r.checks?.filter((c: any) => c.status !== "guidance_only").map((c: any, i: number) => <div key={i} className="small"><Badge kind={c.status === "passed" ? "ok" : c.status === "failed" ? "bad" : "neutral"}>{c.status.replace(/_/g, " ")}</Badge> {c.text}</div>)}
          {r.hypotheses?.map((h: any, i: number) => <div key={i} className="small"><Badge kind={h.status === "contradicted" ? "warn" : "neutral"}>{h.status}</Badge> {h.statement}</div>)}
        </div>)}
        {report.skill_rejected?.length > 0 && <div className="small muted">Not used: {report.skill_rejected.map((r: any) => `${r.skill} (${r.reason})`).join("; ")}</div>}
      </Section>}
      <SimilarIncidents s={report.similar_incidents} investigationId={investigationId} />
      <SkillRecommendations rec={report.skill_recommendations} investigationId={investigationId} />
      {report.skills?.length > 0 && <div className="small" style={{ marginTop: 8 }}>Skills applied: {report.skills.map((s: any) => (
        <Link key={s.name} to="/skills" title={s.reasons.join("; ")}><Badge kind="info">{s.name}</Badge></Link>))}</div>}
      {report.notes?.length > 0 && <details><summary className="small">Investigation notes ({report.notes.length})</summary><ul className="small">{report.notes.map((n: string, i: number) => <li key={i}>{n}</li>)}</ul></details>}
      {report.validation?.uncited_facts_dropped > 0 && <div className="small muted">{report.validation.uncited_facts_dropped} statement(s) with no usable citation were removed.</div>}
      {report.validation?.summary_sentences_removed?.length > 0 && <div className="small muted">{report.validation.summary_sentences_removed.length} sentence(s) of the drafted summary went beyond the evidence and were removed.</div>}
      {report.validation?.method && <div className="small muted">Claims were checked by rules: evidence type, scope, freshness and coverage. That does not prove a passage supports a claim.</div>}
      {report.execution?.resumed_after && <div className="small muted">This run resumed after {report.execution.resumed_after} (attempt {report.execution.attempt}); older evidence is labeled historical.</div>}
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

export function Timeline({ events }: { events: Ev[] }) {
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

export function EvidenceBody({ e, grafana }: { e: any; grafana?: string | null }) {
  return (
    <div className="stack">
      <h2 style={{ margin: 0 }}>{e.ref} · <span className="mono">{e.tool}</span></h2>
      <dl className="kv">
        <dt>Summary</dt><dd>{e.summary}</dd>
        <dt>Source</dt><dd className="mono small">{e.tool}</dd>
        <dt>Collected</dt><dd>{new Date(e.collected_at).toLocaleString(undefined, { timeZoneName: "short" })} ({e.duration_ms} ms)</dd>
        <dt>Environment</dt><dd className="mono small">{e.scope.environment}</dd>
        <dt>Namespace</dt><dd className="mono small">{e.scope.namespace ?? "cluster"}</dd>
        <dt>Window</dt><dd className="small">{new Date(e.scope.from).toLocaleTimeString()}–{new Date(e.scope.to).toLocaleTimeString()}</dd>
        <dt>Arguments</dt><dd className="mono small">{JSON.stringify(e.args)}</dd>
        {e.truncated && <><dt>Note</dt><dd>Result truncated to the size limit.</dd></>}
        {e.error && <><dt>Error</dt><dd className="mono small">{e.error}</dd></>}
      </dl>
      <SourceRef source={e.source} />
      {e.source?.query && <div><CopyButton text={e.source.query} label="Copy query" /></div>}
      {grafana && <a className="small" href={grafana} target="_blank" rel="noreferrer noopener">Open Grafana ↗</a>}
      {e.ok && <EvidenceData tool={e.tool} data={e.data} />}
    </div>
  );
}

/** Ranking detail stays out of the report. It is here for anyone already allowed to open this evidence. */
function RankingDetail({ source }: { source: any }) {
  const r = source?.reranking;
  if (!r) return null;
  const rows = Object.values(source.ranking ?? {}) as any[];
  return (
    <details className="small"><summary>Ranking detail</summary>
      <div>{r.state === "applied" ? `Reranked ${r.candidates} passages with ${r.model} in ${r.inference_ms} ms.` : `Existing order kept (${String(r.reason).replace(/_/g, " ")}).`}{r.truncated_passages ? ` ${r.truncated_passages} passages were only partly scored.` : ""}</div>
      {rows.length > 0 && <ul>{rows.slice(0, 12).map((x, i) => <li key={i}>position {x.before} to {x.after}{x.pinned ? ", exact identifier kept ahead" : ""}{x.partially_scored ? ", partly scored" : ""}</li>)}</ul>}
      <div className="muted">{r.note}</div>
    </details>
  );
}

export function EvidenceDrawer({ e, onClose, grafana }: { e: any; onClose: () => void; grafana?: string | null }) {
  return <Drawer title={`Evidence ${e.ref}`} onClose={onClose}><EvidenceBody e={e} grafana={grafana} /><RankingDetail source={e.source} /></Drawer>;
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
