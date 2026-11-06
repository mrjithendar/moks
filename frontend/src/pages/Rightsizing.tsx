import { useId, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "../auth";
import { Badge } from "../components";
import { ACTION_LABEL, capacityText, changed, CSV_COLUMNS, diffLines, FIELDS, type Pricing, proposeBlocker, RESOURCE_LABEL, type Rec, recsOf, resizePlan, type Row, savingText } from "../rightsizing";
import { Drawer, Identifier } from "../ui";
import { PlanForm } from "./Ops";

const TONE: Record<Rec["action"], "warn" | "ok" | "info"> = { increase: "warn", reduce: "ok", set_requests: "info" };
const ARROW: Record<Rec["action"], string> = { increase: "↑", reduce: "↓", set_requests: "+" };

export function rightsizingCsvRows(rows: Row[], pricing: (env: string) => Pricing | null | undefined) {
  return rows.flatMap((r) => recsOf(r).map((rec) => ({ r, rec, pricing: pricing(r.environment) })));
}
export { CSV_COLUMNS as RIGHTSIZING_CSV };

function ActionBadge({ rec }: { rec: Rec }) {
  return <Badge kind={TONE[rec.action]}><span aria-hidden="true">{ARROW[rec.action]} </span>{RESOURCE_LABEL[rec.resource]}: {ACTION_LABEL[rec.action]}</Badge>;
}

/** All four values of a container; only the ones a recommendation changes are highlighted, each with its reason. */
function Fields({ r, recs }: { r: Row; recs: Rec[] }) {
  return (
    <dl className="rs-fields">
      {FIELDS.map((f) => {
        const c = recs.map((rec) => changed(rec, f.key)).find(Boolean);
        return (
          <div key={f.key} className={`rs-field${c ? " rs-changed" : ""}`}>
            <dt>{f.label}</dt>
            <dd className="mono">{c ? <><span>{c.current ?? "unset"}</span> <span aria-hidden="true">→</span><span className="sr-only"> changes to </span> <strong>{c.recommended}</strong></> : r.current[f.key] ?? <span className="muted">unset</span>}</dd>
            {c ? <dd className="small rs-reason"><span className="rs-dir">{c.direction === "set" ? "Set" : c.direction === "increase" ? "Increase" : "Decrease"}:</span> {c.reason}</dd> : <dd className="small muted">No change</dd>}
          </div>
        );
      })}
    </dl>
  );
}

function Preview({ r, rec, days, pricing, onClose }: { r: Row; rec: Rec; days: number; pricing?: Pricing | null; onClose: () => void }) {
  const { can } = useAuth();
  const nav = useNavigate();
  const [proposing, setProposing] = useState(false);
  const why = proposeBlocker(r, can);
  const whyId = useId();
  const money = savingText(rec, pricing);
  return (
    <Drawer title={`Proposal preview: ${RESOURCE_LABEL[rec.resource]}`} onClose={onClose}>
      <div className="stack">
        <section aria-label="Target">
          <h3>Target</h3>
          <div className="mono small">{r.environment}/{r.namespace}</div>
          <Identifier name={`${r.workload}/${r.container}`} what="container" />
          <div className="small muted">{r.replicas} replica{r.replicas === 1 ? "" : "s"}</div>
        </section>
        <section aria-label="Patch">
          <h3>Exact patch</h3>
          <pre className="rs-diff" data-testid="rs-diff">{diffLines(r, rec).map((l) => `- ${l.path}: ${l.from}\n+ ${l.path}: ${l.to}`).join("\n")}</pre>
          <div className="small muted">Only these fields change. The other values stay as they are.</div>
        </section>
        <section aria-label="Expected impact">
          <h3>Expected impact</h3>
          <ul className="small">
            <li>{capacityText(rec)[0].toUpperCase() + capacityText(rec).slice(1)} across {r.replicas} replica{r.replicas === 1 ? "" : "s"}.</li>
            {money ? <li><Badge kind="info">Estimate</Badge> {money}, from the configured unit prices.</li>
              : <li>Fixed price{pricing?.description ? ` (${pricing.description})` : ""}: the bill does not change. This frees or reserves capacity.</li>}
            <li>Changing a {r.workload.split("/")[0]}'s resources rolls out new pods.</li>
          </ul>
        </section>
        <section aria-label="Approval">
          <h3>Approval</h3>
          <div className="small">Not proposed. Propose creates an action plan in Approvals. Nothing changes until it is approved and run.</div>
        </section>
        {why && <div className="alert warn small" id={whyId} role="note"><strong>Propose is unavailable.</strong> {why}</div>}
        {!proposing && <div className="row end"><button type="button" disabled={!!why} aria-describedby={why ? whyId : undefined} onClick={() => setProposing(true)}>Propose</button></div>}
        {proposing && !why && <PlanForm env={r.environment} ns={r.namespace} initial={resizePlan(r, rec, days)} onDone={(id) => nav(`/actions/${id}`)} />}
      </div>
    </Drawer>
  );
}

export function RightsizingList({ rows, days, pricing }: { rows: Row[]; days: number; pricing: (env: string) => Pricing | null | undefined }) {
  const [open, setOpen] = useState<{ r: Row; rec: Rec } | null>(null);
  return (
    <>
      <ul className="rs-list" aria-label="Rightsizing recommendations">
        {rows.map((r) => {
          const recs = recsOf(r);
          const p = pricing(r.environment);
          return (
            <li key={`${r.environment}/${r.namespace}/${r.workload}/${r.container}`} className="rs-item" data-testid="rs-item">
              <div className="rs-head">
                <Identifier name={`${r.workload}/${r.container}`} what="container" />
                <span className="mono small muted">{r.environment}/{r.namespace} · {r.replicas} replica{r.replicas === 1 ? "" : "s"}</span>
                {r.confidence === "low" && <Badge kind="warn">Low confidence: {Math.round(r.coverage * 100)}% of the window measured</Badge>}
              </div>
              <Fields r={r} recs={recs} />
              <ul className="rs-recs">
                {recs.map((rec) => (
                  <li key={rec.resource} className="rs-rec">
                    <ActionBadge rec={rec} />
                    <span className="small">{capacityText(rec)}</span>
                    {savingText(rec, p) && <span className="small"><Badge kind="info">Estimate</Badge> {savingText(rec, p)}</span>}
                    <button type="button" className="ghost small" onClick={() => setOpen({ r, rec })} aria-label={`Review the ${RESOURCE_LABEL[rec.resource]} proposal for ${r.workload}/${r.container}`}>Review proposal</button>
                  </li>
                ))}
              </ul>
              {r.findings.length > 0 && <div className="small muted">Observed: {r.findings.join("; ")}. p95 CPU {r.observed.cpu_p95} cores, max memory {r.observed.mem_max}.</div>}
            </li>
          );
        })}
      </ul>
      {open && <Preview r={open.r} rec={open.rec} days={days} pricing={pricing(open.r.environment)} onClose={() => setOpen(null)} />}
    </>
  );
}
