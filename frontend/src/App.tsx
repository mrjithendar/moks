import { lazy, Suspense, type ComponentType, type ReactNode } from "react";
import { Link, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { useAuth } from "./auth";
import { Layout } from "./shell";
import Login from "./pages/Login";

// Each area loads on first use, so first paint does not pay for pages never opened.
const admin = () => import("./pages/Admin");
const insights = () => import("./pages/Insights");
const investigate = () => import("./pages/Investigate");
const live = () => import("./pages/Live");
const ops = () => import("./pages/Ops");
const backups = () => import("./pages/Backups");
const page = <M,>(load: () => Promise<M>, name: keyof M) => lazy(() => load().then((m) => ({ default: m[name] as ComponentType })));

const Audit = page(admin, "Audit"), Connectors = page(admin, "Connectors"), Environments = page(admin, "Environments"), LLMProviders = page(admin, "LLMProviders"), Users = page(admin, "Users");
const Backups = page(backups, "Backups"), BackupStorage = page(backups, "BackupStorage");
const Workspace = lazy(() => import("./pages/Conversations").then((m) => ({ default: m.Workspace })));
const InvestigationView = page(investigate, "InvestigationView"), NewInvestigation = page(investigate, "NewInvestigation");
const Evals = page(insights, "Evals"), FinOps = page(insights, "FinOps"), Knowledge = page(insights, "Knowledge"), Skills = page(insights, "Skills");
const Explorer = page(live, "Explorer"), Logs = page(live, "Logs");
const Detections = lazy(() => import("./pages/Detections").then((m) => ({ default: m.Detections })));
const Metrics = lazy(() => import("./pages/Metrics").then((m) => ({ default: m.Metrics })));
const Actions = page(ops, "Actions"), DiagnosticView = page(ops, "DiagnosticView"), Diagnostics = page(ops, "Diagnostics"), IncidentView = page(ops, "IncidentView"), Incidents = page(ops, "Incidents"), PlanView = page(ops, "PlanView");
const Overview = lazy(() => import("./pages/Overview"));

/** Explains a page the role cannot use, instead of an empty page and a 403. The backend still decides. */
function Need({ any, children }: { any: string[]; children: ReactNode }) {
  const { can } = useAuth();
  if (any.some(can)) return children;
  return <div className="alert warn" role="alert"><h1>You don't have access to this page</h1>Ask an administrator for a role that includes it, or go back to <Link to="/">Overview</Link>.</div>;
}

export default function App() {
  const { me, loading } = useAuth();
  const loc = useLocation();
  if (loading) return <div className="empty">Loading…</div>;
  if (loc.pathname === "/login") return <Login />;
  if (!me) return <Navigate to={`/login?next=${encodeURIComponent(import.meta.env.BASE_URL.replace(/\/$/, "") + loc.pathname + loc.search)}`} replace />;
  return (
    <Layout>
      <Suspense fallback={<div className="empty">Loading…</div>}>
      <Routes>
        <Route path="/" element={<Overview />} />
        <Route path="/investigate" element={<NewInvestigation />} />
        <Route path="/investigations" element={<Workspace />} />
        <Route path="/investigations/:id" element={<InvestigationView />} />
        <Route path="/conversations/:id" element={<Workspace />} />
        <Route path="/backups" element={<Need any={["backup.read"]}><Backups /></Need>} />
        <Route path="/admin/backup-storage" element={<Need any={["admin.connectors"]}><BackupStorage /></Need>} />
        <Route path="/explorer" element={<Need any={["explorer.read"]}><Explorer /></Need>} />
        <Route path="/metrics" element={<Need any={["explorer.read"]}><Metrics /></Need>} />
        <Route path="/logs" element={<Need any={["explorer.read"]}><Logs /></Need>} />
        <Route path="/detections" element={<Detections />} />
        <Route path="/incidents" element={<Incidents />} />
        <Route path="/incidents/:id" element={<IncidentView />} />
        <Route path="/actions" element={<Need any={["action.request", "action.approve"]}><Actions /></Need>} />
        <Route path="/actions/:id" element={<Need any={["investigation.read", "action.approve"]}><PlanView /></Need>} />
        <Route path="/diagnostics" element={<Need any={["action.request"]}><Diagnostics /></Need>} />
        <Route path="/diagnostics/:id" element={<Need any={["action.request"]}><DiagnosticView /></Need>} />
        <Route path="/knowledge" element={<Knowledge />} />
        <Route path="/finops" element={<Need any={["finops.read"]}><FinOps /></Need>} />
        <Route path="/evals" element={<Need any={["admin.policies"]}><Evals /></Need>} />
        <Route path="/skills" element={<Skills />} />
        <Route path="/audit" element={<Need any={["audit.read"]}><Audit /></Need>} />
        <Route path="/admin/environments" element={<Need any={["admin.policies"]}><Environments /></Need>} />
        <Route path="/admin/connectors" element={<Need any={["admin.connectors"]}><Connectors /></Need>} />
        <Route path="/admin/llm" element={<Need any={["admin.llm"]}><LLMProviders /></Need>} />
        <Route path="/admin/users" element={<Need any={["admin.users"]}><Users /></Need>} />
        <Route path="*" element={<div className="empty"><h1>Page not found</h1><Link to="/">Back to Overview</Link></div>} />
      </Routes>
      </Suspense>
    </Layout>
  );
}
