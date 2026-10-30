import { memo, useCallback, useEffect, useLayoutEffect, useRef, useState, type FormEvent, type KeyboardEvent, type ReactNode } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api } from "../api";
import { useAuth } from "../auth";
import { Badge, ErrorBox, Time, useInterval, useLoad } from "../components";
import { useFollow } from "../follow";
import { type Ev, type StreamState, useInvestigationEvents } from "../investigationStream";
import { Markdown } from "../markdown";
import { useHeaderScope } from "../shell";
import { Dialog, useFeedback, useIsMobile, useModal } from "../ui";
import { EvidenceBody, Report, Timeline } from "./Investigate";
import { PlanForm } from "./Ops";

const TERMINAL = new Set(["completed", "failed", "cancelled"]);
const PAGE = 40;

/** One short line for a person watching. The full tool activity stays in the collapsible timeline. */
export function progressLabel(events: Ev[]): string {
  const last = [...events].reverse().find((e) => ["phase", "tool_call", "queued", "report"].includes(e.type));
  if (!last) return "Starting";
  if (last.type === "queued") return "Waiting for a worker";
  if (last.type === "report") return "Writing the report";
  if (last.type === "phase") return last.label ?? "Working";
  const tool = String(last.tool ?? "");
  if (tool.startsWith("prom")) return "Querying Prometheus";
  if (tool.startsWith("loki") || tool === "k8s_pod_logs") return "Reading logs";
  if (tool === "k8s_events") return "Checking events";
  if (tool.includes("pod") || tool === "k8s_triage") return "Checking pods";
  if (tool.startsWith("k8s")) return "Checking Kubernetes";
  if (tool.startsWith("git") || tool.startsWith("knowledge")) return "Searching knowledge";
  return "Collecting evidence";
}

const STREAM: Partial<Record<StreamState, string>> = { reconnecting: "Reconnecting…", failed: "Live updates stopped. Checking every few seconds.", ended: "Live updates ended. Checking every few seconds." };

export function messageKind(m: { role: string; investigation_id?: string | null; deleted?: boolean }): "engineer" | "moks" | "note" | "system" {
  if (m.role === "assistant") return "moks";
  if (m.role === "user") return m.investigation_id ? "engineer" : "note";
  return "system";
}

// ---- Conversation list -----------------------------------------------------------------------

function ConversationList({ current }: { current?: string }) {
  const { can } = useAuth();
  const [deleted, setDeleted] = useState(false);
  const [search, setSearch] = useState("");
  const list = useLoad(() => api<any[]>(`/conversations${deleted ? "?deleted=true" : ""}`), [deleted]);
  useInterval(() => { if (!list.loading) list.reload(); }, 15000);
  const needle = search.trim().toLowerCase();
  const rows = (list.data ?? []).filter((c) => !needle || `${c.title} ${c.environment} ${c.namespace} ${c.owner}`.toLowerCase().includes(needle));
  return (
    <section className="ws-list" aria-label="Conversations">
      <div className="ws-list-head">
        <div className="row between"><h2 style={{ margin: 0 }}>Conversations</h2>
          {can("investigation.run") && <Link className="btn" to="/investigate">New</Link>}</div>
        <input type="search" aria-label="Search conversations" placeholder="Search" value={search} onChange={(e) => setSearch(e.target.value)} />
        <label className="inline"><input type="checkbox" checked={deleted} onChange={(e) => setDeleted(e.target.checked)} /> Show deleted</label>
      </div>
      <ErrorBox error={list.error} />
      {list.loading && !list.data && <div className="empty">Loading…</div>}
      <ul className="ws-list-items">
        {rows.map((c) => (
          <li key={c.id}>
            <Link to={`/conversations/${c.id}`} className={`ws-item${c.id === current ? " active" : ""}`} aria-current={c.id === current ? "page" : undefined}>
              <div className="ws-item-title">{c.title}</div>
              <div className="small muted row" style={{ gap: 6 }}>
                <span className="mono">{c.environment}/{c.namespace}</span>
                {c.running && <Badge kind="info">running</Badge>}
                <Time iso={c.updated_at} />
              </div>
            </Link>
          </li>
        ))}
      </ul>
      {list.data && !rows.length && <div className="empty">{needle ? "No match." : deleted ? "Nothing deleted." : "No conversations yet."}</div>}
    </section>
  );
}

