import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DataTable, LogLines } from "./components";
import { ActionMenu, Identifier, Truncate } from "./ui";

const LONG = "payments-checkout-gateway-canary-7f9c6d8b5d-x2x9k-with-an-unusually-long-suffix";

function phone(width: number) {
  vi.stubGlobal("matchMedia", (q: string) => ({ matches: Number(/max-width: (\d+)/.exec(q)?.[1]) >= width, addEventListener() {}, removeEventListener() {} }));
}

beforeEach(() => { vi.stubGlobal("matchMedia", undefined); });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("long names", () => {
  it("keeps the whole name on the element while the CSS clips it to one line", () => {
    render(<Truncate text={LONG} mono />);
    const el = screen.getByText(LONG);
    expect(el.className).toContain("trunc");
    expect(el.getAttribute("title")).toBe(LONG);
  });

  it("clamps a human title to two lines rather than one", () => {
    render(<Truncate text="Checkout returning 502s for every customer in Europe" lines={2} />);
    expect(screen.getByText(/Checkout/).className).toContain("clamp-2");
  });

  it("discloses the full name and copies it, by keyboard and touch", async () => {
    const write = vi.fn(async () => {});
    Object.assign(navigator, { clipboard: { writeText: write } });
    render(<Identifier name={LONG} />);
    const more = screen.getByRole("button", { name: `Show full name: ${LONG}` });
    expect(more.getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(more);
    const pop = screen.getByRole("dialog", { name: "Full name" });
    expect(within(pop).getByText(LONG).className).toContain("name-full");
    await act(async () => { fireEvent.click(within(pop).getByRole("button", { name: "Copy name" })); });
    expect(write).toHaveBeenCalledWith(LONG);
    fireEvent.keyDown(document.body, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});

describe("overflow menu", () => {
  it("is named for what it acts on and works by keyboard", async () => {
    const del = vi.fn(), copy = vi.fn();
    render(<ActionMenu label="Actions for pod a" items={[{ label: "Copy name", run: copy }, { label: "Delete", danger: true, run: del }]} />);
    const trigger = screen.getByRole("button", { name: "Actions for pod a" });
    fireEvent.click(trigger);
    const menu = screen.getByRole("menu", { name: "Actions for pod a" });
    await waitFor(() => expect(document.activeElement?.textContent).toBe("Copy name"));
    fireEvent.keyDown(menu, { key: "ArrowDown" });
    expect(document.activeElement?.textContent).toBe("Delete");
    fireEvent.click(document.activeElement as Element);
    expect(del).toHaveBeenCalled();
    expect(copy).not.toHaveBeenCalled();
    expect(screen.queryByRole("menu")).toBeNull();
    expect(document.activeElement).toBe(trigger);
  });

  it("does not open the row it sits on", () => {
    const row = vi.fn();
    render(<div onClick={row}><ActionMenu label="Row" items={[{ label: "One", run: () => {} }]} /></div>);
    fireEvent.click(screen.getByRole("button", { name: "Row" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "One" }));
    expect(row).not.toHaveBeenCalled();
  });
});

const pods = [{ name: LONG, phase: "Running", namespace: "shop", node: "mk4-m02" }];
const cols = [
  { key: "n", head: "Pod", primary: true, cell: (p: any) => <Truncate text={p.name} mono /> },
  { key: "p", head: "Phase", status: true, cell: (p: any) => p.phase },
  { key: "ns", head: "Namespace", cell: (p: any) => p.namespace },
  { key: "node", head: "Node", wide: true, cell: (p: any) => p.node },
];

describe("DataTable", () => {
  it("is a real table with a labeled scroll region on large screens", () => {
    render(<DataTable label="Pods" rows={pods} cols={cols} rowKey={(p) => p.name} />);
    expect(screen.getByRole("region", { name: "Pods" })).toBeTruthy();
    expect(screen.getByRole("columnheader", { name: "Node" })).toBeTruthy();
  });

  it("is a list on phones, leading with name and status, without squeezing columns", () => {
    phone(820);
    render(<DataTable label="Pods" rows={pods} cols={cols} rowKey={(p) => p.name} onRow={() => {}} menu={(p) => ({ label: `Actions for ${p.name}`, items: [{ label: "Copy name", run: () => {} }] })} />);
    expect(screen.queryByRole("table")).toBeNull();
    const row = screen.getByRole("listitem");
    expect(within(row).getByText(LONG)).toBeTruthy();
    expect(within(row).getByText("Running")).toBeTruthy();
    expect(within(row).getByText("shop")).toBeTruthy();
    expect(within(row).queryByText("mk4-m02")).toBeNull();
    expect(within(row).getByRole("button", { name: `Actions for ${LONG}` })).toBeTruthy();
  });

  it("opens a row from the keyboard through a real button", () => {
    const open = vi.fn();
    render(<DataTable label="Pods" rows={pods} cols={cols} rowKey={(p) => p.name} onRow={open} />);
    fireEvent.click(screen.getByRole("button", { name: LONG }));
    expect(open).toHaveBeenCalledTimes(1);
  });
});

describe("log lines", () => {
  const line = { ts: "2026-10-31T00:00:00Z", line: `level=error msg="${"x".repeat(900)}"`, labels: { namespace: "shop", pod: "a" } };
  it("opens the whole line, and wraps on request, inside its own box", () => {
    render(<LogLines lines={[line]} />);
    expect(screen.getByRole("region", { name: "Log lines" })).toBeTruthy();
    const row = screen.getByRole("button", { expanded: false });
    expect(row.className).not.toContain("wrap");
    fireEvent.click(screen.getByLabelText("Wrap lines"));
    expect(screen.getByRole("button", { expanded: false }).className).toContain("wrap");
    fireEvent.click(screen.getByRole("button", { expanded: false }));
    expect(screen.getByRole("button", { name: "Copy line" })).toBeTruthy();
    expect(screen.getByText(line.line, { selector: "pre" })).toBeTruthy();
  });
});
