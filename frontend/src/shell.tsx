import { createContext, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Link, NavLink, useLocation } from "react-router-dom";
import { api } from "./api";
import { useAuth } from "./auth";
import { Badge, useInterval } from "./components";
import { navFor, titleFor } from "./nav";
import { Icon, useModal } from "./ui";

const THEME_KEY = "moks.theme";
const COLLAPSE_KEY = "moks.sidebar";

export function applyStoredTheme() {
  document.documentElement.dataset.theme = localStorage.getItem(THEME_KEY) === "light" ? "light" : "dark";
}

function Brand({ wordmark = true }: { wordmark?: boolean }) {
  return (
    <Link to="/" className="brand" aria-label="MOKS home">
      <svg width="26" height="26" viewBox="0 0 32 32" aria-hidden="true"><rect width="32" height="32" rx="7" fill="var(--panel-2)" /><path d="M7 23V9l9 8 9-8v14" stroke="var(--accent)" strokeWidth="3" fill="none" strokeLinejoin="round" /></svg>
      {wordmark && <span>MOKS</span>}
    </Link>
  );
}

function NavList({ collapsed = false, label }: { collapsed?: boolean; label: string }) {
  const { can } = useAuth();
  const groups = useMemo(() => navFor(can), [can]);
  return (
    <nav aria-label={label} className="nav-list">
      {groups.map((g) => (
        <div key={g.label} role="group" aria-label={g.label}>
          {!collapsed && <div className="nav-group">{g.label}</div>}
          {g.items.map((i) => (
            <NavLink key={i.to} to={i.to} end={i.end} aria-label={collapsed ? i.label : undefined} data-tip={collapsed ? i.label : undefined}>
              <Icon name={i.icon} />{!collapsed && <span>{i.label}</span>}
            </NavLink>
          ))}
        </div>
      ))}
    </nav>
  );
}

function ProfileMenu({ commit, version }: { commit?: string; version?: string }) {
  const { me, logout } = useAuth();
  const [open, setOpen] = useState(false);
  const [theme, setTheme] = useState(() => document.documentElement.dataset.theme ?? "dark");
  const box = useRef<HTMLDivElement>(null);
  const [mixed, setMixed] = useState(false);
  useEffect(() => {
    if (open) api<{ mixed: boolean }>("/system/versions").then((r) => setMixed(r.mixed), () => setMixed(false));
  }, [open]);
  useEffect(() => {
    if (!open) return;
    const click = (e: MouseEvent) => { if (!box.current?.contains(e.target as Node)) setOpen(false); };
    const key = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", click);
    document.addEventListener("keydown", key);
    return () => { document.removeEventListener("mousedown", click); document.removeEventListener("keydown", key); };
  }, [open]);
  function toggleTheme() {
    const next = theme === "dark" ? "light" : "dark";
    localStorage.setItem(THEME_KEY, next);
    document.documentElement.dataset.theme = next;
    setTheme(next);
  }
  const initial = (me?.display_name || me?.email || "?").slice(0, 1).toUpperCase();
  return (
    <div className="profile" ref={box}>
      <button type="button" className="avatar" aria-label="Profile menu" aria-haspopup="true" aria-expanded={open} onClick={() => setOpen(!open)}>{initial}</button>
      {open && (
        <div className="menu">
          <div className="menu-head"><strong>{me?.display_name}</strong><div className="small muted">{me?.email}</div>
            <div className="small muted">{me?.roles.map((r) => `${r.role}@${r.environment}/${r.namespace}`).join(", ") || "no roles"}</div></div>
          <button type="button" className="ghost" onClick={toggleTheme}>{theme === "dark" ? "Use light theme" : "Use dark theme"}</button>
          <button type="button" className="ghost" onClick={logout}>Sign out</button>
          <div className="small muted">MOKS {version} · <span className="mono">{commit?.slice(0, 12)}</span></div>
          {mixed && <div className="alert warn small">Components are running different builds. A rollout may be incomplete.</div>}
        </div>
      )}
    </div>
  );
}

function useConnection(): { state: "connected" | "offline"; commit?: string; version?: string } {
  const [ok, setOk] = useState(true);
  const [build, setBuild] = useState<{ commit?: string; version?: string }>({});
  const check = () => { api<{ commit?: string; version?: string }>("/health").then((h) => { setOk(true); setBuild(h); }, () => setOk(false)); };
  useInterval(check, 30000);
  useEffect(() => {
    check();
    const down = () => setOk(false);
    window.addEventListener("online", check);
    window.addEventListener("offline", down);
    return () => { window.removeEventListener("online", check); window.removeEventListener("offline", down); };
  }, []);
  return { state: ok ? "connected" : "offline", ...build };
}

