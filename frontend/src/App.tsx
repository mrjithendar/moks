import { Navigate, Route, Routes, useLocation } from "react-router-dom";
import { useAuth } from "./auth";
import { Layout } from "./components";
import { Audit, Connectors, Environments, LLMProviders, Users } from "./pages/Admin";
import { InvestigationList, InvestigationView, NewInvestigation } from "./pages/Investigate";
import { Explorer, Logs, Metrics } from "./pages/Live";
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
