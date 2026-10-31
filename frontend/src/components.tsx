import { useEffect, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent, type ReactNode } from "react";
import { api, ApiError, qs } from "./api";
import { ActionMenu, CopyButton, Empty, type MenuItem, Skeleton, useIsMobile } from "./ui";

/** A table row that opens something. It stays a row for layout, and keyboards can reach it. */
export function rowAction(fn: () => void) {
  return { tabIndex: 0, role: "button", onClick: fn, onKeyDown: (e: ReactKeyboardEvent) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fn(); } } };
}

export function PageHead({ title, sub, children }: { title: string; sub?: ReactNode; children?: ReactNode }) {
  return (
    <div className="page-head">
      <div><h1>{title}</h1>{sub && <p>{sub}</p>}</div>
      <div className="row">{children}</div>
    </div>
  );
}

export function Badge({ kind = "neutral", children }: { kind?: "ok" | "warn" | "bad" | "info" | "neutral"; children: ReactNode }) {
  return <span className={`badge ${kind}`}>{children}</span>;
}

export function StatusBadge({ status }: { status: string }) {
  const kind = status === "completed" ? "ok" : status === "failed" ? "bad" : status === "cancelled" ? "warn" : "info";
  return <Badge kind={kind}>{status}</Badge>;
}

export function ErrorBox({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  if (!error) return null;
  if (error instanceof ApiError && error.status === 403) return <div className="alert warn" role="alert"><strong>You don't have access to this.</strong> <span className="small">{error.message}</span></div>;
  return <div className="alert error row between" role="alert"><span className="grow">{error instanceof Error ? error.message : String(error)}</span>{onRetry && <button type="button" className="ghost small" onClick={onRetry}>Try again</button>}</div>;
}

export interface Col<T> {
  key: string; head: string; cell: (row: T) => ReactNode;
  /** The row's name: first on a phone, and what a keyboard user activates. */
  primary?: boolean;
  /** Status shown beside the name on a phone. */
  status?: boolean;
  /** Left out of the phone row. The detail view still has it. */
  wide?: boolean;
  /** Plain text for the phone's second line. Without it the cell itself is used. */
  meta?: (row: T) => ReactNode;
}

/** A table where a table reads well, and a compact list on phones. Both show the same rows, and the
 *  backend's data stays complete: `wide` columns are for the detail view, never data found nowhere else. */
export function DataTable<T>({ label, rows, cols, rowKey, onRow, menu, empty }: {
  label: string; rows: T[]; cols: Col<T>[]; rowKey: (row: T) => string; onRow?: (row: T) => void; menu?: (row: T) => { label: string; items: MenuItem[] }; empty?: ReactNode;
}) {
  const phone = useIsMobile(820);
  if (!rows.length) return <>{empty ?? <Empty title="Nothing here." />}</>;
  const primary = cols.find((c) => c.primary) ?? cols[0];
  if (phone) {
    const status = cols.filter((c) => c.status);
    const meta = cols.filter((c) => !c.primary && !c.status && !c.wide);
    return (
      <ul className="rl" aria-label={label}>
        {rows.map((r) => {
          const m = menu?.(r);
          const inner = <>
            <span className="rl-top"><span className="rl-name">{primary.cell(r)}</span>{status.map((c) => <span key={c.key} className="rl-status">{c.cell(r)}</span>)}</span>
            <span className="rl-meta">{meta.map((c) => { const v = (c.meta ?? c.cell)(r); return v === "" || v === null || v === undefined ? null : <span key={c.key}>{v}</span>; })}</span>
          </>;
          return (
            <li key={rowKey(r)} className="rl-row">
              {onRow ? <button type="button" className="rl-main" onClick={() => onRow(r)}>{inner}</button> : <div className="rl-main">{inner}</div>}
              {m && <ActionMenu label={m.label} items={m.items} />}
            </li>
          );
        })}
      </ul>
    );
  }
  return (
    <div className="table-wrap" role="region" aria-label={label} tabIndex={0}>
      <table>
        <thead><tr>{cols.map((c) => <th key={c.key} scope="col">{c.head}</th>)}{menu && <th scope="col"><span className="sr-only">Actions</span></th>}</tr></thead>
        <tbody>{rows.map((r) => {
          const m = menu?.(r);
          return (
            <tr key={rowKey(r)} className={onRow ? "clickable" : undefined} onClick={onRow ? () => onRow(r) : undefined}>
              {cols.map((c) => <td key={c.key} className={c.primary ? "td-name" : undefined}>{c === primary && onRow ? <button type="button" className="cell-btn">{c.cell(r)}</button> : c.cell(r)}</td>)}
              {m && <td className="td-menu"><ActionMenu label={m.label} items={m.items} /></td>}
            </tr>
          );
        })}</tbody>
      </table>
    </div>
  );
}

/** First load only: a refresh keeps what is on screen. */
export function Loading({ load, rows }: { load: { data?: unknown; loading: boolean; error?: unknown; reload?: () => void }; rows?: number }) {
  if (load.error) return <ErrorBox error={load.error} onRetry={load.reload} />;
  return !load.data && load.loading ? <Skeleton rows={rows} /> : null;
}

export function ago(iso?: string | null): string {
  if (!iso) return "—";
  const s = Math.round((Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 5) return "just now";
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

export function Time({ iso }: { iso?: string | null }) {
  if (!iso) return <span className="muted">—</span>;
  return <time dateTime={iso} title={new Date(iso).toLocaleString(undefined, { timeZoneName: "short" })}>{ago(iso)}</time>;
}

export function useLoad<T>(fn: () => Promise<T>, deps: unknown[]): { data: T | undefined; error: unknown; loading: boolean; reload: () => void } {
  const [data, setData] = useState<T>();
  const [error, setError] = useState<unknown>();
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);
  useEffect(() => {
    let live = true;
    setLoading(true);
    fn().then((d) => live && (setData(d), setError(undefined)), (e) => live && setError(e)).finally(() => live && setLoading(false));
    return () => { live = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick]);
  return { data, error, loading, reload: () => setTick((t) => t + 1) };
}

/** Calls fn every ms while the tab is visible. */
export function useInterval(fn: () => void, ms: number, enabled = true) {
  const ref = useRef(fn);
  ref.current = fn;
  useEffect(() => {
    if (!enabled) return;
    const id = setInterval(() => { if (!document.hidden) ref.current(); }, ms);
    return () => clearInterval(id);
  }, [ms, enabled]);
}

export interface Multi<T = any> {
  items: T[]; errors: { environment: string; namespace: string | null; error: string; denied: boolean }[]; environments: string[];
  queries: Record<string, string>; collected_at: string | null; step?: number; missing_references?: any[];
}
export interface Scope { environment: string; display_name: string; namespaces: string[]; error?: string }

export function useScopes() {
  return useLoad(() => api<{ scopes: Scope[] }>("/multi/scopes").then((r) => r.scopes), []);
}

/** "All environments / All namespaces" filters; namespaces follow the chosen environment. */
export function EnvNsFilter({ scopes, env, ns, onEnv, onNs, noNamespace = false }: {
  scopes?: Scope[]; env: string; ns: string; onEnv: (e: string) => void; onNs: (n: string) => void; noNamespace?: boolean;
}) {
  const shown = (scopes ?? []).filter((s) => env === "all" || s.environment === env);
  const namespaces = [...new Set(shown.flatMap((s) => s.namespaces))].sort();
  return (
    <>
      <label>Environment
        <select value={env} onChange={(e) => {
          const next = e.target.value;
          onEnv(next);
          const list = next === "all" ? scopes?.flatMap((s) => s.namespaces) : scopes?.find((s) => s.environment === next)?.namespaces;
          if (ns !== "all" && !list?.includes(ns)) onNs("all");
        }}>
          <option value="all">All environments</option>
          {scopes?.map((s) => <option key={s.environment} value={s.environment}>{s.display_name}</option>)}
        </select>
      </label>
      {!noNamespace && (
        <label>Namespace
          <select value={ns} onChange={(e) => onNs(e.target.value)}>
            <option value="all">All namespaces</option>
            {namespaces.map((n) => <option key={n} value={n}>{n}</option>)}
          </select>
        </label>
      )}
    </>
  );
}

/** Per-environment/namespace failures: the rest of the view still renders. */
export function PartialErrors({ errors }: { errors?: Multi["errors"] }) {
  if (!errors?.length) return null;
  return (
    <details className="alert warn small" style={{ marginBottom: 10 }}>
      <summary>{errors.length} source{errors.length > 1 ? "s" : ""} returned partial data</summary>
      {errors.slice(0, 40).map((e, i) => <div key={i}><span className="mono">{e.environment}{e.namespace ? `/${e.namespace}` : ""}</span>: {e.error}</div>)}
    </details>
  );
}

export function Stat({ value, label }: { value: ReactNode; label: ReactNode }) {
  return <div className="stat-tile"><div className="stat">{value}</div><div className="small muted">{label}</div></div>;
}

export interface Env { name: string; display_name: string; kind: string; is_production: boolean; can_investigate?: boolean; grafana_url?: string | null }

/** Environment + namespace picker. Namespaces come from the backend, already filtered to the user's scope. */
export function ScopePicker({ env, ns, onEnv, onNs, allowAll = false }: {
  env: string; ns: string; onEnv: (e: string) => void; onNs: (n: string) => void; allowAll?: boolean;
}) {
  const envs = useLoad(() => api<Env[]>("/environments"), []);
  const nss = useLoad(() => (env ? api<{ namespaces: string[]; cluster_wide: boolean; error?: string }>(`/explore/${env}/namespaces`) : Promise.resolve(null)), [env]);
  useEffect(() => {
    if (!env && envs.data?.length) onEnv(envs.data[0].name);
  }, [envs.data, env, onEnv]);
  useEffect(() => {
    const list = nss.data?.namespaces ?? [];
    if (env && list.length && !list.includes(ns) && !(allowAll && ns === "*")) onNs(list.includes("default") && list.length === 1 ? "default" : list[0]);
  }, [nss.data, env, ns, onNs, allowAll]);
  return (
    <div className="row">
      <label>Environment
        <select value={env} onChange={(e) => onEnv(e.target.value)}>
          {envs.data?.map((e) => <option key={e.name} value={e.name}>{e.display_name}{e.is_production ? " (production)" : ""}</option>)}
        </select>
      </label>
      <label>Namespace
        <select value={ns} onChange={(e) => onNs(e.target.value)}>
          {allowAll && nss.data?.cluster_wide && <option value="*">All permitted namespaces</option>}
          {nss.data?.namespaces.map((n) => <option key={n} value={n}>{n}</option>)}
        </select>
      </label>
      {nss.data?.error && <span className="small muted" title={nss.data.error}>namespace list limited</span>}
    </div>
  );
}

const COLORS = ["#38bdf8", "#f59e0b", "#22c55e", "#a78bfa", "#f472b6", "#ef4444", "#14b8a6", "#eab308", "#818cf8", "#fb923c"];

export interface Series { metric: Record<string, string>; values?: [number, number | null][]; value?: [number, number | null] }

function seriesLabel(m: Record<string, string>): string {
  const keys = Object.keys(m).filter((k) => k !== "__name__");
  if (!keys.length) return m.__name__ ?? "value";
  return keys.map((k) => `${k}=${m[k]}`).join(", ");
}

/** Minimal dependency-free SVG line chart for Prometheus matrix results. */
export function LineChart({ series, unit }: { series: Series[]; unit?: "bytes" | "cores" | "ratio" }) {
  const [hover, setHover] = useState<number | null>(null);
  const W = 800, H = 220, P = { l: 56, r: 10, t: 10, b: 22 };
  const pts = useMemo(() => series.filter((s) => s.values?.length).slice(0, 10), [series]);
  const all = pts.flatMap((s) => s.values!.filter((v) => v[1] !== null));
  if (!all.length) return <div className="empty">No data points in this range.</div>;
  const xs = all.map((v) => v[0]), ys = all.map((v) => v[1] as number);
  const x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(0, ...ys), y1 = Math.max(...ys) || 1;
  const sx = (x: number) => P.l + ((x - x0) / (x1 - x0 || 1)) * (W - P.l - P.r);
  const sy = (y: number) => H - P.b - ((y - y0) / (y1 - y0 || 1)) * (H - P.t - P.b);
  const fmt = (v: number) => unit === "bytes" ? fmtBytes(v) : unit === "ratio" ? `${(v * 100).toFixed(0)}%` : Math.abs(v) >= 1000 ? v.toExponential(1) : +v.toFixed(3) + "";
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => y0 + f * (y1 - y0));
  return (
    <div>
      <svg className="chart" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" role="img" aria-label="time series chart"
        onMouseLeave={() => setHover(null)}
        onMouseMove={(e) => {
          const r = (e.currentTarget as SVGSVGElement).getBoundingClientRect();
          const x = x0 + ((e.clientX - r.left) / r.width * W - P.l) / (W - P.l - P.r) * (x1 - x0);
          setHover(x);
        }}>
        {ticks.map((t, i) => <g key={i}><line x1={P.l} x2={W - P.r} y1={sy(t)} y2={sy(t)} style={{ stroke: "var(--border)" }} /><text x={4} y={sy(t) + 3}>{fmt(t)}</text></g>)}
        <text x={P.l} y={H - 6}>{new Date(x0 * 1000).toLocaleTimeString()}</text>
        <text x={W - P.r - 60} y={H - 6}>{new Date(x1 * 1000).toLocaleTimeString()}</text>
        {pts.map((s, i) => {
          let d = "";
          let pen = false;
          for (const [x, y] of s.values!) {
            if (y === null) { pen = false; continue; }
            d += `${pen ? "L" : "M"}${sx(x).toFixed(1)},${sy(y).toFixed(1)}`;
            pen = true;
          }
          return <path key={i} d={d} fill="none" stroke={COLORS[i % COLORS.length]} strokeWidth={1.6} vectorEffect="non-scaling-stroke" />;
        })}
        {hover !== null && <line x1={sx(hover)} x2={sx(hover)} y1={P.t} y2={H - P.b} style={{ stroke: "var(--muted)" }} strokeDasharray="3 3" />}
      </svg>
      <div className="legend">
        {pts.map((s, i) => {
          const near = hover !== null ? s.values!.reduce((a, b) => (Math.abs(b[0] - hover) < Math.abs(a[0] - hover) ? b : a)) : null;
          return <span key={i} style={{ ["--c" as any]: COLORS[i % COLORS.length] }}>{seriesLabel(s.metric)}{near && near[1] !== null ? `: ${fmt(near[1])}` : ""}</span>;
        })}
        {series.length > pts.length && <span className="muted">+{series.length - pts.length} more series not drawn</span>}
      </div>
    </div>
  );
}

export function fmtBytes(v: number): string {
  const u = ["B", "KiB", "MiB", "GiB", "TiB"];
  let i = 0;
  while (Math.abs(v) >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(v < 10 && i ? 1 : 0)} ${u[i]}`;
}

function LogRow({ l, wrap }: { l: LogLine; wrap: boolean }) {
  const [open, setOpen] = useState(false);
  const lvl = levelOf(l);
  const where = [l.environment, l.labels?.namespace, l.labels?.pod].filter(Boolean).join("/");
  return (
    <div className={`log-row${lvl === "error" ? " err" : ""}${open ? " open" : ""}`}>
      <button type="button" className={`log-line${wrap ? " wrap" : ""}`} aria-expanded={open} onClick={() => setOpen(!open)}>
        {l.ts && <time className="log-ts" dateTime={l.ts}>{new Date(l.ts).toLocaleTimeString()}</time>}
        {lvl && <span className={`log-lvl ${lvl}`}>{lvl}</span>}
        {where && <span className="log-src">{where}</span>}
        <span className="log-text">{l.line}</span>
      </button>
      {open && <div className="log-detail">
        <pre>{l.line}</pre>
        <dl className="kv small">
          {l.ts && <><dt>Time</dt><dd>{new Date(l.ts).toLocaleString(undefined, { timeZoneName: "short" })}</dd></>}
          {Object.entries(l.labels ?? {}).map(([k, v]) => <div key={k} className="kv-row"><dt className="mono">{k}</dt><dd className="mono">{String(v)}</dd></div>)}
        </dl>
        <CopyButton text={l.line} label="Copy line" />
      </div>}
    </div>
  );
}

export interface LogLine { ts?: string; line: string; labels?: Record<string, string>; environment?: string }

export function levelOf(row: LogLine): string {
  const v = (row.labels?.level ?? /\blevel\W{0,3}(\w+)/i.exec(row.line)?.[1] ?? (/\b(error|exception|fatal|panic)\b/i.test(row.line) ? "error" : "")).toLowerCase();
  if (/err|fatal|crit|panic/.test(v)) return "error";
  if (/warn/.test(v)) return "warn";
  return /info|debug|trace/.test(v) ? v : "";
}

/** Short lines by default, so a page of logs can be scanned. Opening one shows it whole, wrapped inside its own box. */
export function LogLines({ lines, label = "Log lines", tall }: { lines: LogLine[]; label?: string; tall?: boolean }) {
  const [wrap, setWrap] = useState(false);
  if (!lines.length) return <Empty title="No log lines." />;
  return (
    <div className="stack">
      <div className="row end"><label className="inline small"><input type="checkbox" checked={wrap} onChange={(e) => setWrap(e.target.checked)} /> Wrap lines</label></div>
      <div className={`logbox${tall ? " tall" : ""}`} role="region" aria-label={label} tabIndex={0}>{lines.map((l, i) => <LogRow key={i} l={l} wrap={wrap} />)}</div>
    </div>
  );
}

export function SourceRef({ source }: { source?: Record<string, any> | null }) {
  if (!source) return null;
  return (
    <div className="small muted stack" style={{ gap: 2 }}>
      {source.query && <div>Query: <code>{source.query}</code></div>}
      {source.queries && (source.queries as string[]).map((q, i) => <div key={i}>Query: <code>{q}</code></div>)}
      {source.api && <div>API: <code>{source.api}</code></div>}
      {source.link && <a href={source.link} target="_blank" rel="noreferrer noopener">Open in Prometheus ↗</a>}
    </div>
  );
}

export { qs };