export interface HeaderScope { environment: string; namespace?: string; production?: boolean }
const ScopeCtx = createContext<(s: HeaderScope | null) => void>(() => {});

/** A page says which environment it is showing, and the header makes production obvious. */
export function useHeaderScope(scope: HeaderScope | null) {
  const set = useContext(ScopeCtx);
  const key = JSON.stringify(scope);
  useEffect(() => {
    set(scope);
    return () => set(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, set]);
}

/** Keeps the layout above the on-screen keyboard, including browsers without dvh resizing. */
function useViewportHeight() {
  useEffect(() => {
    const vv = window.visualViewport;
    if (!vv) return;
    const root = document.documentElement.style;
    const set = () => { root.setProperty("--vvh", `${vv.height}px`); root.setProperty("--vvt", `${vv.offsetTop}px`); }; // iOS moves the viewport instead of resizing it
    set();
    vv.addEventListener("resize", set);
    vv.addEventListener("scroll", set);
    return () => { vv.removeEventListener("resize", set); vv.removeEventListener("scroll", set); };
  }, []);
}

const FLUSH = /^\/(investigations\/?$|conversations\/)/;

export function Layout({ children }: { children: ReactNode }) {
  const loc = useLocation();
  const [drawer, setDrawer] = useState(false);
  const [collapsed, setCollapsed] = useState(() => localStorage.getItem(COLLAPSE_KEY) === "1");
  const [scope, setScope] = useState<HeaderScope | null>(null);
  const { state: connection, commit, version } = useConnection();
  const panel = useRef<HTMLDivElement>(null);
  const title = titleFor(loc.pathname);
  useViewportHeight();
  useModal(panel, drawer, () => setDrawer(false));
  useEffect(() => { setDrawer(false); }, [loc.pathname]);
  function toggle() {
    localStorage.setItem(COLLAPSE_KEY, collapsed ? "0" : "1");
    setCollapsed(!collapsed);
  }
  return (
    <ScopeCtx.Provider value={setScope}>
      <a className="skip" href="#main">Skip to content</a>
      <div className={`shell${collapsed ? " collapsed" : ""}`} {...(drawer ? { "data-drawer": "open" } : {})}>
        <aside className="side">
          <Brand wordmark={!collapsed} />
          <NavList collapsed={collapsed} label="Main" />
          <button type="button" className="ghost collapse" onClick={toggle} aria-expanded={!collapsed} aria-label={collapsed ? "Expand navigation" : "Collapse navigation"}>
            <Icon name={collapsed ? "expand" : "collapse"} />{!collapsed && <span>Collapse</span>}
          </button>
        </aside>
        <header className="top">
          <button type="button" className="ghost icon menu-btn" aria-label="Open navigation" aria-expanded={drawer} onClick={() => setDrawer(true)}><Icon name="menu" size={22} /></button>
          <div className="crumbs">{title.group && <span className="muted crumb-group">{title.group}</span>}<strong>{title.label}</strong></div>
          <div className="top-right">
            {scope && <span className="scope-chip mono">{scope.environment}{scope.namespace ? `/${scope.namespace}` : ""}</span>}
            {scope?.production && <Badge kind="warn">production</Badge>}
            <span className={`conn ${connection}`} role="status" title={connection === "connected" ? "Connected to MOKS" : "Cannot reach MOKS"}>
              <span className="sr-only">{connection === "connected" ? "Connected to MOKS" : "Cannot reach MOKS"}</span>
            </span>
            <ProfileMenu commit={commit} version={version} />
          </div>
        </header>
        <main id="main" tabIndex={-1} className={`main${FLUSH.test(loc.pathname) ? " flush" : ""}`}>{children}</main>
      </div>
      {drawer && (
        <div className="backdrop drawer-backdrop" onMouseDown={(e) => { if (e.target === e.currentTarget) setDrawer(false); }}>
          <div ref={panel} className="nav-drawer" role="dialog" aria-modal="true" aria-label="Navigation" tabIndex={-1}>
            <div className="row between"><Brand /><button type="button" className="ghost icon" aria-label="Close navigation" onClick={() => setDrawer(false)}><Icon name="close" size={22} /></button></div>
            <NavList label="Main menu" />
          </div>
        </div>
      )}
    </ScopeCtx.Provider>
  );
}
