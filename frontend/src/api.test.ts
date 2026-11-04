import { afterEach, describe, expect, it, vi } from "vitest";
import { api, API, ApiError, BASE, qs, setCsrf, toApiError } from "./api";

describe("base path", () => {
  it("serves the API under the Vite base (/moks)", () => {
    expect(BASE).toBe("/moks");
    expect(API).toBe("/moks/api");
  });
});

describe("api()", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    setCsrf(null);
  });

  it("sends the CSRF token only on unsafe methods", async () => {
    const calls: RequestInit[] = [];
    vi.stubGlobal("fetch", vi.fn(async (_url: string, init: RequestInit) => {
      calls.push(init);
      return new Response("{}", { status: 200 });
    }));
    setCsrf("tok");
    await api("/environments");
    await api("/environments", { method: "POST", body: { name: "x" } });
    expect((calls[0].headers as Record<string, string>)["X-CSRF-Token"]).toBeUndefined();
    expect((calls[1].headers as Record<string, string>)["X-CSRF-Token"]).toBe("tok");
    expect(calls[1].credentials).toBe("same-origin");
  });

  it("redirects to the login page under the base path on 401", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response('{"detail":"Not signed in"}', { status: 401 })));
    const assign = vi.fn();
    vi.stubGlobal("location", { ...window.location, pathname: "/moks/investigations/1", search: "", assign });
    await expect(api("/investigations")).rejects.toThrow();
    expect(assign).toHaveBeenCalledWith("/moks/login?next=%2Fmoks%2Finvestigations%2F1");
  });

  it("surfaces backend error details", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response('{"detail":"You need investigation.run in mk4/shop"}', { status: 403 })));
    await expect(api("/investigations", { method: "POST", body: {} })).rejects.toThrow("You need investigation.run in mk4/shop");
  });
});

describe("qs", () => {
  it("drops empty values", () => {
    expect(qs({ a: 1, b: "", c: undefined, d: false })).toBe("?a=1&d=false");
  });
});

describe("errors", () => {
  afterEach(() => vi.restoreAllMocks());

  it("reads the server's error shape: message, code, reference and fields", () => {
    const e = toApiError(422, { detail: "Invalid request — name: Field required", error: { code: "invalid_request", message: "Invalid request — name: Field required", status: 422, request_id: "abc123ef", fields: [{ field: "body.name", message: "Field required" }] } }, "Unprocessable");
    expect(e.message).toBe("Invalid request — name: Field required");
    expect(e.code).toBe("invalid_request");
    expect(e.requestId).toBe("abc123ef");
    expect(e.fields).toEqual([{ field: "body.name", message: "Field required" }]);
  });

  it("turns a bare FastAPI validation list into readable text, not JSON", () => {
    const e = toApiError(422, { detail: [{ loc: ["body", "limit"], msg: "Input should be less than 500" }] }, "");
    expect(e.message).toBe("limit: Input should be less than 500");
  });

  it("explains a proxy error page and keeps the header reference", () => {
    const e = toApiError(502, "<html>Bad Gateway</html>", "Bad Gateway", "req-12345678");
    expect(e.message).toMatch(/not reachable right now \(HTTP 502\)/);
    expect(e.requestId).toBe("req-12345678");
  });

  it("reports a network failure as an ApiError with status 0", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new TypeError("Failed to fetch"); }));
    const err = await api("/environments").catch((x) => x);
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(0);
    expect(err.code).toBe("network_error");
  });
});
