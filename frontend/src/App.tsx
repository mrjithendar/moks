import { Navigate, Route, Routes, useLocation } from "react-router-dom";
import { useAuth } from "./auth";
import { Layout } from "./components";
import { Audit, Connectors, Environments, LLMProviders, Users } from "./pages/Admin";
import { InvestigationList, InvestigationView, NewInvestigation } from "./pages/Investigate";
import { Evals, FinOps, Knowledge, Skills } from "./pages/Insights";
import { Explorer, Logs } from "./pages/Live";
import { Detections } from "./pages/Detections";
import { Metrics } from "./pages/Metrics";
import { Actions, DiagnosticView, Diagnostics, IncidentView, Incidents, PlanView } from "./pages/Ops";
import Login from "./pages/Login";
import Overview from "./pages/Overview";

export default function App() {
  const { me, loading } = useAuth();
  const loc = useLocation();
  if (loading) return <div className="empty">Loading…</div>;
  if (loc.pathname === "/login") return <Login />;
  if (!me) return <Navigate to={`/login?next=${encodeURIComponent(import.meta.env.BASE_URL.replace(/\/$/, "") + loc.pathname + loc.search)}`} replace />;
  return (
    <Layout>
      <Routes>
        <Route path="/" element={<Overview />} />
        <Route path="/investigate" element={<NewInvestigation />} />
        <Route path="/investigations" element={<InvestigationList />} />
        <Route path="/investigations/:id" element={<InvestigationView />} />
        <Route path="/explorer" element={<Explorer />} />
        <Route path="/metrics" element={<Metrics />} />
        <Route path="/logs" element={<Logs />} />
        <Route path="/detections" element={<Detections />} />
        <Route path="/incidents" element={<Incidents />} />
        <Route path="/incidents/:id" element={<IncidentView />} />
        <Route path="/actions" element={<Actions />} />
        <Route path="/actions/:id" element={<PlanView />} />
        <Route path="/diagnostics" element={<Diagnostics />} />
        <Route path="/diagnostics/:id" element={<DiagnosticView />} />
        <Route path="/knowledge" element={<Knowledge />} />
        <Route path="/finops" element={<FinOps />} />
        <Route path="/evals" element={<Evals />} />
        <Route path="/skills" element={<Skills />} />
        <Route path="/audit" element={<Audit />} />
        <Route path="/admin/environments" element={<Environments />} />
        <Route path="/admin/connectors" element={<Connectors />} />
        <Route path="/admin/llm" element={<LLMProviders />} />
        <Route path="/admin/users" element={<Users />} />
        <Route path="*" element={<div className="empty">Page not found.</div>} />
      </Routes>
    </Layout>
  );
}
