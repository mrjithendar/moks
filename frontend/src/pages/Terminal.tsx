import { FitAddon } from "@xterm/addon-fit";
import { Terminal as XTerm } from "@xterm/xterm";
import "@xterm/xterm/css/xterm.css";
import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, ApiError } from "../api";
import { useAuth } from "../auth";
import { Badge, DataTable, ErrorBox, Loading, PageHead, StatusBadge, Time, useLoad } from "../components";
import { TerminalSocket, TerminalState } from "../terminalSocket";
import { Empty, useFeedback, useIsMobile } from "../ui";

interface Profile { name: string; label: string; environment: string; account: string; role_arn: string; regions: string[]; clusters: string[] }
interface Options { profiles: Profile[]; idle_minutes: number; max_minutes: number; min_minutes: number; kube_namespaces: string[]; recording: { text: string } }

const STATE_TEXT: Record<TerminalState, string> = {
  connecting: "Connecting", connected: "Connected", reconnecting: "Reconnecting: your shell is still running", taken_over: "Opened in another window",
  ended: "Session ended", signed_out: "Signed out", forbidden: "You no longer have terminal access", gone: "Not connected",
};

function minutesLeft(iso: string | null | undefined, now: number): number | null {
  return iso ? Math.max(0, Math.ceil((new Date(iso).getTime() - now) / 60_000)) : null;
}

function NewSession({ options, onOpen }: { options: Options; onOpen: (id: string) => void }) {
  const environments = useMemo(() => [...new Set(options.profiles.map((p) => p.environment))], [options]);
  const [environment, setEnvironment] = useState("");
  const [name, setName] = useState("");
  const [region, setRegion] = useState("");
  const [cluster, setCluster] = useState("");
  const [purpose, setPurpose] = useState("");
  const [minutes, setMinutes] = useState(Math.min(30, options.max_minutes));
  const [error, setError] = useState<unknown>();
  const [busy, setBusy] = useState(false);
  const profile = options.profiles.find((p) => p.name === name);
  async function open(e: FormEvent) {
    e.preventDefault();
    if (!profile) return;
    setBusy(true); setError(null);
    try {
      const s = await api<any>("/terminal/sessions", { method: "POST", body: { profile: name, environment, account: profile.account, region, cluster: cluster || null, purpose, minutes } });
      onOpen(s.id);
    } catch (err) { setError(err); setBusy(false); }
  }
  // Nothing is preselected: each part of the scope is a choice someone made on purpose.
  return (
    <form className="card stack" onSubmit={open}>
      <h2>Open a terminal</h2>
      {error ? <ErrorBox error={error} /> : null}
      <div className="row">
        <label>Environment<select required value={environment} onChange={(e) => { setEnvironment(e.target.value); setName(""); setRegion(""); setCluster(""); }}><option value="">choose…</option>{environments.map((n) => <option key={n}>{n}</option>)}</select></label>
        <label>AWS account and role<select required disabled={!environment} value={name} onChange={(e) => { setName(e.target.value); setRegion(""); setCluster(""); }}><option value="">choose…</option>
          {options.profiles.filter((p) => p.environment === environment).map((p) => <option key={p.name} value={p.name}>{p.account} ({p.label})</option>)}</select></label>
        <label>Region<select required disabled={!profile} value={region} onChange={(e) => setRegion(e.target.value)}><option value="">choose…</option>{profile?.regions.map((r) => <option key={r}>{r}</option>)}</select></label>
        <label>Kubernetes cluster<select disabled={!profile || !profile.clusters.length} value={cluster} onChange={(e) => setCluster(e.target.value)}><option value="">none</option>{profile?.clusters.map((c) => <option key={c}>{c}</option>)}</select></label>
        <label>Maximum time<select value={minutes} onChange={(e) => setMinutes(Number(e.target.value))}>{[15, 30, 45, 60, 90, 120].filter((m) => m >= options.min_minutes && m <= options.max_minutes).map((m) => <option key={m} value={m}>{m} minutes</option>)}</select></label>
      </div>
      {profile && <div className="small muted">Role <span className="mono">{profile.role_arn}</span></div>}
      <label>Purpose<textarea required minLength={10} maxLength={2000} rows={2} value={purpose} placeholder="Why you need a shell; it stays in the audit log" onChange={(e) => setPurpose(e.target.value)} /></label>
      <div className="alert info small">A terminal runs any command you type, inside a disposable pod. Its IAM role and Kubernetes RBAC are read-only and decide what it can do. Changes to production go through an action plan. {options.recording.text} It ends after {options.idle_minutes} minutes without input.</div>
      <div className="row"><button disabled={busy || !profile || !region || purpose.trim().length < 10}>{busy ? "Starting…" : "Open terminal"}</button></div>
    </form>
  );
}

