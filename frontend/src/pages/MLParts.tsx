import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { useAuth } from "../auth";
import { Badge } from "../components";

/** One person's verdict on a similar incident, a passage or a recommendation. Review input: nothing trains on it. */
export function Judge({ kind, target, investigationId, options }: { kind: string; target: string; investigationId?: string; options: [string, string][] }) {
  const { can } = useAuth();
  const [sent, setSent] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  if (!investigationId || !can("ml.feedback")) return null;
  const send = async (verdict: string) => {
    setError(null);
    try {
      await api("/ml/learning/judgments", { method: "POST", body: { target_kind: kind, target_id: target, verdict, investigation_id: investigationId } });
      setSent(verdict);
    } catch (e: any) {
      setError(e?.message ?? "Could not record that");
    }
  };
  return <div className="row tight wrap judge" role="group" aria-label="Your judgment">
    {options.map(([v, label]) => <button key={v} type="button" className={`ghost small${sent === v ? " selected" : ""}`} aria-pressed={sent === v} onClick={() => send(v)}>{label}</button>)}
    {sent && <span className="small muted" role="status">Recorded for review.</span>}
    {error && <span className="small error-text" role="alert">{error}</span>}
  </div>;
}

const EMPTY: Record<string, string> = {
  empty: "No approved incident you can read is similar enough to this one. That does not mean this has never happened: only incidents with an approved postmortem are compared.",
  failed: "The similar-incident lookup failed, so past incidents could not be compared. The rest of this answer is unaffected.",
  not_attempted: "Triage found nothing to compare with past incidents.",
  skipped: "The similar-incident lookup did not run.",
};

/** Approved past incidents like this one: what matched, what differs, how old, and how it ended. Leads, not causes. */
export function SimilarIncidents({ s, investigationId }: { s?: any; investigationId?: string }) {
  if (!s || s.status === "disabled" || s.mode === "shadow") return null;
  const results: any[] = s.results ?? [];
  return <details className="rs similar" open={results.length > 0}>
    <summary>Similar incidents<span className="count">{results.length}</span></summary>
    <div className="small muted">{s.note}</div>
    {!results.length && <div className="alert info small">{EMPTY[s.status] ?? s.reason ?? "Nothing to show."}{s.reason && EMPTY[s.status] ? ` (${s.reason})` : ""}</div>}
    {s.text?.note && <div className="small muted">{s.text.note}</div>}
    <ol className="similar-list">{results.map((r) => {
      const m = r.matching ?? {}, d = r.differences ?? {}, res = r.resolution ?? {};
      return <li key={r.incident_id} className="similar-item">
        <div className="row tight wrap">
          <Link to={`/incidents/${r.incident_id}`}><strong>{r.title ?? "Incident"}</strong></Link>
          <Badge kind="neutral">similarity {r.score}</Badge>
          {r.age_days !== null && r.age_days !== undefined && <span className="small muted">{r.age_days} days ago</span>}
          <span className="small muted mono">{r.environment}/{r.namespace}</span>
          <Badge kind={res.cause_verified ? "ok" : "warn"}>{res.cause_verified ? "cause verified" : "cause not verified"}</Badge>
        </div>
        <dl className="small kv">
          {m.symptoms?.length > 0 && <><dt>Same symptoms</dt><dd>{m.symptoms.join(", ")}</dd></>}
          {m.signature?.length > 0 && <><dt>Same error terms</dt><dd className="mono">{m.signature.join(" ")}</dd></>}
          {m.services?.length > 0 && <><dt>Same service</dt><dd>{m.services.join(", ")}</dd></>}
          {m.change_context && <><dt>Change context</dt><dd>{m.change_context}</dd></>}
          {(d.only_now?.length > 0 || d.only_then?.length > 0 || d.services_then?.length > 0 || d.change_context) && <><dt>Differences</dt><dd>
            {[d.only_now?.length ? `only now: ${d.only_now.join(", ")}` : "", d.only_then?.length ? `only then: ${d.only_then.join(", ")}` : "",
              d.services_then?.length ? `then it was ${d.services_then.join(", ")}` : "", d.change_context ?? ""].filter(Boolean).join("; ")}</dd></>}
          {res.cause && <><dt>Confirmed cause then</dt><dd>{res.cause}</dd></>}
          {res.resolution && <><dt>Resolved by</dt><dd>{res.resolution}</dd></>}
        </dl>
        <Judge kind="similar_incident" target={r.incident_id} investigationId={investigationId} options={[["similar", "Similar"], ["not_similar", "Not similar"], ["unsure", "Unsure"]]} />
      </li>;
    })}</ol>
  </details>;
}

/** What semantic ranking suggested beside the rules. Shadow: shown, not used. */
export function SkillRecommendations({ rec, investigationId }: { rec?: any; investigationId?: string }) {
  if (!rec || !rec.ranked?.length) return null;
  const shadow = rec.mode === "shadow";
  return <details className="rs">
    <summary>Skill recommendations<span className="count">{rec.ranked.length}</span> <Badge kind={shadow ? "neutral" : "info"}>{shadow ? "shadow: not used" : rec.mode}</Badge></summary>
    <div className="small muted">{rec.note} Method: {rec.method === "embedding" ? "embedding similarity" : "term similarity (TF-IDF)"}. Rules chose: {rec.deterministic?.join(", ") || "none"}.</div>
    {rec.added?.length > 0 && <div className="small">Added by semantic ranking: {rec.added.join(", ")}</div>}
    <ol className="small">{rec.ranked.map((r: any) => <li key={r.skill}><span className="mono">{r.skill}</span> <span className="muted">{r.score} · {r.why}</span>
      <Judge kind="skill_recommendation" target={r.skill} investigationId={investigationId} options={[["helpful", "Helpful"], ["not_helpful", "Not helpful"]]} /></li>)}</ol>
  </details>;
}
