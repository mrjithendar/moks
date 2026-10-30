import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it } from "vitest";
import { MultiSelect } from "./Metrics";

function Harness() {
  const [v, setV] = useState<string[]>([]);
  return <div><span>outside</span><MultiSelect label="Environments" allLabel="All authorized environments" options={[{ value: "mk4", label: "Minikube" }, { value: "eks" }]} value={v} onChange={setV} /></div>;
}

afterEach(cleanup);

const open = () => fireEvent.click(screen.getByRole("button", { name: /^Environments:/ }));

describe("MultiSelect", () => {
  it("selects an option and shows it, staying open for multiple choices", () => {
    render(<Harness />);
    expect(screen.queryByRole("listbox")).toBeNull();
    open();
    fireEvent.click(screen.getByRole("option", { name: "Minikube" }));
    expect(screen.getByRole("button", { name: /^Environments:/ }).textContent).toContain("Minikube");
    fireEvent.click(screen.getByRole("option", { name: "eks" }));
    expect(screen.getByRole("button", { name: /^Environments:/ }).textContent).toContain("2 selected");
  });

  it("closes on an outside click and on Escape, so it never covers other filters", () => {
    render(<Harness />);
    open();
    fireEvent.mouseDown(screen.getByText("outside"));
    expect(screen.queryByRole("listbox")).toBeNull();
    open();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("listbox")).toBeNull();
  });

  it("returns to all environments in one click", () => {
    render(<Harness />);
    open();
    fireEvent.click(screen.getByRole("option", { name: "Minikube" }));
    fireEvent.click(screen.getByRole("option", { name: "All authorized environments" }));
    expect(screen.getByRole("button", { name: /^Environments:/ }).textContent).toContain("All authorized environments");
    expect(screen.queryByRole("listbox")).toBeNull();
  });
});