export function Terminals() {
  const { can } = useAuth();
  const nav = useNavigate();
  const options = useLoad(() => api<Options>("/terminal/options"), []);
  const list = useLoad(() => api<any[]>("/terminal/sessions"), []);
  if (options.error instanceof ApiError && options.error.status === 404) {
    return <><PageHead title="Terminal" /><Empty title="Terminals are not enabled here">Set terminal.enabled in the Helm values; see docs/TERMINAL.md.</Empty></>;
  }
  if (!options.data) return <><PageHead title="Terminal" /><Loading load={options} /></>;
  return (
    <>
      <PageHead title="Terminal" sub="A disposable Bash shell with AWS CLI and kubectl, on short-lived read-only credentials" />
      <div className="stack">
        {can("terminal.use") && (options.data.profiles.length
          ? <NewSession options={options.data} onOpen={(id) => nav(`/terminal/${id}`)} />
          : <Empty title="No terminal profile for your access">A terminal needs a profile in the Helm values for an environment where you hold the terminal role.</Empty>)}
        <div className="card">
          <h2>Sessions</h2>
          <DataTable label="Terminal sessions" rows={list.data ?? []} rowKey={(s) => s.id} onRow={(s) => nav(`/terminal/${s.id}`)} empty="No terminal sessions yet."
            cols={[
              { key: "status", head: "Status", status: true, cell: (s: any) => <StatusBadge status={s.status} /> },
              { key: "owner", head: "Owner", cell: (s: any) => s.owner },
              { key: "scope", head: "Scope", cell: (s: any) => <span className="mono">{s.environment} · {s.account} · {s.region}{s.cluster ? ` · ${s.cluster}` : ""}</span> },
              { key: "purpose", head: "Purpose", primary: true, cell: (s: any) => s.purpose },
              { key: "opened", head: "Opened", cell: (s: any) => <Time iso={s.created_at} /> },
            ]} />
        </div>
      </div>
    </>
  );
}

function Scope({ s, state, now }: { s: any; state?: TerminalState; now: number }) {
  const left = minutesLeft(s.expires_at, now);
  const credentials = minutesLeft(s.credential_expires_at, now);
  const mobile = useIsMobile();
  const role = s.aws_identity?.split("/")[1];
  const details = (
    <dl className="term-scope" aria-label="Session scope">
      <div><dt>Environment</dt><dd>{s.environment}</dd></div>
      <div><dt>Account</dt><dd>{s.account}</dd></div>
      <div><dt>Region</dt><dd>{s.region}</dd></div>
      <div><dt>Identity</dt><dd>{s.aws_identity ?? "not verified yet"}</dd></div>
      <div><dt>Cluster</dt><dd>{s.cluster ?? "none"}{s.kube_identity ? ` as ${s.kube_identity}` : ""}{s.namespaces?.length ? ` (${s.namespaces.join(", ")})` : ""}</dd></div>
      {s.status === "ready" && <div><dt>Ends in</dt><dd>{left} min</dd></div>}
      {s.status === "ready" && <div><dt>Credentials</dt><dd>{credentials !== null && credentials <= 5 ? <Badge kind="warn">expire in {credentials} min</Badge> : `${credentials} min`}</dd></div>}
      {state && !mobile && <div><dt>Connection</dt><dd role="status">{STATE_TEXT[state]}</dd></div>}
    </dl>
  );
  if (!mobile) return details;
  // A phone needs the room for the terminal and its keyboard, so the scope stays on screen as one line.
  return (
    <details className="term-scope-line">
      <summary><span className="mono">{s.account} · {s.region} · {role ?? "not verified"}</span>{s.status === "ready" && <span className={credentials !== null && credentials <= 5 ? "term-warn" : "muted"}> · {left} min</span>}
        {state && <span role="status" className="muted"> · {STATE_TEXT[state]}</span>}</summary>
      {details}
    </details>
  );
}

function Suggestions({ id, onPlace }: { id: string; onPlace: (command: string) => void }) {
  const [question, setQuestion] = useState("");
  const [answer, setAnswer] = useState<any>();
  const [error, setError] = useState<unknown>();
  const [busy, setBusy] = useState(false);
  async function ask(e: FormEvent) {
    e.preventDefault();
    setBusy(true); setError(null); setAnswer(null);
    try { setAnswer(await api(`/assist/terminal/${id}`, { method: "POST", body: { question } })); } catch (err) { setError(err); }
    setBusy(false);
  }
  return (
    <form className="card stack" onSubmit={ask}>
      <h2>Suggest a command</h2>
      <div className="small muted">The model sees your question and this session's region and cluster, never what is in the terminal. Nothing runs until you press Enter yourself.</div>
      <div className="row"><input aria-label="What do you want to find out?" style={{ flex: 1 }} value={question} maxLength={1000} placeholder="Which load balancers have unhealthy targets?" onChange={(e) => setQuestion(e.target.value)} /><button disabled={busy || question.trim().length < 3}>{busy ? "Asking…" : "Suggest"}</button></div>
      {error ? <ErrorBox error={error} /> : null}
      {answer?.note && <div className="alert info small">{answer.note}</div>}
      {answer?.suggestions?.map((c: any) => (
        <div key={c.command} className="term-suggestion stack tight">
          <code>{c.command}</code>
          <div className="small">{c.explanation}</div>
          <div><button type="button" className="ghost small" onClick={() => onPlace(c.command)}>Put at my prompt</button></div>
        </div>
      ))}
      {answer && !answer.suggestions?.length && !answer.note && <div className="small muted">No usable suggestion. Try asking differently.</div>}
    </form>
  );
}

