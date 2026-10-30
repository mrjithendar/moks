// API client. Everything is relative to the Vite base (import.meta.env.BASE_URL, "/moks/"),
// so the same build works at any base path the backend is configured with.

export const BASE = import.meta.env.BASE_URL.replace(/\/$/, ""); // "/moks"
export const API = `${BASE}/api`;

let csrfToken: string | null = null;
export function setCsrf(token: string | null) {
  csrfToken = token;
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

type Json = Record<string, unknown> | unknown[] | null;

export async function api<T = any>(path: string, opts: { method?: string; body?: Json; signal?: AbortSignal } = {}): Promise<T> {
  const method = opts.method ?? "GET";
  const headers: Record<string, string> = { Accept: "application/json" };
  if (opts.body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET" && csrfToken) headers["X-CSRF-Token"] = csrfToken;
  const res = await fetch(`${API}${path}`, {
    method,
    headers,
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
    credentials: "same-origin",
    signal: opts.signal,
  });
  if (res.status === 401 && !path.startsWith("/auth/")) {
    const next = encodeURIComponent(window.location.pathname + window.location.search);
    window.location.assign(`${BASE}/login?next=${next}`);
    throw new ApiError(401, "Not signed in");
  }
  const text = await res.text();
  const data = text ? safeJson(text) : null;
  if (!res.ok) {
    const detail = data && typeof data === "object" && "detail" in data ? (data as any).detail : res.statusText;
    throw new ApiError(res.status, typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return data as T;
}

function safeJson(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

export function qs(params: Record<string, string | number | boolean | undefined | null>): string {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== "") p.set(k, String(v));
  const s = p.toString();
  return s ? `?${s}` : "";
}
