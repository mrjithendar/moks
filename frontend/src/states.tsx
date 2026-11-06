import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { ApiError } from "./api";
import type { Multi } from "./components";
import { CopyButton, Skeleton } from "./ui";

/** Every state a list can be in, said the same way everywhere: what happened, why, and what to do next.
 *  Hiding something is never the access control: "denied" only explains what the server refused. */
export type StateKind = "loading" | "empty" | "stale" | "partial" | "unavailable" | "denied";

const TITLE: Record<StateKind, string> = {
  loading: "Loading", empty: "Nothing to show", stale: "Showing earlier results", partial: "Partial results",
  unavailable: "Unavailable", denied: "You don't have access",
};
const TONE: Record<StateKind, string> = { loading: "", empty: "", stale: "warn", partial: "warn", unavailable: "error", denied: "warn" };
const ICON: Record<StateKind, string> = { loading: "…", empty: "∅", stale: "⏱", partial: "◐", unavailable: "✕", denied: "⊘" };

export function StateView({ kind, title, why, next, children }: { kind: StateKind; title?: string; why?: ReactNode; next?: ReactNode; children?: ReactNode }) {
  if (kind === "loading") return <div className="state" data-state="loading"><Skeleton rows={6} label={title ?? "Loading"} /></div>;
  const urgent = kind === "unavailable" || kind === "denied";
  return (
    <div className={`state ${TONE[kind] ? `alert ${TONE[kind]}` : "empty"}`} data-state={kind} role={urgent ? "alert" : "status"}>
      <div className="state-title"><span aria-hidden="true" className="state-icon">{ICON[kind]}</span><strong>{title ?? TITLE[kind]}</strong></div>
      {why && <div className="small state-why">{why}</div>}
      {children}
      {next && <div className="small state-next"><span className="state-k">What you can do:</span> {next}</div>}
    </div>
  );
}

/** States for a fan-out read (/multi): stale, partial (failed sources or results cut short), unavailable, denied. */
export function multiStates({ data, error, reload, what }: { data?: Multi & { truncated?: { environment: string; namespace: string | null; shown: number; summary: string }[] }; error?: unknown; reload: () => void; what: string }): ReactNode[] {
  const out: ReactNode[] = [];
  const retry = <button type="button" className="ghost small" onClick={reload}>Try again</button>;
  if (error instanceof ApiError && error.status === 403) {
    out.push(<StateView key="denied" kind="denied" why={error.message} next={<>Ask an administrator for a role that includes this environment, or <Link to="/">go to Overview</Link>.</>} />);
    return out;
  }
  if (error && !data) {
    const ref = error instanceof ApiError ? error.requestId : undefined;
    out.push(<StateView key="unavailable" kind="unavailable" title={`${what[0].toUpperCase()}${what.slice(1)} unavailable`} why={error instanceof Error ? error.message : String(error)}
      next={<>{retry}{ref && <> Reference <code>{ref}</code> <CopyButton text={ref} /></>}</>} />);
    return out;
  }
  if (error && data) out.push(<StateView key="stale" kind="stale" why={`Refreshing failed (${error instanceof Error ? error.message : String(error)}). The list below is from the last successful read.`} next={retry} />);
  const denied = data?.errors.filter((e) => e.denied) ?? [];
  const failed = data?.errors.filter((e) => !e.denied) ?? [];
  const cut = data?.truncated ?? [];
  if (failed.length || cut.length || denied.length) {
    out.push(
      <StateView key="partial" kind="partial" why={`Some ${what} are not in this list.`}
        next={cut.length ? "Choose one namespace, or filter, to read fewer at a time." : failed.length ? retry : "Ask an administrator if you need these environments."}>
        <ul className="small state-list">
          {cut.map((c) => <li key={`t${c.environment}/${c.namespace}`}><span className="mono">{c.environment}{c.namespace ? `/${c.namespace}` : ""}</span>: showing {c.shown}, cut to MOKS's result size limit ({c.summary.split(";")[0]})</li>)}
          {failed.map((e, i) => <li key={`f${i}`}><span className="mono">{e.environment}{e.namespace ? `/${e.namespace}` : ""}</span>: could not be read ({e.error})</li>)}
          {denied.map((e, i) => <li key={`d${i}`}><span className="mono">{e.environment}{e.namespace ? `/${e.namespace}` : ""}</span>: not readable with your roles</li>)}
        </ul>
      </StateView>);
  }
  return out;
}