function Activity({ id }: { id: string }) {
  const [out, setOut] = useState<any>();
  const [error, setError] = useState<unknown>();
  return (
    <div className="card stack">
      <div className="row between"><h2>AWS activity</h2><button type="button" className="ghost small" onClick={() => { setError(null); api(`/terminal/sessions/${id}/cloudtrail`).then(setOut, setError); }}>Look up CloudTrail</button></div>
      <div className="small muted">What MOKS can show you here is who opened which scope and what AWS recorded. Terminal input is not recorded, and it would not be a complete record anyway: scripts and interactive programs hide what they run.</div>
      {error ? <ErrorBox error={error} /> : null}
      {out && <>
        <div className="small muted">{out.note}</div>
        {out.events.length ? <ul className="small mono">{out.events.map((e: any, i: number) => <li key={i}>{e.at} {e.source} {e.name}</li>)}</ul> : <div className="small">No events for {out.role_session_name} yet.</div>}
      </>}
    </div>
  );
}

const THEME = { background: "#0b0e12", foreground: "#e6e9ee", cursor: "#5aa9f8", selectionBackground: "#27466a" };
const KEYS: [string, string][] = [["Esc", "\x1b"], ["Tab", "\t"], ["Ctrl+C", "\x03"], ["Ctrl+D", "\x04"], ["Ctrl+L", "\x0c"], ["↑", "\x1b[A"], ["↓", "\x1b[B"], ["←", "\x1b[D"], ["→", "\x1b[C"]];

