import { useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { api, qs } from "../api";
import { useAuth } from "../auth";
import { Badge, ErrorBox, Loading, PageHead, Time, useLoad } from "../components";
import { type Chip, FilterPanel, useUrlFilters } from "../filters";
import { Drawer, Empty, Identifier, useFeedback, useIsMobile } from "../ui";

const CATEGORIES = ["kubernetes", "application", "aws", "devops", "support", "finops"];
const READY: Record<string, "ok" | "warn" | "bad" | "neutral"> = { ready: "ok", partial: "warn", unknown: "neutral", unavailable: "bad" };
const OUTCOME: Record<string, "ok" | "warn" | "bad" | "neutral"> = { complete: "ok", incomplete: "warn", stopped: "warn", not_applicable: "neutral" };
const short = (d?: string) => d?.slice(0, 12) ?? "";
const words = (s: string) => s.replace(/_/g, " ");

function Part({ title, open = false, children }: { title: string; open?: boolean; children: ReactNode }) {
  return <details className="ke-section" open={open}><summary>{title}</summary><div className="stack">{children}</div></details>;
}

/** Shown as enforced only when the server says MOKS checks it, so guidance never reads like a guarantee. */
function Contract({ rows }: { rows: any[] }) {
  const groups = [...new Set(rows.map((r) => r.field))];
  return <>{groups.map((g) => <div key={g} className="stack tight"><div className="small"><strong>{words(g)}</strong></div>
    {rows.filter((r) => r.field === g).map((r, i) => <div key={i} className="ke-contract small"><Badge kind={r.enforced ? "ok" : "neutral"}>{r.enforced ? "enforced" : "guidance"}</Badge><span className="trunc" title={String(r.item)}>{String(r.item)}</span><span className="muted">{r.how}</span></div>)}
  </div>)}</>;
}

function Readiness({ d }: { d: any }) {
  const envs = Object.entries(d.availability as Record<string, any>);
  if (!envs.length) return <div className="small muted">No environment you can read.</div>;
  return <>{envs.map(([env, v]) => <div key={env} className="stack tight">
    <div className="row tight small"><span className="mono">{env}</span><Badge kind={READY[v.state] ?? "neutral"}>{v.state}</Badge></div>
    {v.reasons.map((r: string) => <div key={r} className="small muted">{r}</div>)}
    {v.actions?.map((a: string) => <div key={a} className="small">{a}</div>)}
  </div>)}</>;
}

function Traces({ name }: { name: string }) {
  const runs = useLoad(() => api<any[]>(`/skills/${name}/runs`), [name]);
  if (runs.data && !runs.data.length) return <div className="small muted">No investigation you can read has used this skill.</div>;
  return <><Loading load={runs} rows={2} />{runs.data?.map((r) => <details key={r.investigation} className="ke-chunk">
    <summary><Badge kind={OUTCOME[r.outcome] ?? "neutral"}>{words(r.outcome ?? "unknown")}</Badge> <span className="small"><Time iso={r.created_at} /> · {r.environment}/{r.namespace} · generation {r.generation ?? "none"} · v{r.version}</span></summary>
    <div className="stack tight">
      <Link className="small" to={`/investigations/${r.investigation}`}>Open the investigation</Link>
      {r.not_run && <div className="alert info small">Did not run: {r.not_run}</div>}
      {r.stopped && <div className="alert warn small">Stopped: {r.stopped}{r.escalation ? `. ${r.escalation}` : ""}</div>}
      {r.budget_exhausted && <div className="alert warn small">A budget ran out, so some steps did not run.</div>}
      {r.gaps.map((g: string) => <div key={g} className="alert warn small">{g}</div>)}
      <ol className="small">{r.steps.map((s: any, i: number) => <li key={i}><span className="mono">{s.id ?? s.tool}</span> <Badge kind={s.status === "executed" ? "ok" : s.status === "failed" ? "bad" : "neutral"}>{words(s.status)}</Badge> <span className="muted">{s.reason ?? s.errors?.[0] ?? ""}</span></li>)}</ol>
      {r.checks.filter((c: any) => c.status !== "guidance_only").map((c: any, i: number) => <div key={i} className="small"><Badge kind={c.status === "passed" ? "ok" : c.status === "failed" ? "bad" : "neutral"}>{words(c.status)}</Badge> {c.text}</div>)}
      {r.hypotheses.map((h: any, i: number) => <div key={i} className="small"><Badge kind={h.status === "contradicted" ? "warn" : "neutral"}>{h.status}</Badge> {h.statement}</div>)}
    </div></details>)}</>;
}

function Diff({ name, base, head, onClose }: { name: string; base: string; head: string; onClose: () => void }) {
  const d = useLoad(() => api<any>(`/skills/${name}/diff${qs({ base, head })}`), [name, base, head]);
  return <Drawer title={`Compare ${name}`} onClose={onClose}><Loading load={d} rows={6} /><ErrorBox error={d.error} />
    {d.data && <><div className="small muted">v{d.data.from} to v{d.data.to}</div>
      {d.data.lines.length ? <pre className="logbox ke-diff" aria-label="Changes">{d.data.lines.map((l: string, i: number) => <span key={i} className={l.startsWith("+") ? "add" : l.startsWith("-") ? "del" : ""}>{l}{"\n"}</span>)}</pre> : <Empty title="These versions are identical." />}</>}</Drawer>;
}

function Waiting({ v, onDone }: { v: any; onDone: () => void }) {
  const { can } = useAuth();
  const { toast, confirm } = useFeedback();
  const [note, setNote] = useState("");
  const [check, setCheck] = useState<any>(null);
  const post = (path: string, body: any) => api(`/skills/${v.skill}/${path}`, { method: "POST", body });
  const validate = () => post("validate", { digest: v.digest }).then(setCheck, (e) => toast(e.message, "bad"));
  const review = () => post("review", { digest: v.digest, note }).then(() => { toast("Reviewed. Publishing is a separate step."); onDone(); }, (e) => toast(e.message, "bad"));
  async function publish() {
    if (!await confirm({ title: `Publish ${v.skill} v${v.version}?`, body: "Every process moves to the new generation within a minute. Investigations already running keep the version they pinned.", action: "Publish" })) return;
    post("publish", { digest: v.digest, note }).then(() => { toast("Published."); onDone(); }, (e) => toast(e.message, "bad"));
  }
  return (
    <details className="ke-section"><summary><strong>{v.skill}</strong> v{v.version} <Badge kind={v.state === "draft" ? "bad" : v.reviewed_at ? "ok" : "warn"}>{v.state === "draft" ? "failed validation" : v.reviewed_at ? "reviewed" : "needs review"}</Badge> <span className="small muted">{v.source} · {v.owner}</span></summary>
      <div className="stack">
        <div className="small mono muted">{short(v.digest)}</div>
        {v.validation.map((i: any, n: number) => <div key={n} className={`small ${i.level === "error" ? "" : "muted"}`}>{i.level}: {i.where}: {i.message}</div>)}
        {v.state !== "draft" && <>
          <button type="button" className="ghost small" onClick={validate}>Check the whole generation</button>
          {check && <div className="small"><Badge kind={check.ok ? "ok" : "bad"}>{check.ok ? "this generation would be valid" : "this generation would be refused"}</Badge> {check.skills} skills{check.replaces ? `, replacing ${short(check.replaces)}` : ""}
            {check.errors.map((e: any, i: number) => <div key={i}>{e.skill}: {e.message}</div>)}{check.warnings.map((e: any, i: number) => <div key={i} className="muted">{e.message}</div>)}</div>}
          <label>Note<input value={note} onChange={(e) => setNote(e.target.value)} placeholder="What you checked" /></label>
          <div className="row">
            {can("skills.review") && !v.reviewed_at && <button type="button" className="ghost" disabled={note.length < 3} onClick={review}>Mark reviewed</button>}
            {can("skills.publish") && <button type="button" disabled={!v.reviewed_at} onClick={publish}>Publish</button>}
          </div>
        </>}
      </div>
    </details>
  );
}

export function Skills() {
  const { can } = useAuth();
  const { toast, confirm } = useFeedback();
  const list = useLoad(() => api<any>("/skills"), []);
  const [open, setOpen] = useState<string | null>(null);
  const [compare, setCompare] = useState<{ base: string; head: string } | null>(null);
  const [{ category, q: text, state, source }, setF, clearF] = useUrlFilters({ category: "", q: "", state: "", source: "" });
  const phone = useIsMobile();
  const chips: Chip[] = [...(category ? [{ key: "c", label: category, onRemove: () => setF({ category: "" }) }] : []), ...(state ? [{ key: "s", label: `readiness ${state}`, onRemove: () => setF({ state: "" }) }] : []), ...(source ? [{ key: "o", label: source, onRemove: () => setF({ source: "" }) }] : []), ...(text ? [{ key: "q", label: `matches ${text}`, onRemove: () => setF({ q: "" }) }] : [])];
  const detail = useLoad(() => (open ? api<any>(`/skills/${open}`) : Promise.resolve(null)), [open]);
  const generation = useLoad(() => api<any>("/skills/generation"), []);
  const canReview = can("skills.review");
  const registry = useLoad(() => (canReview ? api<any>("/skills/registry").catch(() => null) : Promise.resolve(null)), [canReview]);
  const refresh = () => { list.reload(); detail.reload(); registry.reload(); generation.reload(); };
  const waiting: any[] = (registry.data?.versions ?? []).filter((v: any) => v.state === "validated" || v.state === "draft");
  const statesOf = (s: any) => Object.values(s.availability as Record<string, any>).map((v) => v.state as string);
  const rows = (list.data?.skills ?? []).filter((s: any) => (!category || s.category === category) && (!source || s.source === source) && (!state || statesOf(s).includes(state)) && JSON.stringify([s.name, s.title, s.description, s.owner]).toLowerCase().includes(text.toLowerCase()));
  const d = detail.data;
  async function rollback(digest: string, version: string) {
    if (!await confirm({ title: `Roll ${d.name} back to v${version}?`, body: "This makes a new generation. History keeps both.", action: "Roll back", danger: true })) return;
    api(`/skills/${d.name}/rollback`, { method: "POST", body: { digest, note: "from the Skills page" } }).then(() => { toast("Rolled back."); refresh(); }, (e) => toast(e.message, "bad"));
  }
  const toggle = (s: any) => api(`/skills/${s.name}/policy`, { method: "PUT", body: { enabled: !s.enabled } }).then(refresh, (e) => toast(e.message, "bad"));
  const body = d && <>
    <div className="row between">{!phone && <h2 className="trunc" title={d.title}>{d.title}</h2>}{can("skills.publish") && <button className="ghost small" onClick={() => toggle(d)}>{d.enabled ? "Turn off" : "Turn on"}</button>}</div>
    <p className="muted" style={{ margin: 0 }}>{d.description}</p>
    <div className="row small"><Badge kind={d.state === "published" ? "ok" : "warn"}>{d.state}</Badge><Badge>v{d.version}</Badge><Badge>{d.source}</Badge>{!d.enabled && <Badge kind="warn">off</Badge>}<span className="muted">owner {d.owner}</span></div>
    <Identifier name={d.digest} what="digest" />
    <div className="small">Applies to: {d.environments.join(", ")}{d.policy_environments.length > 0 && ` (limited by policy to ${d.policy_environments.join(", ")})`}{d.resource_types.length > 0 && ` · ${d.resource_types.join(", ")}`}</div>
    <div className="small">Needs: {d.needs.join(", ") || "nothing beyond MOKS"}{d.telemetry.length > 0 && ` · metrics ${d.telemetry.join(", ")}`}{d.permissions.length > 0 && ` · permissions ${d.permissions.join(", ")}`}</div>
    <Part title="Readiness" open><Readiness d={d} /></Part>
    <Part title="Steps and evidence contract" open>
      <div className="small muted">Each line says whether MOKS checks it. Skill confidence never authorizes a change.</div>
      <Contract rows={d.enforced} />
      {d.steps.length > 0 && <ol className="small">{d.steps.map((s: any, i: number) => <li key={i}><span className="mono">{s.tool}</span> {s.required && <Badge kind="info">required</Badge>} {s.for_each && <Badge>{s.for_each}</Badge>} {s.purpose}
        {(s.requires_finding || s.unless_finding || s.if_step || s.stop_if_finding) && <div className="muted">{s.requires_finding ? `only with ${s.requires_finding}. ` : ""}{s.unless_finding ? `only without ${s.unless_finding}. ` : ""}{s.if_step ? `only when ${s.if_step.step} is ${s.if_step.status.join(" or ")}. ` : ""}{s.stop_if_finding ? `stops on ${s.stop_if_finding}: ${s.escalate}` : ""}</div>}</li>)}</ol>}
    </Part>
    <Part title="Triggers and actions">
      <div className="small">keywords: {d.triggers.keywords.join(", ") || "—"} · findings: {d.triggers.findings.join(", ") || "—"}</div>
      {d.remediation.map((r: any, i: number) => <div key={i} className="small">when <Badge kind="warn">{r.when}</Badge> propose <span className="mono">{r.action}</span> {r.target} <span className="muted">{r.note}</span></div>)}
      {d.remediation.length > 0 && <div className="small muted">Proposals only. Execution stays behind action plans, policy and approvals.</div>}
      {d.detection.map((r: any) => <div key={r.id} className="small"><span className="mono">{r.id}</span> {r.kind} · {r.severity} · every {r.interval_minutes} min</div>)}
    </Part>
    <Part title="Evaluation results">
      {d.evaluation_results?.cases?.length ? d.evaluation_results.cases.map((c: any, i: number) => <div key={i} className="small"><Badge kind={c.passed ? "ok" : "bad"}>{c.passed ? "passed" : "failed"}</Badge> {words(c.kind)} · {c.case} {c.failures.map((f: string) => <div key={f} className="muted">{f}</div>)}</div>)
        : <div className="small muted">{d.evaluation_results ? "The latest evaluation has no case for this skill." : "No skills evaluation has run yet."}</div>}
      {d.evaluation_results && <div className="small muted">Run <Time iso={d.evaluation_results.created_at} />, generation {d.evaluation_results.generation ?? "none"}. Fixtures and no model.</div>}
    </Part>
    <Part title={`Versions (${d.history.length})`}>{d.history.map((h: any) => <div key={h.digest} className="row between small"><span><span>v{h.version}</span> <Badge kind={h.digest === d.digest ? "ok" : "neutral"}>{h.digest === d.digest ? "running" : h.state}</Badge> <span className="mono muted">{short(h.digest)}</span></span>
      <span className="row tight">{h.digest !== d.digest && <button className="ghost small" onClick={() => setCompare({ base: h.digest, head: d.digest })}>Compare</button>}
        {can("skills.publish") && h.digest !== d.digest && ["published", "deprecated"].includes(h.state) && <button className="ghost small" onClick={() => rollback(h.digest, h.version)}>Roll back</button>}</span></div>)}</Part>
    <Part title="Execution traces"><Traces name={d.name} /></Part>
    <Part title="Guidance"><pre className="logbox">{d.body}</pre></Part>
  </>;
  return (
    <>
      <PageHead title="SRE skills" sub="Versioned playbooks. Registering, reviewing, publishing and running a skill are separate steps.">
        {can("skills.publish") && <button className="ghost" onClick={() => api("/skills/reload", { method: "POST" }).then((r) => { toast(`${r.registered.length} registered. Nothing was published.`); refresh(); }, (e) => toast(e.message, "bad"))}>Register from files</button>}
      </PageHead>
      {list.data?.errors?.length > 0 && <div className="alert error" style={{ marginBottom: 12 }}>{list.data.errors.map((e: any) => <div key={e.path} className="small">{e.path}: {e.error}</div>)}</div>}
      {generation.data?.active && <div className="card stack">
        <div className="row between"><span className="small">Skill generation <strong>{generation.data.active.generation}</strong> · {generation.data.active.skills} skills · <span className="mono">{short(generation.data.active.digest)}</span></span>
          <Badge kind={generation.data.consistent ? "ok" : "warn"}>{generation.data.consistent ? "every process current" : "processes differ"}</Badge></div>
        <div className="row small">{generation.data.processes.map((p: any) => <span key={p.instance} title={p.error ?? p.instance}><Badge kind={p.error ? "bad" : p.current ? "ok" : "warn"}>{p.component} {p.generation ?? "none"}</Badge></span>)}</div>
        {generation.data.processes.filter((p: any) => p.error).map((p: any) => <div key={p.instance} className="alert error small">{p.instance} kept its last valid generation: {p.error}</div>)}
      </div>}
      {!generation.data?.active && generation.data && <div className="alert warn small">No generation is published yet. Run <span className="mono">moks skills bootstrap</span>.</div>}
      {canReview && waiting.length > 0 && <div className="card stack mt"><h2>Waiting for review ({waiting.length})</h2>{waiting.map((v) => <Waiting key={v.digest} v={v} onDone={refresh} />)}</div>}
      <div className="mt" />
      <FilterPanel chips={chips} onClear={clearF} search={<input type="search" className="grow" aria-label="Search skills" value={text} onChange={(e) => setF({ q: e.target.value })} placeholder="Search skills" />}>
        <label>Category<select value={category} onChange={(e) => setF({ category: e.target.value })}><option value="">All</option>{CATEGORIES.map((c) => <option key={c}>{c}</option>)}</select></label>
        <label>Readiness<select value={state} onChange={(e) => setF({ state: e.target.value })}><option value="">All</option>{Object.keys(READY).map((c) => <option key={c}>{c}</option>)}</select></label>
        <label>Origin<select value={source} onChange={(e) => setF({ source: e.target.value })}><option value="">All</option><option>builtin</option><option>custom</option><option>proposal</option></select></label>
      </FilterPanel>
      <Loading load={list} rows={8} />
      <div className="small muted mt">{rows.length} skills</div>
      <div className="grid split-2-3 mt">
        <div className="card">
          {!rows.length && list.data && <Empty title="No skills match." />}
          <ul className="rl">{rows.map((s: any) => {
            const blocked = Object.entries(s.availability as Record<string, any>).filter(([, v]) => v.state === "unavailable");
            return <li key={s.name} className={`rl-row${open === s.name ? " active" : ""}`}><button type="button" className="rl-main" onClick={() => setOpen(s.name)}>
              <span className="rl-top"><span className="rl-name"><strong className="trunc" title={s.title}>{s.title}</strong></span><span className="rl-status"><Badge>{s.category}</Badge>{s.source !== "builtin" && <Badge kind="info">{s.source}</Badge>}{!s.enabled && <Badge kind="warn">off</Badge>}</span></span>
              <span className="small muted clamp-2">{s.description}</span>
              {blocked.length > 0 && <span className="small muted clamp-2">Unavailable in {blocked.map(([e]) => e).join(", ")}: {(blocked[0][1] as any).reasons.join("; ")}</span>}
            </button></li>;
          })}</ul>
        </div>
        {phone ? (open && d && <Drawer title={d.title} onClose={() => setOpen(null)}><div className="stack">{body}</div></Drawer>) : <div className="card stack">{d ? body : <Empty title="Select a skill." />}</div>}
      </div>
      {compare && d && <Diff name={d.name} base={compare.base} head={compare.head} onClose={() => setCompare(null)} />}
    </>
  );
}
