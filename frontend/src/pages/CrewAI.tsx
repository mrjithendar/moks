import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";
import { Badge, ErrorBox } from "../components";

// The opt-in CrewAI workload investigation (docs/crewai-investigation.md). Shown only when
// /investigate/config reports it available, so with the flag off none of this renders.

export type CrewCapability = { enabled: boolean; available: boolean; reason: string | null; version: string | null; llm?: string;
  limits: { timeout_seconds: number; max_tool_calls: number; max_iterations_per_agent: number } };

export function CrewAIForm({ capability, environments, minutes }: { capability: CrewCapability; environments: string[]; minutes: string }) {
  const nav = useNavigate();
  const [environment, setEnvironment] = useState(environments[0] ?? "");
  const [namespace, setNamespace] = useState("");
  const [workload, setWorkload] = useState("");
  const [kind, setKind] = useState("Deployment");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>();

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const inv = await api<{ conversation_id: string }>("/investigations/crewai", {
        method: "POST", body: { environment: environment || undefined, namespace, workload, kind, minutes: minutes ? Number(minutes) : undefined } });
      nav(`/conversations/${inv.conversation_id}`);
    } catch (err) {
      setError(err);
      setBusy(false);
    }
  }

  return (
    <form className="card stack" onSubmit={submit} aria-label="CrewAI investigation">
      <div className="small muted">
        Three agents (workload, changes, review) investigate one workload with read-only tools, using {capability.llm}. Every finding cites recorded evidence.
        Limits: {capability.limits.timeout_seconds}s, {capability.limits.max_tool_calls} tool calls, {capability.limits.max_iterations_per_agent} iterations per agent.
      </div>
      <div className="filters">
        <label>Environment<select value={environment} onChange={(e) => setEnvironment(e.target.value)}>{environments.map((n) => <option key={n}>{n}</option>)}</select></label>
        <label>Namespace<input required pattern="[a-z0-9]([\-a-z0-9]*[a-z0-9])?" maxLength={63} value={namespace} onChange={(e) => setNamespace(e.target.value)} /></label>
        <label>Kind<select value={kind} onChange={(e) => setKind(e.target.value)}><option>Deployment</option><option>StatefulSet</option><option>DaemonSet</option></select></label>
        <label style={{ flex: 1 }}>Workload<input required pattern="[a-z0-9]([\-a-z0-9.]*[a-z0-9])?" maxLength={253} value={workload} onChange={(e) => setWorkload(e.target.value)} placeholder="e.g. payments" /></label>
        <button disabled={busy || !namespace || !workload}>{busy ? "Starting…" : "Run CrewAI investigation"}</button>
      </div>
      <ErrorBox error={error} />
    </form>
  );
}

export function CrewAIPanel({ crew, onCite }: { crew?: any; onCite: (ref: string) => void }) {
  if (!crew) return null;
  const v = crew.validation ?? {};
  return (
    <div className="rs static" aria-label="CrewAI run">
      <h3>CrewAI run <Badge kind={crew.status === "completed" ? "ok" : "warn"}>{crew.status}</Badge> <span className="small muted">crewai {crew.version} · {crew.tool_calls} tool calls</span></h3>
      <div className="row small">{(crew.agents ?? []).map((a: any) => <Badge key={a.agent} kind={a.status === "done" ? "ok" : "warn"}>{a.label}: {a.status}</Badge>)}</div>
      {crew.stopped && <div className="alert warn small">Stopped early: {crew.stopped}. The report covers the evidence collected before that.</div>}
      {(crew.gaps ?? []).length > 0 && <div className="small"><strong>Gaps</strong><ul>{crew.gaps.map((g: any, i: number) => <li key={i}><span className="mono">{g.source}</span>: {g.reason}</li>)}</ul></div>}
      {((v.citations_rejected ?? []).length > 0 || (v.claims_removed ?? []).length > 0 || (v.confidence_lowered ?? []).length > 0) && (
        <div className="small muted">
          Checked by MOKS: {(v.citations_rejected ?? []).length} invented citation(s) rejected{(v.citations_rejected ?? []).length ? ` (${v.citations_rejected.join(", ")})` : ""},{" "}
          {(v.claims_removed ?? []).length} uncited claim(s) removed, {(v.confidence_lowered ?? []).length} confidence level(s) lowered.
        </div>)}
      {(crew.evidence ?? []).length > 0 && <details><summary className="small">Cited evidence excerpts ({crew.evidence.length})</summary>
        <ul className="small">{crew.evidence.map((e: any) => <li key={e.id}><button type="button" className="cite" style={{ marginLeft: 0 }} onClick={() => onCite(e.id)}>{e.id}</button>{" "}
          <span className="mono">{e.source}</span> {e.truncated && <Badge kind="warn">truncated</Badge>}<pre className="mono small">{e.excerpt}</pre></li>)}</ul>
        <div className="small muted">Excerpts are cut from the stored evidence, not written by the model.</div>
      </details>}
    </div>
  );
}
