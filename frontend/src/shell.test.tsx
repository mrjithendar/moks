import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { navFor, titleFor } from "./nav";

const auth = vi.hoisted(() => ({ perms: [] as string[] }));
vi.mock("./auth", () => ({ useAuth: () => ({ me: { display_name: "Sam", email: "s@x", roles: [] }, logout: vi.fn(), can: (p: string) => auth.perms.includes(p) }) }));
vi.mock("./api", async (orig) => ({ ...(await orig<any>()), api: vi.fn(async () => ({ status: "ok" })) }));

import { Layout } from "./shell";

function app(path = "/") {
  return render(<MemoryRouter initialEntries={[path]}><Layout><Routes><Route path="*" element={<p>page</p>} /></Routes></Layout></MemoryRouter>);
}
const open = () => fireEvent.click(screen.getByRole("button", { name: "Open navigation" }));

beforeEach(() => { auth.perms = ["investigation.read", "investigation.run", "explorer.read", "admin.users"]; });
afterEach(() => { cleanup(); document.body.className = ""; });

describe("navigation drawer", () => {
  it("opens with every authorized destination and moves focus inside", () => {
    app();
    open();
    const d = screen.getByRole("dialog", { name: "Navigation" });
    const links = within(d).getAllByRole("link").map((a) => a.textContent);
    for (const l of ["Overview", "Metrics", "Conversations", "Incidents", "Users & roles"]) expect(links).toContain(l);
    expect(d.contains(document.activeElement)).toBe(true);
    expect(document.body.classList.contains("scroll-locked")).toBe(true);
  });

  it("closes on Escape, unlocks the page and returns focus to the menu button", () => {
    app();
    const btn = screen.getByRole("button", { name: "Open navigation" });
    btn.focus();
    fireEvent.click(btn);
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.body.classList.contains("scroll-locked")).toBe(false);
    expect(document.activeElement).toBe(btn);
  });

  it("closes when the backdrop is tapped", () => {
    app();
    open();
    fireEvent.mouseDown(screen.getByRole("dialog").parentElement!);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("closes after navigating", async () => {
    app();
    open();
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("link", { name: "Metrics" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(screen.getAllByText("Metrics").length).toBeGreaterThan(0);
  });

  it("keeps Tab inside the drawer", () => {
    app();
    open();
    const d = screen.getByRole("dialog");
    const items = [...d.querySelectorAll<HTMLElement>("a[href], button")];
    items[items.length - 1].focus();
    act(() => { fireEvent.keyDown(document, { key: "Tab" }); });
    expect(document.activeElement).toBe(items[0]);
  });
});

describe("RBAC visibility", () => {
  it("lists only what the role may use, and drops empty groups", () => {
    const groups = navFor((p) => p === "investigation.read");
    expect(groups.map((g) => g.label)).toEqual(["Monitor", "Investigate", "Knowledge"]);
    const items = groups.flatMap((g) => g.items.map((i) => i.label));
    expect(items).not.toContain("New investigation");
    expect(items).not.toContain("Audit log");
    expect(items).not.toContain("Metrics");
  });

  it("the drawer and the sidebar show the same destinations", () => {
    auth.perms = ["investigation.read"];
    app();
    const side = within(document.querySelector(".side") as HTMLElement).getAllByRole("link").map((a) => a.textContent).filter((t) => t !== "MOKS");
    open();
    const drawer = within(screen.getByRole("dialog")).getAllByRole("link").map((a) => a.textContent).filter((t) => t !== "MOKS");
    expect(drawer).toEqual(side);
  });

  it("names deep links for the header", () => {
    expect(titleFor("/conversations/abc").label).toBe("Conversations");
    expect(titleFor("/admin/users").group).toBe("Admin");
  });
});
