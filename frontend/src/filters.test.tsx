import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, describe, expect, it } from "vitest";
import { type Chip, FilterPanel, useUrlFilters } from "./filters";

function Page() {
  const [f, set, clear] = useUrlFilters({ env: "all", level: "", size: 200, only: false });
  const loc = useLocation();
  const chips: Chip[] = [...(f.env !== "all" ? [{ key: "e", label: f.env, onRemove: () => set({ env: "all" }) }] : []), ...(f.level ? [{ key: "l", label: f.level, onRemove: () => set({ level: "" }) }] : [])];
  return (
    <>
      <FilterPanel chips={chips} onClear={clear} advanced={<label>Level<input aria-label="Level" value={f.level} onChange={(e) => set({ level: e.target.value })} /></label>}>
        <button type="button" onClick={() => set({ env: "mk4" })}>Pick mk4</button>
      </FilterPanel>
      <output data-testid="url">{loc.search}</output>
      <output data-testid="size">{typeof f.size}:{f.size}</output>
    </>
  );
}
const app = (path = "/logs") => render(<MemoryRouter initialEntries={[path]}><Page /></MemoryRouter>);
const url = () => screen.getByTestId("url").textContent;
const toggle = () => screen.getByRole("button", { name: /Filters/ });

afterEach(cleanup);

describe("FilterPanel", () => {
  it("collapses without losing what is selected, and keeps the scope visible", () => {
    app("/logs?env=mk4&level=error");
    expect(toggle().getAttribute("aria-expanded")).toBe("false");
    expect(toggle().textContent).toContain("2");
    expect(screen.getByLabelText("Level").closest("[hidden]")).not.toBeNull();
    fireEvent.click(toggle());
    expect(toggle().getAttribute("aria-expanded")).toBe("true");
    expect((screen.getByLabelText("Level") as HTMLInputElement).value).toBe("error");
    fireEvent.click(toggle());
    expect(screen.getByRole("button", { name: "Remove filter mk4" })).toBeTruthy();
    expect(url()).toBe("?env=mk4&level=error");
  });

  it("removes one filter or all of them", () => {
    app("/logs?env=mk4&level=error");
    fireEvent.click(screen.getByRole("button", { name: "Remove filter error" }));
    expect(url()).toBe("?env=mk4");
    fireEvent.click(screen.getByRole("button", { name: "Clear all" }));
    expect(url()).toBe("");
    expect(screen.queryByRole("button", { name: /Remove filter/ })).toBeNull();
  });
});

describe("useUrlFilters", () => {
  it("makes a filtered view shareable and reads values back with their types", () => {
    app("/logs?size=50");
    fireEvent.click(screen.getByText("Pick mk4"));
    expect(url()).toBe("?size=50&env=mk4");
    expect(screen.getByTestId("size").textContent).toBe("number:50");
  });
  it("keeps the other parameters of the page", () => {
    app("/logs?view=logql&env=mk4");
    fireEvent.click(screen.getByRole("button", { name: "Clear all" }));
    expect(url()).toBe("?view=logql");
  });
  it("leaves defaults out of the URL", () => {
    app("/logs?env=mk4");
    fireEvent.click(screen.getByRole("button", { name: "Remove filter mk4" }));
    expect(url()).toBe("");
  });
});