function Shell({ s, onEnded, now }: { s: any; onEnded: () => void; now: number }) {
  const host = useRef<HTMLDivElement>(null);
  const term = useRef<XTerm | null>(null);
  const socket = useRef<TerminalSocket | null>(null);
  const stopButton = useRef<HTMLButtonElement>(null);
  const [state, setState] = useState<TerminalState>("connecting");
  const [detail, setDetail] = useState<string>();
  const { confirm, toast } = useFeedback();
  const mobile = useIsMobile();

  const copy = useCallback(async () => {
    const text = term.current?.getSelection();
    if (!text) { toast("Select some text first", "bad"); return; }
    try { await navigator.clipboard.writeText(text); toast("Copied"); } catch { toast("The browser refused access to the clipboard", "bad"); }
  }, [toast]);
  const paste = useCallback(async () => {
    try { term.current?.paste(await navigator.clipboard.readText()); term.current?.focus(); } catch { toast("The browser refused access to the clipboard. Use your usual paste keys.", "bad"); }
  }, [toast]);

  useEffect(() => {
    const t = new XTerm({ theme: THEME, fontSize: mobile ? 12 : 13, fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace", scrollback: 10_000, cursorBlink: true, screenReaderMode: false, allowProposedApi: false });
    const fit = new FitAddon();
    t.loadAddon(fit);
    t.open(host.current!);
    term.current = t;
    const sock = new TerminalSocket(s.id, {
      data: (bytes) => t.write(bytes),
      state: (next, why) => {
        setState(next); setDetail(why);
        if (["ended", "signed_out", "forbidden"].includes(next)) onEnded();
      },
      opened: () => { fit.fit(); sock.resize(t.cols, t.rows); t.focus(); },
    });
    socket.current = sock;
    const input = t.onData((d) => sock.send(d));
    const binary = t.onBinary((d) => sock.send(d));
    const size = t.onResize(({ cols, rows }) => sock.resize(cols, rows));
    t.attachCustomKeyEventHandler((e) => {
      if (e.type !== "keydown") return true;
      if (e.key === "F6") { e.preventDefault(); stopButton.current?.focus(); return false; }  // Tab belongs to the shell, so this is the way out
      if (e.ctrlKey && e.shiftKey && e.key.toLowerCase() === "c") { e.preventDefault(); copy(); return false; }
      if (e.ctrlKey && e.shiftKey && e.key.toLowerCase() === "v") return false;  // the browser's paste reaches xterm's textarea
      return true;
    });
    const observer = new ResizeObserver(() => { try { fit.fit(); } catch { /* not laid out yet */ } });
    observer.observe(host.current!);
    sock.connect();
    return () => { observer.disconnect(); input.dispose(); binary.dispose(); size.dispose(); sock.close(); t.dispose(); term.current = null; };
    // The terminal belongs to this session for as long as the page is open.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [s.id]);

  async function stop() {
    if (await confirm({ title: "Stop this terminal?", body: "The shell and everything in its pod are deleted. This cannot be undone.", action: "Stop session", danger: true })) {
      await api(`/terminal/sessions/${s.id}/stop`, { method: "POST" });
      onEnded();
    }
  }
  const live = state === "connected";
  return (
    <div className="stack">
      <Scope s={s} state={state} now={now} />
      {state === "reconnecting" && <div className="alert warn small">The connection dropped. {detail}</div>}
      {state === "taken_over" && <div className="alert info small">{detail} <button type="button" className="ghost small" onClick={() => socket.current?.again()}>Use it here</button></div>}
      {state === "gone" && <div className="alert error small">{detail} <button type="button" className="ghost small" onClick={() => socket.current?.again()}>Try again</button></div>}
      {(["ended", "signed_out", "forbidden"] as TerminalState[]).includes(state) && <div className="alert error small">{detail || STATE_TEXT[state]}</div>}
      <div className={`term-frame${live ? "" : " off"}`}><div ref={host} className="term-host" role="application" aria-label="Terminal. Press F6 to leave it." /></div>
      {mobile && <div className="term-keys" aria-label="Terminal keys">{KEYS.map(([label, seq]) => <button key={label} type="button" className="ghost small" disabled={!live} onClick={() => { socket.current?.send(seq); term.current?.focus(); }}>{label}</button>)}</div>}
      <div className="row">
        <button type="button" className="ghost small" onClick={copy}>Copy selection</button>
        <button type="button" className="ghost small" disabled={!live} onClick={paste}>Paste</button>
        <button type="button" ref={stopButton} className="danger small" onClick={stop}>Stop session</button>
        {!mobile && <span className="small muted">Ctrl+Shift+C copies, Ctrl+Shift+V pastes, and F6 leaves the terminal.</span>}
      </div>
      <Suggestions id={s.id} onPlace={(command) => { if (!live) return; term.current?.paste(command); term.current?.focus(); toast("At your prompt. Review it, then press Enter to run it."); }} />
      <Activity id={s.id} />
    </div>
  );
}

export function TerminalView() {
  const { id } = useParams();
  const { can } = useAuth();
  const { confirm } = useFeedback();
  const session = useLoad(() => api<any>(`/terminal/sessions/${id}`), [id]);
  const [now, setNow] = useState(Date.now());
  const s = session.data;
  useEffect(() => { const t = setInterval(() => setNow(Date.now()), 15_000); return () => clearInterval(t); }, []);
  useEffect(() => {
    if (!s || !["starting", "ready"].includes(s.status)) return;
    const t = setInterval(session.reload, s.status === "starting" ? 2000 : 15_000);
    return () => clearInterval(t);
  }, [s?.status, session.reload]);
  if (!s) return <Loading load={session} rows={6} />;
  const head = <PageHead title="Terminal" sub={<span className="row small"><StatusBadge status={s.status} /><span>{s.purpose}</span>{!s.mine && <span>Owner {s.owner}</span>}<Link to="/terminal">All sessions</Link></span>} />;
  if (s.status === "ready" && s.mine) return <>{head}<Shell s={s} onEnded={session.reload} now={now} /></>;
  async function revoke() {
    if (await confirm({ title: `End ${s.owner}'s terminal?`, body: "Their shell is cut off and its pod is deleted.", action: "End session", danger: true })) {
      await api(`/terminal/sessions/${s.id}/stop`, { method: "POST" });
      session.reload();
    }
  }
  return (
    <>
      {head}
      <div className="stack">
        <Scope s={s} now={now} />
        {s.status === "starting" && <div className="alert info" role="status">Verifying the AWS identity and starting the pod. Input stays off until the identity is confirmed.</div>}
        {s.status === "failed" && <div className="alert error">This session did not start: {s.error}</div>}
        {s.status === "ended" && <div className="alert info">{s.end_text ?? "This session has ended."}</div>}
        {s.status === "ready" && !s.mine && <div className="alert info">A shell belongs to the person who opened it. Nobody else can attach to it.</div>}
        {!s.mine && ["starting", "ready"].includes(s.status) && can("terminal.revoke") && <div><button className="danger" onClick={revoke}>End this session</button></div>}
        {s.aws_identity && <Activity id={s.id} />}
      </div>
    </>
  );
}
