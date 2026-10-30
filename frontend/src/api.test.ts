import { afterEach, describe, expect, it, vi } from "vitest";
import { api, API, BASE, qs, setCsrf } from "./api";

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
