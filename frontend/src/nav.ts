/** Every destination in one place, so the sidebar and the mobile drawer can never disagree.
 *  `show` only decides what is listed: the backend enforces every permission itself. */
export type Can = (perm: string) => boolean;

export interface NavItem { to: string; label: string; icon: string; end?: boolean; show: (can: Can) => boolean }
export interface NavGroup { label: string; items: NavItem[] }

const read = (can: Can) => can("investigation.read");
const live = (can: Can) => can("explorer.read");

export const NAV: NavGroup[] = [
  { label: "Monitor", items: [
    { to: "/", label: "Overview", icon: "grid", end: true, show: () => true },
    { to: "/metrics", label: "Metrics", icon: "chart", show: live },
    { to: "/logs", label: "Logs", icon: "lines", show: live },
    { to: "/detections", label: "Detection rules", icon: "radar", show: read },
  ] },
  { label: "Investigate", items: [
    { to: "/investigate", label: "New investigation", icon: "plus", show: (can) => can("investigation.run") },
    { to: "/investigations", label: "Conversations", icon: "chat", show: read },
    { to: "/incidents", label: "Incidents", icon: "alert", show: read },
  ] },
  { label: "Operate", items: [
    { to: "/actions", label: "Approvals & actions", icon: "check", show: (can) => can("action.request") || can("action.approve") },
    { to: "/diagnostics", label: "Diagnostics", icon: "tool", show: (can) => can("action.request") },
    { to: "/explorer", label: "Kubernetes explorer", icon: "cube", show: live },
    { to: "/backups", label: "Backups", icon: "archive", show: (can) => can("backup.read") },
  ] },
  { label: "Knowledge", items: [
    { to: "/knowledge", label: "Runbooks & knowledge", icon: "book", show: read },
    { to: "/skills", label: "SRE skills", icon: "spark", show: read },
  ] },
  { label: "Cost", items: [{ to: "/finops", label: "FinOps", icon: "coin", show: (can) => can("finops.read") }] },
  { label: "Admin", items: [
    { to: "/audit", label: "Audit log", icon: "shield", show: (can) => can("audit.read") },
    { to: "/admin/environments", label: "Environments", icon: "layers", show: (can) => can("admin.policies") },
    { to: "/admin/connectors", label: "Connectors", icon: "plug", show: (can) => can("admin.connectors") },
    { to: "/admin/backup-storage", label: "Backup storage", icon: "cloud", show: (can) => can("admin.connectors") },
    { to: "/admin/llm", label: "LLM providers", icon: "cpu", show: (can) => can("admin.llm") },
    { to: "/admin/users", label: "Users & roles", icon: "users", show: (can) => can("admin.users") },
    { to: "/evals", label: "Evaluation", icon: "flask", show: (can) => can("admin.policies") },
  ] },
];

export function navFor(can: Can): NavGroup[] {
  return NAV.map((g) => ({ ...g, items: g.items.filter((i) => i.show(can)) })).filter((g) => g.items.length);
}

export function titleFor(path: string): { group?: string; label: string } {
  if (path.startsWith("/conversations/")) return { group: "Investigate", label: "Conversations" };
  if (path.startsWith("/investigations/")) return { group: "Investigate", label: "Investigation" };
  for (const g of NAV) for (const i of g.items) {
    if (i.end ? path === i.to : path === i.to || path.startsWith(`${i.to}/`)) return { group: g.label, label: i.label };
  }
  return { label: "MOKS" };
}
