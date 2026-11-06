import { memo, useCallback, useEffect, useLayoutEffect, useRef, useState, type FormEvent, type KeyboardEvent, type ReactNode } from "react";
import { Link, useLocation, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { api } from "../api";
import { useAuth } from "../auth";
import { Badge, ErrorBox, Time, useInterval, useLoad } from "../components";
import { type Chip, FilterPanel, useUrlFilters } from "../filters";
import { useFollow } from "../follow";
import { type Ev, type StreamState, useInvestigationEvents } from "../investigationStream";
import { evidenceTarget } from "../evidence";
import { Markdown } from "../markdown";
import { useScope } from "../scope";
import { useHeaderScope } from "../shell";
import { StateView } from "../states";
import { ActionMenu, Dialog, Empty, Icon, Skeleton, Truncate, useFeedback, useIsMobile, useModal } from "../ui";
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

const SEVERITY: Record<string, "bad" | "warn" | "neutral"> = { sev1: "bad", sev2: "bad", sev3: "warn", sev4: "neutral" };
const STATES: Record<string, string> = { running: "Running", completed: "Completed", failed: "Failed", cancelled: "Cancelled" };
const STATE_TONE: Record<string, "ok" | "bad" | "info" | "warn"> = { running: "info", completed: "ok", failed: "bad", cancelled: "warn" };
const stateOf = (c: any) => (c.running ? "running" : c.status ?? "");
const INBOX = { state: "", unread: false, deleted: false, sort: "recent" }; // environment and namespace are the shared scope (env, ns)
const LAST = "moks.inbox.last"; // the conversation last opened, so Back can show where you were

export function relativeTime(iso: string, now = Date.now()): string {
  const s = Math.round((now - Date.parse(iso)) / 1000);
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  if (s < 86400 * 7) return `${Math.round(s / 86400)} d ago`;
  return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}
const absolute = (iso: string) => new Date(iso).toLocaleString(undefined, { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", timeZoneName: "short" });

function InboxRow({ c, current, recent, search, onChange, onDelete }: { c: any; current?: string; recent?: boolean; search: string; onChange: () => void; onDelete: (c: any) => void }) {
  const { toast } = useFeedback();
  const state = stateOf(c);
  const items = [
    !c.deleted && !c.deleted_view ? { label: c.unread ? "Mark as read" : "Mark as unread", run: () => { api(`/conversations/${c.id}/read`, { method: "POST", body: { unread: !c.unread } }).then(onChange, (e) => toast(e.message, "bad")); } } : null,
    { label: "Copy link", run: () => { navigator.clipboard?.writeText(`${window.location.origin}${import.meta.env.BASE_URL}conversations/${c.id}`).then(() => toast("Link copied"), () => toast("The browser refused to copy", "bad")); } },
    c.can_manage && !c.deleted_view ? { label: "Delete", danger: true, run: () => onDelete(c) } : null,
  ].filter((x): x is NonNullable<typeof x> => !!x);
  return (
    <li className={`inbox-row${c.id === current ? " active" : ""}${recent && c.id !== current ? " recent" : ""}${c.unread ? " unread" : ""}`} data-id={c.id}>
      <Link to={{ pathname: `/conversations/${c.id}`, search }} className="inbox-main" aria-current={c.id === current ? "page" : undefined} onClick={() => sessionStorage.setItem(LAST, c.id)}>
        <span className="inbox-top">
          {c.unread && <span className="inbox-dot" aria-hidden="true" />}{c.unread && <span className="sr-only">Unread: </span>}
          <Truncate text={c.title} />
        </span>
        {c.preview && <span className="inbox-preview">{c.preview}</span>}
        <span className="inbox-meta">
          {state && <Badge kind={STATE_TONE[state] ?? "neutral"}>{STATES[state] ?? state}</Badge>}
          {c.severity && <Badge kind={SEVERITY[c.severity] ?? "neutral"}>{c.severity}</Badge>}
          <span className="mono trunc">{c.environment}/{c.namespace}</span>
        </span>
      </Link>
      <time className="inbox-time" dateTime={c.updated_at} data-abs={absolute(c.updated_at)} tabIndex={0} aria-label={`Updated ${absolute(c.updated_at)}`}>{relativeTime(c.updated_at)}</time>
      <ActionMenu label={`Actions for ${c.title}`} items={items} />
    </li>
  );
}

function ConversationList({ current }: { current?: string }) {
  const { can } = useAuth();
  const { confirm, toast } = useFeedback();
  const nav = useNavigate();
  const loc = useLocation();
  const Heading = current ? "h2" : "h1"; // with a conversation open, its subject is the page's heading
  const [f, setFilters, clearFilters] = useUrlFilters(INBOX);
  const [scope, setScope] = useScope();
  const [params, setParams] = useSearchParams();
  useEffect(() => { // links from before the shared scope used ?environment=
    const legacy = params.get("environment");
    if (legacy === null) return;
    setParams((prev) => { const n = new URLSearchParams(prev); n.delete("environment"); if (legacy) n.set("env", legacy); return n; }, { replace: true });
  }, [params, setParams]);
  const set = setFilters;
  const clear = () => { clearFilters(); setScope({ env: "all" }); };
  const [search, setSearch] = useState(""); // what someone searches for stays out of the URL
  const recent = current ? undefined : sessionStorage.getItem(LAST) ?? undefined;
  const list = useLoad(() => api<any[]>(`/conversations?limit=200${f.deleted ? "&deleted=true" : ""}`), [f.deleted]);
  useInterval(() => { if (!list.loading) list.reload(); }, 15000);
  const needle = search.trim().toLowerCase();
  const all = (list.data ?? []).map((c) => ({ ...c, deleted_view: f.deleted }));
  const rows = all.filter((c) => (!needle || `${c.title} ${c.preview} ${c.environment} ${c.namespace} ${c.owner}`.toLowerCase().includes(needle))
    && (scope.env === "all" || c.environment === scope.env) && (scope.ns === "all" || c.namespace === scope.ns) && (!f.state || stateOf(c) === f.state) && (!f.unread || c.unread));
  if (f.sort === "oldest") rows.reverse();
  if (f.sort === "subject") rows.sort((a, b) => a.title.localeCompare(b.title));
  const envs = [...new Set((list.data ?? []).map((c) => c.environment as string))].sort();

  // The list is hidden, not unmounted, on phones: the browser forgets its scroll offset, so keep it here.
  const scroller = useRef<HTMLUListElement>(null);
  // The list remounts between the list and detail routes, so its offset is kept for this tab and these filters.
  const scrollKey = `moks.inbox.scroll${loc.search}`;
  const offset = useRef(Number(sessionStorage.getItem(scrollKey) ?? 0));
  const restored = useRef(false);
  useLayoutEffect(() => {
    if (current || !scroller.current || !list.data || restored.current) return;
    restored.current = true;
    if (offset.current) { scroller.current.scrollTop = offset.current; return; }
    if (recent) scroller.current.querySelector<HTMLElement>(`[data-id="${CSS.escape(recent)}"]`)?.scrollIntoView?.({ block: "center" });
  }, [current, recent, list.data]);
  useEffect(() => { if (current) restored.current = false; }, [current]);

  async function remove(c: any) {
    if (!await confirm({ title: "Delete this conversation?", body: "It is hidden from everyone, and you can restore it from Show deleted.", action: "Delete conversation", danger: true })) return;
    try {
      await api(`/conversations/${c.id}`, { method: "DELETE" });
      list.reload();
      if (c.id === current) nav({ pathname: "/investigations", search: loc.search });
    } catch (e) { toast(e instanceof Error ? e.message : String(e), "bad"); }
  }
  const chips: Chip[] = [
    ...(scope.env !== "all" ? [{ key: "e", label: scope.env, onRemove: () => setScope({ env: "all" }) }] : []),
    ...(scope.ns !== "all" ? [{ key: "n", label: `namespace ${scope.ns}`, onRemove: () => setScope({ ns: "all" }) }] : []),
    ...(f.state ? [{ key: "s", label: STATES[f.state] ?? f.state, onRemove: () => set({ state: "" }) }] : []),
    ...(f.unread ? [{ key: "u", label: "Unread", onRemove: () => set({ unread: false }) }] : []),
    ...(f.deleted ? [{ key: "d", label: "Deleted", onRemove: () => set({ deleted: false }) }] : []),
  ];
  return (
    <section className="ws-list" aria-label="Conversations">
      <div className="ws-list-head">
        <div className="row between nowrap"><Heading className="inbox-title">Conversations</Heading>
          {can("investigation.run") && <Link className="btn" to="/investigate"><Icon name="plus" size={16} /> New investigation</Link>}</div>
        <FilterPanel chips={chips} onClear={clear} label="Filters"
          search={<input type="search" className="grow" aria-label="Search conversations" placeholder="Search subject, preview, scope" value={search} onChange={(e) => setSearch(e.target.value)} />}
          advanced={<div className="filters">
            <label>Environment<select value={scope.env} onChange={(e) => setScope({ env: e.target.value })}><option value="all">All authorized environments</option>{envs.map((e) => <option key={e}>{e}</option>)}</select></label>
            <label>Status<select value={f.state} onChange={(e) => set({ state: e.target.value })}><option value="">Any</option>{Object.entries(STATES).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select></label>
            <label>Sort<select value={f.sort} onChange={(e) => set({ sort: e.target.value })}><option value="recent">Newest first</option><option value="oldest">Oldest first</option><option value="subject">Subject</option></select></label>
            <label className="inline"><input type="checkbox" checked={f.unread} onChange={(e) => set({ unread: e.target.checked })} /> Unread only</label>
            <label className="inline"><input type="checkbox" checked={f.deleted} onChange={(e) => set({ deleted: e.target.checked })} /> Show deleted</label>
          </div>} />
      </div>
      <ErrorBox error={list.error} onRetry={list.reload} />
      {list.loading && !list.data && <div className="inbox-pad"><Skeleton rows={6} label="Loading conversations" /></div>}
      <ul className="ws-list-items" ref={scroller} onScroll={(e) => { if (!current || e.currentTarget.offsetParent) { offset.current = e.currentTarget.scrollTop; sessionStorage.setItem(scrollKey, String(offset.current)); } }}>
        {rows.map((c) => <InboxRow key={c.id} c={c} current={current} recent={c.id === recent} search={loc.search} onChange={list.reload} onDelete={remove} />)}
        {list.data && !rows.length && <li><Empty title={needle || chips.length ? "No conversations match." : "No conversations yet."}>{needle || chips.length ? "Change the search or clear the filters." : "Start a new investigation and it appears here."}</Empty></li>}
      </ul>
    </section>
  );
}

// ---- Evidence --------------------------------------------------------------------------------

interface PanelState { investigation: string; ref?: string }

function EvidencePanel({ panel, onRef, onClose }: { panel: PanelState; onRef: (r?: string) => void; onClose: () => void }) {
  const mobile = useIsMobile(1180);
  const box = useRef<HTMLElement>(null);
  useModal(box, mobile, onClose); // a sheet where three columns would be squeezed; beside the conversation elsewhere
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
        {open ? <button type="button" className="ghost small" onClick={() => onRef(undefined)}>All evidence</button> : <h2>Evidence ({evidence.length})</h2>}
        <button type="button" className="ghost small" onClick={onClose}>Close</button>
      </div>
      <div className="ws-evidence-body">
        <ErrorBox error={inv.error} />
        {!data && !inv.error && <Skeleton rows={4} label="Loading evidence" />}
        {data && !open && !evidence.length && <Empty title="No evidence collected yet." />}
        {!open && evidence.map((e) => (
          <button type="button" key={e.ref} className="ev-row" onClick={() => onRef(e.ref)}>
            <div className="row" style={{ gap: 6 }}><span className="cite" style={{ marginLeft: 0 }}>{e.ref}</span><span className="mono small">{e.tool}</span>{!e.ok && <Badge kind="bad">failed</Badge>}</div>
            <div className="small muted clamp-2">{e.summary}</div>
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
  const items = [...(m.can_delete ? [{ label: "Delete message", danger: true, run: remove }] : []), ...(m.can_restore ? [{ label: "Restore message", run: () => act("/restore", "POST") }] : [])];
  return <span className="msg-actions"><ActionMenu label={`Actions for the message from ${m.author ?? "MOKS"}`} items={items} /></span>;
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
      {d.report ? <Report report={d.report} evidence={d.evidence} hideSummary onCite={(r) => onCite(id, r)} investigationId={id}
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

function RunningTurn({ id, onChange, onActivity, stopping }: { id: string; onChange: () => void; onActivity: () => void; stopping: boolean }) {
  const { events, stream } = useInvestigationEvents(id, onChange);
  const label = progressLabel(events);
  useEffect(() => { onActivity(); }, [events.length, onActivity]);
  return (
    <div className="msg moks">
      <div className="msg-meta"><span className="msg-who">MOKS</span></div>
      <div className="msg-body stack">
        <div className="progress"><span className="spin" aria-hidden="true" /><span>{stopping ? "Stopping" : label}…</span></div>
        {STREAM[stream] && <div className="small muted">{STREAM[stream]}</div>}
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
        <span className="msg-who">{kind === "moks" ? "MOKS" : m.author ?? "unknown"}</span>
        <span className="msg-kind">{kind === "note" ? "Note to collaborators" : kind === "engineer" ? "Asked MOKS" : kind === "moks" ? "Report" : "System"}</span>
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

// ---- Reading pane ----------------------------------------------------------------------------

/** The evidence of one investigation, in the order it was collected. Failed reads stay listed with what went
 *  wrong, and everything already collected stays when a connector fails part-way. */
function EvidenceList({ id, running, onOpen }: { id: string; running: boolean; onOpen: (ref: string) => void }) {
  const inv = useLoad(() => api<any>(`/investigations/${id}`), [id]);
  useInterval(inv.reload, 4000, running);
  const ev: any[] = inv.data?.id === id ? inv.data.evidence ?? [] : [];
  if (!inv.data) return inv.error ? <ErrorBox error={inv.error} onRetry={inv.reload} /> : <Skeleton rows={3} label="Loading evidence" />;
  const failed = ev.filter((e) => !e.ok);
  return (
    <>
      {failed.length > 0 && <StateView kind="partial" why={`${failed.length} of ${ev.length} evidence reads failed. The rest is shown, and the report says what is missing.`} />}
      {!ev.length ? <StateView kind="empty" title="No evidence yet" why={running ? "MOKS is still collecting evidence." : "No tool returned evidence for this question."} />
        : <ul className="rp-ev">{ev.map((e) => {
          const t = evidenceTarget(e);
          return <li key={e.ref}><button type="button" className="ghost" onClick={() => onOpen(e.ref)} aria-label={`Open evidence ${e.ref}: ${t.kind}`}>
            <span className="row tight"><span className="cite" style={{ marginLeft: 0 }}>{e.ref}</span><strong>{t.kind}</strong>{!e.ok && <Badge kind="bad">✕ failed</Badge>}</span>
            <span className="small muted clamp-2">{e.summary}</span>
            {t.detail && <span className="mono small trunc" style={{ maxWidth: "100%" }}>{t.detail}</span>}
          </button></li>;
        })}</ul>}
    </>
  );
}

function ReadingPane({ q, a, status, running, conversation, onChange, onCite, onPropose, onActivity, stopping }: {
  q: any; a?: any; status?: string; running: boolean; conversation: string; onChange: () => void; onCite: (inv: string, ref?: string) => void;
  onPropose: (inv: any, r: any) => void; onActivity: () => void; stopping: boolean;
}) {
  return (
    <>
      <section className="rp-section" aria-labelledby={`q-${q.id}`}>
        <h2 id={`q-${q.id}`}>Question</h2>
        <div className="rp-question">{q.content}</div>
        <div className="small muted">{q.author ?? "unknown"} · <Time iso={q.created_at} /></div>
      </section>
      <section className="rp-section" aria-labelledby={`a-${q.id}`}>
        <h2 id={`a-${q.id}`}>Answer</h2>
        {a ? <Turn m={a} status={status} conversation={conversation} latest onChange={onChange} onCite={onCite} onPropose={onPropose} />
          : running ? <RunningTurn id={q.investigation_id} onChange={onChange} onActivity={onActivity} stopping={stopping} />
          : status === "failed" ? <StateView kind="unavailable" title="This investigation failed" why="MOKS could not finish it. The evidence it collected is below." next="Ask again in the box below, or open the full view for the error." />
          : status === "cancelled" ? <StateView kind="empty" title="This investigation was cancelled" why="It was stopped before it answered." next="Ask again in the box below." />
          : <StateView kind="loading" title="Waiting for the answer" />}
      </section>
      <section className="rp-section" aria-labelledby={`e-${q.id}`}>
        <h2 id={`e-${q.id}`}>Evidence</h2>
        <EvidenceList id={q.investigation_id} running={running} onOpen={(ref) => onCite(q.investigation_id, ref)} />
      </section>
    </>
  );
}

// ---- Composer --------------------------------------------------------------------------------

export function draftKey(id: string) { return `moks.draft.${id}`; }

/** Keeps the draft through failures and across conversations. It stays in this tab's sessionStorage only,
 *  and clearLocalWork removes it on sign-out. Enter sends unless an input method is composing. */
export function Composer({ id, canAsk, running, stopping, onSend, onStop }: { id: string; canAsk: boolean; running: boolean; stopping?: boolean; onSend: (text: string, ask: boolean) => Promise<void>; onStop?: () => void }) {
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
          {running && onStop && <button type="button" className="ghost" disabled={stopping} onClick={onStop}>{stopping ? "Stopping…" : "Stop"}</button>}
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
  const [stopping, setStopping] = useState(false);
  const [stopError, setStopError] = useState<unknown>();
  const loc = useLocation();
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
  useEffect(() => { // only after this person has had the conversation open with these messages
    if (count && !d?.deleted && !document.hidden) api(`/conversations/${id}/read`, { method: "POST", body: {} }).catch(() => {});
  }, [id, count, d?.deleted]);
  useEffect(() => { if (!running) setStopping(false); }, [running]);
  const started = useRef(false);
  useLayoutEffect(() => {
    if (!count) return;
    if (!started.current) { started.current = true; if (scroller.current) scroller.current.scrollTop = 0; } else notify(); // opens on the question, not the end of the history
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
    if (scroller.current) scroller.current.scrollTop = 0; // a new question becomes the one at the top
  }, [id, load]);

  if (!d) return error ? <div className="ws-conv ws-pad"><ErrorBox error={error} onRetry={load} /><Link to="/investigations">Back to inbox</Link></div> : <div className="ws-conv ws-pad"><Skeleton rows={6} label="Loading conversation" /></div>;

  // Reading order: the latest question, its answer, its evidence, then everything else in the order it happened.
  const lastQ = [...d.messages].reverse().find((m: any) => messageKind(m) === "engineer" && !m.deleted);
  const lastA = lastQ && d.messages.find((m: any) => m.role === "assistant" && m.investigation_id === lastQ.investigation_id);
  const past = d.messages.filter((m: any) => m !== lastQ && m !== lastA);
  const shown = past.slice(-limit);
  const hidden = past.length - shown.length;
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
  async function stop() {
    setStopping(true);
    setStopError(null);
    try { await Promise.all(active.map((m: any) => api(`/investigations/${m.investigation_id}/cancel`, { method: "POST" }))); }
    catch (err) { setStopError(err); setStopping(false); }
  }

  const firstEvidence = [...d.messages].reverse().find((m: any) => m.investigation_id)?.investigation_id;
  const menu = [
    { label: `People (${d.participants.length})`, run: () => setPeople(true) },
    ...(d.can_manage && !d.deleted ? [{ label: "Delete conversation", danger: true, run: removeConversation }] : []),
    ...(d.can_manage && d.deleted ? [{ label: "Restore conversation", run: async () => { await api(`/conversations/${id}/restore`, { method: "POST" }); load(); } }] : []),
  ];
  return (
    <>
      <section className="ws-conv" aria-label={d.title}>
        <div className="ws-head">
          <Link to={{ pathname: "/investigations", search: loc.search }} className="btn ghost ws-back" aria-label="Back to inbox"><Icon name="back" size={20} /></Link>
          <div className="ws-title">
            <h1 className="trunc" title={d.title}>{d.title}</h1>
            <div className="ws-sub small muted"><span className="mono trunc">{d.environment}/{d.namespace}</span>
              {running && <Badge kind="info">working</Badge>}{d.deleted && <Badge kind="warn">deleted</Badge>}</div>
          </div>
          {firstEvidence && <button type="button" className="ghost small" aria-expanded={!!panel} onClick={() => setPanel(panel ? null : { investigation: firstEvidence })}>Evidence</button>}
          <ActionMenu label={`Actions for ${d.title}`} items={menu} />
        </div>
        {people && <Dialog title="People in this conversation" onClose={() => setPeople(false)}>
          <div className="stack">
            <p className="small muted">Started by {d.owner}.</p>
            <ul className="people">{d.participants.map((p: any) => <li key={p.email}><Truncate text={p.email} /> {p.owner && <Badge kind="info">owner</Badge>}</li>)}</ul>
            {!d.deleted && (d.is_participant || can("admin.policies")) && <form className="row nowrap" onSubmit={addPerson}>
              <input type="email" className="grow" required aria-label="Add a person by email" value={person} onChange={(e) => setPerson(e.target.value)} placeholder="teammate@example.com" />
              <button className="ghost">Add</button></form>}
            <ErrorBox error={peopleError} />
          </div>
        </Dialog>}
        {stale && <div className="alert warn small">Cannot refresh this conversation. What you see may be out of date. Retrying.</div>}
        <div className="ws-scroll" ref={scroller}>
          <div className="ws-messages" ref={content}>
            {lastQ && <ReadingPane q={lastQ} a={lastA} status={d.statuses[lastQ.investigation_id]} running={active.some((m: any) => m.investigation_id === lastQ.investigation_id)}
              conversation={id} onChange={load} onCite={onCite} onPropose={onPropose} onActivity={notify} stopping={stopping} />}
            <section className="rp-section" aria-labelledby={`hist-${id}`}>
              <details className="rp-history" open={!lastQ || past.length <= 6}>
                <summary id={`hist-${id}`}>Conversation history ({past.length})</summary>
                <div className="ws-history">
                  {hidden > 0 && <button type="button" className="ghost small" style={{ alignSelf: "center" }} onClick={() => { earlier.current = scroller.current?.scrollHeight ?? 0; setLimit(limit + PAGE); }}>Show {Math.min(hidden, PAGE)} earlier messages</button>}
                  {shown.map((m: any) => <Turn key={m.id} m={m} status={m.investigation_id ? d.statuses[m.investigation_id] : undefined} conversation={id} latest={!lastQ && m.id === lastAnswer} onChange={load} onCite={onCite} onPropose={onPropose} />)}
                  {active.filter((m: any) => m !== lastQ).map((m: any) => <RunningTurn key={m.investigation_id} id={m.investigation_id} onChange={load} onActivity={notify} stopping={stopping} />)}
                  {!past.length && <div className="small muted">Nothing before this question.</div>}
                </div>
              </details>
            </section>
          </div>
          {fresh && <button type="button" className="ws-new" onClick={jump}>New activity</button>}
        </div>
        <ErrorBox error={stopError} />
        {!d.deleted && <Composer key={id} id={id} canAsk={d.can_ask} running={running} stopping={stopping} onSend={onSend} onStop={active.length ? stop : undefined} />}
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
      {id ? <Conversation key={id} id={id} /> : <div className="ws-empty ws-conv-empty"><Empty title="Choose a conversation">Or start a new investigation.</Empty></div>}
    </div>
  );
}
