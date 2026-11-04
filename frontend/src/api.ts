// API client. Everything is relative to the Vite base (import.meta.env.BASE_URL, "/moks/"),
// so the same build works at any base path the backend is configured with.

export const BASE = import.meta.env.BASE_URL.replace(/\/$/, ""); // "/moks"
export const API = `${BASE}/api`;

let csrfToken: string | null = null;
export function setCsrf(token: string | null) {
  csrfToken = token;
}

/** Drafts, unsent queries and search text live only in this tab's sessionStorage, under "moks.". They
 *  go when the tab closes, and on sign-out or an expired session, so a shared browser keeps nothing. */
export function clearLocalWork() {
  for (const key of Object.keys(sessionStorage)) if (key.startsWith("moks.")) sessionStorage.removeItem(key);
}

export interface FieldError { field: string; message: string }

/** A failed API call. `requestId` is the reference the server logged the request under (the trace
 *  id when tracing is on); `code` is a stable machine name such as "not_found" or "connector_timeout". */
export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
    public code?: string,
    public requestId?: string,
    public fields: FieldError[] = [],
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/** Builds an ApiError from a response body in the server's error shape
 *  ({detail, error: {code, message, request_id, fields}}), or from whatever came back instead. */
export function toApiError(status: number, data: unknown, statusText: string, headerRequestId?: string | null): ApiError {
  const body = data && typeof data === "object" ? (data as Record<string, any>) : null;
  const err = body?.error && typeof body.error === "object" ? body.error : null;
  const requestId = (err?.request_id as string | undefined) || headerRequestId || undefined;
  let message: string;
  if (typeof err?.message === "string" && err.message) message = err.message;
  else if (typeof body?.detail === "string" && body.detail) message = body.detail;
  else if (Array.isArray(body?.detail)) message = body.detail.map((d: any) => `${(d.loc ?? []).slice(1).join(".") || "request"}: ${d.msg}`).join("; ");
  else if (status >= 502 && status <= 504) message = `MOKS is not reachable right now (HTTP ${status}). Try again in a moment.`;
  else message = statusText || `Request failed (HTTP ${status})`;
  return new ApiError(status, message, err?.code, requestId, Array.isArray(err?.fields) ? err.fields : []);
}

const OFFLINE = "Could not reach MOKS. Check your connection and try again.";

async function send(url: string, init: RequestInit): Promise<Response> {
  try {
    return await fetch(url, init);
  } catch (e) {
    if (e instanceof DOMException && e.name === "AbortError") throw e;
    throw new ApiError(0, OFFLINE, "network_error");
  }
}

type Json = Record<string, unknown> | unknown[] | null;

export async function api<T = any>(path: string, opts: { method?: string; body?: Json; signal?: AbortSignal } = {}): Promise<T> {
  const method = opts.method ?? "GET";
  const headers: Record<string, string> = { Accept: "application/json" };
  if (opts.body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET" && csrfToken) headers["X-CSRF-Token"] = csrfToken;
  const res = await send(`${API}${path}`, {
    method,
    headers,
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
    credentials: "same-origin",
    signal: opts.signal,
  });
  if (res.status === 401 && !path.startsWith("/auth/")) {
    const next = encodeURIComponent(window.location.pathname + window.location.search);
    clearLocalWork();
    window.location.assign(`${BASE}/login?next=${next}`);
    throw new ApiError(401, "Not signed in");
  }
  const text = await res.text();
  const data = text ? safeJson(text) : null;
  if (!res.ok) throw toApiError(res.status, data, res.statusText, res.headers.get("x-request-id"));
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

/** Multipart upload (CSRF header included; the browser sets the boundary). */
export async function apiForm<T = any>(path: string, form: FormData): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (csrfToken) headers["X-CSRF-Token"] = csrfToken;
  const res = await send(`${API}${path}`, { method: "POST", headers, body: form, credentials: "same-origin" });
  const data = await res.json().catch(() => null);
  if (!res.ok) throw toApiError(res.status, data, res.statusText, res.headers.get("x-request-id"));
  return data as T;
}
