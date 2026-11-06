import { useCallback, useRef, useState } from "react";
import { useLocation, useSearchParams } from "react-router-dom";
import { useScopes } from "./components";
import { Floating, Icon } from "./ui";

/** One scope for the pages it affects, in the URL as `env` and `ns`, so it is shareable and survives Back and
 *  reloads. Moving between those pages keeps it. "all" or no value is every environment the person may read:
 *  the server decides which those are, so this is a filter, never an access control. */
export const SCOPED_PAGES = ["/explorer", "/finops", "/investigations", "/conversations/"];

export function isScoped(path: string): boolean {
  return SCOPED_PAGES.some((p) => (p.endsWith("/") ? path.startsWith(p) : path === p || path.startsWith(`${p}/`)));
}

export interface ScopeValue { env: string; ns: string }

export function readScope(search: string): ScopeValue {
  const p = new URLSearchParams(search);
  const env = p.get("env") || "all";
  return { env, ns: env === "all" ? "all" : p.get("ns") || "all" };
}

/** The current page's search string with only the scope kept, for links to other scoped pages. */
export function scopeSearch(search: string): string {
  const { env, ns } = readScope(search);
  const p = new URLSearchParams();
  if (env !== "all") p.set("env", env);
  if (env !== "all" && ns !== "all") p.set("ns", ns);
  const s = p.toString();
  return s ? `?${s}` : "";
}

export function scopeLabel(s: ScopeValue): string {
  if (s.env === "all") return "All authorized environments";
  return s.ns === "all" ? `${s.env} · all namespaces` : `${s.env} · ${s.ns}`;
}

export function useScope(): [ScopeValue, (s: Partial<ScopeValue>) => void] {
  const [params, setParams] = useSearchParams();
  const value = readScope(params.toString());
  const set = useCallback((s: Partial<ScopeValue>) => {
    setParams((prev) => {
      const next = new URLSearchParams(prev);
      const env = s.env ?? next.get("env") ?? "all";
      const ns = s.env !== undefined && s.ns === undefined ? "all" : s.ns ?? next.get("ns") ?? "all";
      if (env === "all") next.delete("env"); else next.set("env", env);
      if (env === "all" || ns === "all") next.delete("ns"); else next.set("ns", ns);
      return next;
    }, { replace: true });
  }, [setParams]);
  return [value, set];
}

/** The header's scope: on scoped pages a button that shows and changes it; on a conversation, the conversation's own. */
export function ScopeIndicator({ fixed }: { fixed?: { environment: string; namespace?: string } | null }) {
  const loc = useLocation();
  const [scope, setScope] = useScope();
  const [open, setOpen] = useState(false);
  const button = useRef<HTMLButtonElement>(null);
  const scopes = useScopes();
  if (fixed) {
    return <span className="scope-ind fixed" data-testid="scope-indicator"><span className="scope-k">Scope</span> <span className="mono">{fixed.environment}{fixed.namespace ? ` · ${fixed.namespace}` : ""}</span></span>;
  }
  if (!isScoped(loc.pathname)) return null;
  const list = scopes.data ?? [];
  const nss = list.find((x) => x.environment === scope.env)?.namespaces ?? [];
  return (
    <>
      <button ref={button} type="button" className="ghost scope-ind" data-testid="scope-indicator" aria-haspopup="dialog" aria-expanded={open} onClick={() => setOpen(!open)}>
        <span className="scope-k">Scope</span> <span className="scope-v">{scopeLabel(scope)}</span><Icon name="filter" size={16} />
      </button>
      {open && <Floating anchor={button.current} onClose={() => setOpen(false)} className="scope-pop" label="Scope" role="dialog">
        <div className="stack">
          <label>Environment
            <select value={scope.env} onChange={(e) => setScope({ env: e.target.value })} autoFocus>
              <option value="all">All authorized environments</option>
              {list.map((x) => <option key={x.environment} value={x.environment}>{x.display_name || x.environment}</option>)}
            </select></label>
          <label>Namespace
            <select value={scope.ns} disabled={scope.env === "all"} onChange={(e) => setScope({ ns: e.target.value })}>
              <option value="all">All namespaces</option>
              {nss.map((n: string) => <option key={n} value={n}>{n}</option>)}
            </select></label>
          <div className="small muted">Kept as you move between Investigations, the Kubernetes explorer and FinOps. You only ever see what your roles allow.</div>
          <button type="button" className="ghost small" onClick={() => setOpen(false)}>Done</button>
        </div>
      </Floating>}
    </>
  );
}