// ---- Evidence --------------------------------------------------------------------------------

interface PanelState { investigation: string; ref?: string }

function EvidencePanel({ panel, onRef, onClose }: { panel: PanelState; onRef: (r?: string) => void; onClose: () => void }) {
  const mobile = useIsMobile();
  const box = useRef<HTMLElement>(null);
  useModal(box, mobile, onClose); // a sheet on phones; beside the conversation elsewhere
  useEffect(() => {
    if (mobile) return;
    const key = (e: globalThis.KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", key);
    return () => window.removeEventListener("keydown", key);
  }, [mobile, onClose]);
  const inv = useLoad(() => api<any>(`/investigations/${panel.investigation}`), [panel.investigation]);
  const data = inv.data?.id === panel.investigation ? inv.data : undefined;
  const evidence: any[] = data?.evidence ?? [];
  const open = evidence.find((e) => e.ref === panel.ref);
  return (
    <aside ref={box} className="ws-evidence" aria-label="Evidence" tabIndex={-1} {...(mobile ? { role: "dialog", "aria-modal": true } : {})}>
      <div className="ws-evidence-head">
        {open ? <button type="button" className="ghost small" onClick={() => onRef(undefined)}>All evidence</button> : <strong>Evidence ({evidence.length})</strong>}
        <button type="button" className="ghost small" onClick={onClose}>Close</button>
      </div>
      <div className="ws-evidence-body">
        <ErrorBox error={inv.error} />
        {!data && !inv.error && <div className="empty">Loading…</div>}
        {data && !open && !evidence.length && <div className="empty">No evidence collected yet.</div>}
        {!open && evidence.map((e) => (
          <button type="button" key={e.ref} className="ev-row" onClick={() => onRef(e.ref)}>
            <div className="row" style={{ gap: 6 }}><span className="cite" style={{ marginLeft: 0 }}>{e.ref}</span><span className="mono small">{e.tool}</span>{!e.ok && <Badge kind="bad">failed</Badge>}</div>
            <div className="small muted">{e.summary}</div>
          </button>
        ))}
        {open && <EvidenceBody e={open} grafana={data.grafana_url} />}
      </div>
    </aside>
  );
}

// ---- Messages --------------------------------------------------------------------------------

function MessageActions({ m, conversation, onChange }: { m: any; conversation: string; onChange: () => void }) {
  const { confirm, toast } = useFeedback();
  async function act(path: string, method: string) {
    try { await api(`/conversations/${conversation}/messages/${m.id}${path}`, { method }); onChange(); }
    catch (e) { toast(e instanceof Error ? e.message : String(e), "bad"); }
  }
  async function remove() {
    if (await confirm({ title: "Delete this message?", body: "It is hidden from everyone and from MOKS, and can be restored.", action: "Delete message", danger: true })) act("", "DELETE");
  }
  return (
    <span className="msg-actions">
      {m.can_delete && <button type="button" className="ghost small" onClick={remove} aria-label="Delete message">Delete</button>}
      {m.can_restore && <button type="button" className="ghost small" onClick={() => act("/restore", "POST")}>Restore</button>}
    </span>
  );
}

function Actions({ actions }: { actions: any[] }) {
  if (!actions?.length) return null;
  return (
    <div className="rs static"><h3>Actions already executed</h3>
      <ul>{actions.map((a) => <li key={a.id}><Link to={`/actions/${a.id}`}>{a.action}</Link> <span className="mono small">{a.target}</span>{" "}
        <Badge kind={a.status === "succeeded" ? "ok" : a.status === "reconciliation_required" ? "bad" : "neutral"}>{a.status}</Badge>{" "}
        <span className="small muted">{a.validated ? "recovery validated" : "not validated"}</span></li>)}</ul>
    </div>
  );
}

function AssistantDetail({ id, onCite, onPropose }: { id: string; onCite: (inv: string, ref?: string) => void; onPropose: (inv: any, r: any) => void }) {
  const { can } = useAuth();
  const inv = useLoad(() => api<any>(`/investigations/${id}`), [id]);
  if (inv.error) return <ErrorBox error={inv.error} />;
  const d = inv.data;
  if (!d) return <div className="small muted">Loading the report…</div>;
  return (
    <>
      {d.error && <div className="alert error">{d.error}</div>}
      {d.report ? <Report report={d.report} evidence={d.evidence} hideSummary onCite={(r) => onCite(id, r)}
        onPropose={can("action.request") && d.namespace !== "*" ? (r) => onPropose(d, r) : undefined} /> : <div className="small muted">No report was produced.</div>}
      <Actions actions={d.actions} />
      <div className="small muted row" style={{ marginTop: 6 }}>
        <span>{d.report?.generated_by === "rules" ? "Rule-based report" : "Written by the LLM from cited evidence"}</span>
        <span>{d.tool_calls} tool calls</span>
        <button type="button" className="ghost small" onClick={() => onCite(id)}>All evidence ({d.evidence?.length ?? 0})</button>
        <Link to={`/investigations/${id}`}>Full view</Link>
      </div>
    </>
  );
}

function RunningTurn({ id, onChange, onActivity }: { id: string; onChange: () => void; onActivity: () => void }) {
  const { events, stream } = useInvestigationEvents(id, onChange);
  const [stopping, setStopping] = useState(false);
  const [error, setError] = useState<unknown>();
  const label = progressLabel(events);
  useEffect(() => { onActivity(); }, [events.length, onActivity]);
  async function stop() {
    setStopping(true);
    setError(null);
    try { await api(`/investigations/${id}/cancel`, { method: "POST" }); } catch (e) { setError(e); setStopping(false); }
  }
  return (
    <div className="msg moks">
      <div className="msg-meta"><span>MOKS</span></div>
      <div className="msg-body stack">
        <div className="progress"><span className="spin" aria-hidden="true" /><span>{stopping ? "Stopping" : label}…</span>
          <button type="button" className="ghost small" onClick={stop} disabled={stopping}>Stop</button></div>
        {STREAM[stream] && <div className="small muted">{STREAM[stream]}</div>}
        <ErrorBox error={error} />
        <details><summary className="small">Tool activity ({events.filter((e) => e.type === "tool_call").length})</summary><Timeline events={events} /></details>
      </div>
    </div>
  );
}

const Turn = memo(function Turn({ m, status, conversation, latest, onChange, onCite, onPropose }: {
  m: any; status?: string; conversation: string; latest: boolean; onChange: () => void; onCite: (inv: string, ref?: string) => void; onPropose: (inv: any, r: any) => void;
}) {
  const kind = messageKind(m);
  const [open, setOpen] = useState<boolean | undefined>(undefined);
  const expanded = open ?? latest;
  const body: ReactNode = m.deleted ? `Deleted by ${m.deleted_by ?? "someone"}.` : kind === "moks" ? <Markdown text={m.content ?? ""} /> : m.content;
  return (
    <article className={`msg ${kind}${m.deleted ? " deleted" : ""}`}>
      <div className="msg-meta">
        <span>{kind === "moks" ? "MOKS" : m.author ?? "unknown"}</span>
        {kind === "note" && <Badge>note</Badge>}
        <Time iso={m.created_at} />
        <MessageActions m={m} conversation={conversation} onChange={onChange} />
      </div>
      <div className="msg-body">{body}</div>
      {kind === "moks" && !m.deleted && m.investigation_id && (expanded
        ? <AssistantDetail id={m.investigation_id} onCite={onCite} onPropose={onPropose} />
        : <button type="button" className="ghost small" style={{ marginTop: 6 }} onClick={() => setOpen(true)}>Show report and evidence</button>)}
      {kind === "engineer" && (status === "failed" || status === "cancelled") && <div className="small muted" style={{ marginTop: 4 }}>{status === "failed" ? "This investigation failed." : "This investigation was cancelled."}</div>}
    </article>
  );
}, (a, b) => a.m.id === b.m.id && a.m.deleted === b.m.deleted && a.m.content === b.m.content && a.m.can_delete === b.m.can_delete && a.m.can_restore === b.m.can_restore && a.status === b.status && a.latest === b.latest);

// ---- Composer --------------------------------------------------------------------------------

export function draftKey(id: string) { return `moks.draft.${id}`; }

/** Keeps the draft through failures and across conversations. Enter sends unless an input method is composing. */
export function Composer({ id, canAsk, running, onSend }: { id: string; canAsk: boolean; running: boolean; onSend: (text: string, ask: boolean) => Promise<void> }) {
  const [text, setText] = useState(() => sessionStorage.getItem(draftKey(id)) ?? "");
  const [note, setNote] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>();
  const sending = useRef(false);
  const area = useRef<HTMLTextAreaElement>(null);
  const asNote = note || !canAsk;
  const ready = text.trim().length >= 3 && !busy;
  useLayoutEffect(() => {
    const el = area.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight + 2, 200)}px`;
  }, [text]);
  function change(value: string) {
    setText(value);
    if (value) sessionStorage.setItem(draftKey(id), value); else sessionStorage.removeItem(draftKey(id));
  }
  async function submit(e?: FormEvent) {
    e?.preventDefault();
    if (sending.current || text.trim().length < 3) return;
    sending.current = true;
    setBusy(true);
    setError(null);
    try {
      await onSend(text.trim(), !asNote);
      change("");
    } catch (err) {
      setError(err);
    } finally {
      sending.current = false;
      setBusy(false);
    }
  }
  function key(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key !== "Enter" || e.shiftKey || e.nativeEvent.isComposing || e.keyCode === 229) return;
    e.preventDefault();
    submit();
  }
  return (
    <form className="composer" onSubmit={submit}>
      <div className="composer-inner">
        <ErrorBox error={error} />
        <div className="composer-row">
          <textarea ref={area} rows={1} maxLength={2000} value={text} aria-label="Message" onChange={(e) => change(e.target.value)} onKeyDown={key}
            placeholder={asNote ? "Write a note for the others" : "Ask a follow-up"} />
          <button disabled={!ready}>{busy ? "Sending…" : asNote ? "Post" : "Send"}</button>
        </div>
        <div className="row between small muted">
          {canAsk ? <label className="inline"><input type="checkbox" checked={note} onChange={(e) => setNote(e.target.checked)} /> Note only, don't ask MOKS</label>
            : <span>You can post notes here. Asking MOKS needs the Investigator role for this scope.</span>}
          <span className="composer-hint">{running ? "MOKS is working on this conversation" : "Enter to send, Shift+Enter for a new line"}</span>
        </div>
      </div>
    </form>
  );
}

// ---- Conversation ----------------------------------------------------------------------------

function useConversation(id: string) {
  const [data, setData] = useState<any>();
  const [error, setError] = useState<unknown>();
  const [stale, setStale] = useState(false);
  const last = useRef("");
  const load = useCallback(async () => {
    try {
      const r = await api<any>(`/conversations/${id}`);
      setStale(false);
      setError(undefined);
      const text = JSON.stringify(r);
      if (text !== last.current) { last.current = text; setData(r); } // an unchanged poll renders nothing
    } catch (e) {
      setError(e);
      setStale(true);
    }
  }, [id]);
  useEffect(() => { load(); }, [load]);
  return { data, error, stale, load };
}

function Conversation({ id }: { id: string }) {
  const { can } = useAuth();
  const nav = useNavigate();
  const { confirm } = useFeedback();
  const { data: d, error, stale, load } = useConversation(id);
  const envs = useLoad(() => api<any[]>("/environments"), []);
  const [panel, setPanel] = useState<PanelState | null>(null);
  const [people, setPeople] = useState(false);
  const [person, setPerson] = useState("");
  const [peopleError, setPeopleError] = useState<unknown>();
  const [propose, setPropose] = useState<{ inv: any; r: any } | null>(null);
  const [limit, setLimit] = useState(PAGE);
  const [status, setStatus] = useState("");
  const scroller = useRef<HTMLDivElement>(null);
  const content = useRef<HTMLDivElement>(null);
  const { fresh, jump, notify } = useFollow(scroller, content);
  const running = !!d?.running;
  useInterval(load, running ? 3000 : 8000);
  const production = envs.data?.find((e) => e.name === d?.environment)?.is_production;
  useHeaderScope(d ? { environment: d.environment, namespace: d.namespace, production } : null);

  const count = d?.messages.length ?? 0;
  const started = useRef(false);
  useLayoutEffect(() => {
    if (!count) return;
    if (!started.current) { started.current = true; jump(); } else notify();
  }, [count, jump, notify]);
  const wasRunning = useRef(false);
  useEffect(() => {
    if (wasRunning.current && !running) setStatus("MOKS finished.");
    if (!wasRunning.current && running) setStatus("MOKS is investigating.");
    wasRunning.current = running;
  }, [running]);

  const earlier = useRef<number | null>(null);
  useLayoutEffect(() => {
    const el = scroller.current;
    if (el && earlier.current !== null) { el.scrollTop += el.scrollHeight - earlier.current; earlier.current = null; }
  }, [limit]);

  const onCite = useCallback((investigation: string, ref?: string) => setPanel({ investigation, ref }), []);
  const onPropose = useCallback((inv: any, r: any) => setPropose({ inv, r }), []);
  const onSend = useCallback(async (text: string, ask: boolean) => {
    await api(`/conversations/${id}/messages`, { method: "POST", body: { content: text, ask } });
    await load();
    jump();
  }, [id, load, jump]);

  if (!d) return error ? <div className="ws-conv" style={{ padding: 16 }}><ErrorBox error={error} /><Link to="/investigations">Back to conversations</Link></div> : <div className="ws-conv empty">Loading…</div>;

  const shown = d.messages.slice(-limit);
  const hidden = d.messages.length - shown.length;
  const answered = new Set(d.messages.filter((m: any) => m.role === "assistant").map((m: any) => m.investigation_id));
  const lastAnswer = [...d.messages].reverse().find((m: any) => m.role === "assistant" && !m.deleted)?.id;
  const active = d.messages.filter((m: any) => m.role === "user" && m.investigation_id && !answered.has(m.investigation_id) && !TERMINAL.has(d.statuses[m.investigation_id] ?? "completed"));

  async function removeConversation() {
    if (await confirm({ title: "Delete this conversation?", body: "It is hidden from everyone, and you can restore it from Show deleted.", action: "Delete conversation", danger: true })) {
      await api(`/conversations/${id}`, { method: "DELETE" });
      nav("/investigations");
    }
  }
  async function addPerson(e: FormEvent) {
    e.preventDefault();
    setPeopleError(null);
    try { await api(`/conversations/${id}/participants`, { method: "POST", body: { email: person } }); setPerson(""); load(); } catch (err) { setPeopleError(err); }
  }

  const firstEvidence = [...d.messages].reverse().find((m: any) => m.investigation_id)?.investigation_id;
  return (
    <>
      <section className="ws-conv" aria-label={d.title}>
        <div className="ws-head">
          <Link to="/investigations" className="btn ghost ws-back" aria-label="Back to conversations">Conversations</Link>
          <div className="ws-title">
            <h1 title={d.title}>{d.title}</h1>
            <div className="small muted row" style={{ gap: 6 }}><span className="mono">{d.environment}/{d.namespace}</span><span>started by {d.owner}</span>
              {running && <Badge kind="info">working</Badge>}{d.deleted && <Badge kind="warn">deleted</Badge>}</div>
          </div>
          <div className="row" style={{ gap: 6 }}>
            <button type="button" className="ghost small" aria-expanded={people} onClick={() => setPeople(!people)}>People ({d.participants.length})</button>
            {firstEvidence && <button type="button" className="ghost small" aria-expanded={!!panel} onClick={() => setPanel(panel ? null : { investigation: firstEvidence })}>Evidence</button>}
            {d.can_manage && !d.deleted && <button type="button" className="ghost small" onClick={removeConversation}>Delete</button>}
            {d.can_manage && d.deleted && <button type="button" className="small" onClick={async () => { await api(`/conversations/${id}/restore`, { method: "POST" }); load(); }}>Restore</button>}
          </div>
        </div>
        {people && (
          <div className="ws-people stack">
            <div className="row small">{d.participants.map((p: any) => <Badge key={p.email} kind={p.owner ? "info" : "neutral"}>{p.email}</Badge>)}</div>
            {!d.deleted && (d.is_participant || can("admin.policies")) && <form className="row" onSubmit={addPerson}>
              <input type="email" required aria-label="Add a person by email" value={person} onChange={(e) => setPerson(e.target.value)} placeholder="teammate@example.com" />
              <button className="ghost small">Add</button></form>}
            <ErrorBox error={peopleError} />
          </div>
        )}
        {stale && <div className="alert warn small">Cannot refresh this conversation. What you see may be out of date. Retrying.</div>}
        <div className="ws-scroll" ref={scroller}>
          <div className="ws-messages" ref={content}>
            {hidden > 0 && <button type="button" className="ghost small" style={{ alignSelf: "center" }} onClick={() => { earlier.current = scroller.current?.scrollHeight ?? 0; setLimit(limit + PAGE); }}>Show {Math.min(hidden, PAGE)} earlier messages</button>}
            {shown.map((m: any) => <Turn key={m.id} m={m} status={m.investigation_id ? d.statuses[m.investigation_id] : undefined} conversation={id} latest={m.id === lastAnswer} onChange={load} onCite={onCite} onPropose={onPropose} />)}
            {active.map((m: any) => <RunningTurn key={m.investigation_id} id={m.investigation_id} onChange={load} onActivity={notify} />)}
          </div>
          {fresh && <button type="button" className="ws-new" onClick={jump}>New activity</button>}
        </div>
        {!d.deleted && <Composer key={id} id={id} canAsk={d.can_ask} running={running} onSend={onSend} />}
        <div className="sr-only" role="status" aria-live="polite">{status}</div>
      </section>
      {panel && <EvidencePanel panel={panel} onRef={(ref) => setPanel({ ...panel, ref })} onClose={() => setPanel(null)} />}
      {propose && (
        <Dialog title="Propose an action plan" wide onClose={() => setPropose(null)}>
          <p className="small muted">Proposal from the report: {propose.r.action}. Nothing runs until the plan is reviewed.</p>
          <PlanForm env={propose.inv.environment} ns={propose.inv.namespace} initial={proposalPlan(propose.inv, propose.r)} onDone={(pid) => nav(`/actions/${pid}`)} />
        </Dialog>
      )}
    </>
  );
}

export function proposalPlan(inv: any, r: any) {
  return { ...(r.fromSkill ? { action: r.action, target_kind: r.target_kind ?? "Deployment", params: r.params ?? {} } : {}),
    target_name: r.fromSkill ? r.target_name ?? "" : (r.target || "").split("/").pop()?.split(" ")[0] ?? "",
    reason: r.fromSkill ? `${r.note}\n\nFinding: ${r.finding} (skill ${r.skill}). From investigation: ${inv.question}` : `${r.action}\n\nFrom investigation: ${inv.question}`,
    investigation_id: inv.id, incident_id: inv.incident_id ?? undefined,
    evidence: (inv.report?.observed_facts ?? []).slice(0, 5).map((f: any) => ({ ref: f.evidence.join(","), summary: f.statement })) };
}

export function Workspace() {
  const { id } = useParams();
  return (
    <div className={`ws${id ? " has-conv" : ""}`} data-workspace>
      <ConversationList current={id} />
      {id ? <Conversation key={id} id={id} /> : <div className="ws-empty ws-conv-empty"><h1 className="sr-only">Conversations</h1><div>Choose a conversation, or start a new investigation.</div></div>}
    </div>
  );
}
