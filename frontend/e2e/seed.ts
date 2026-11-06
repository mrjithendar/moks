import { type APIRequestContext, type Page, request } from "@playwright/test";

export const ADMIN = "sre@moks.local";
export const VIEWER = "viewer@moks.local";
export const LONG_POD = "payments-reconciliation-ledger-writer-eu-central-1-7d9f8c6b5-x2k4q";

/** Dev-mode sign-in for a page: the session cookie lands in the page's context. */
export async function signIn(page: Page, email = ADMIN) {
  const r = await page.request.post("/moks/api/auth/dev-login", { data: { email } });
  if (!r.ok()) throw new Error(`dev-login ${email}: ${r.status()} ${await r.text()}`);
}

async function client(baseURL: string, email = ADMIN): Promise<{ api: APIRequestContext; headers: Record<string, string> }> {
  const api = await request.newContext({ baseURL });
  const r = await api.post("/moks/api/auth/dev-login", { data: { email } });
  if (!r.ok()) throw new Error(`dev-login: ${r.status()}`);
  const me = await (await api.get("/moks/api/auth/me")).json();
  return { api, headers: { "X-CSRF-Token": me.csrf_token, Origin: baseURL } };
}

const QUESTIONS = [
  `Why is ${LONG_POD} crashlooping in shop?`,
  "Is cart-redis-cache-0 running out of memory in shop?",
  "Why is the payments service returning 502 in shop?",
  "Are any pods restarting in shop?",
  "Is anything unhealthy in kube-system?",
  // enough rows that the inbox scrolls on a phone
  ...Array.from({ length: 20 }, (_, i) => `Check pod restarts in shop, round ${i + 1}`),
];

/** Investigations through the real API and worker (rules mode), plus incidents. Idempotent per server. */
export async function seed(baseURL: string): Promise<{ conversations: any[]; incidents: any[] }> {
  const { api, headers } = await client(baseURL);
  const existing = await (await api.get("/moks/api/conversations?limit=200")).json();
  if (existing.length >= QUESTIONS.length) {
    const incidents = await (await api.get("/moks/api/incidents")).json();
    await api.dispose();
    return { conversations: existing, incidents };
  }
  const started: string[] = [];
  for (const question of QUESTIONS) {
    const r = await api.post("/moks/api/investigations", { headers, data: { question, environment: "mk4", namespace: question.includes("kube-system") ? "kube-system" : "shop" } });
    if (!r.ok()) throw new Error(`investigation: ${r.status()} ${await r.text()}`);
    started.push((await r.json()).id);
  }
  for (const id of started) {
    for (let i = 0; i < 120; i++) {
      const inv = await (await api.get(`/moks/api/investigations/${id}`)).json();
      if (["completed", "failed", "cancelled"].includes(inv.status)) break;
      await new Promise((ok) => setTimeout(ok, 500));
    }
  }
  for (const [title, severity] of [["Payments ledger writer crashlooping", "sev2"], ["cart-redis-cache OOMKilled", "sev3"], ["Checkout latency above SLO", "sev1"]] as const) {
    await api.post("/moks/api/incidents", { headers, data: { title, environment: "mk4", namespace: "shop", severity, summary: `${title}. Opened by the fixture seed.` } });
  }
  const out = { conversations: await (await api.get("/moks/api/conversations?limit=200")).json(), incidents: await (await api.get("/moks/api/incidents")).json() };
  await api.dispose();
  return out;
}
