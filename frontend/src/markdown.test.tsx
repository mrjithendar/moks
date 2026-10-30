import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { Markdown, safeUrl } from "./markdown";

afterEach(cleanup);

describe("safe Markdown", () => {
  it("renders structure", () => {
    const { container } = render(<Markdown text={"# Title\n\n- one\n- **two**\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n```\nkubectl get pods\n```"} />);
    expect(container.querySelector("h4")?.textContent).toBe("Title");
    expect(container.querySelectorAll("li")).toHaveLength(2);
    expect(container.querySelector("strong")?.textContent).toBe("two");
    expect(container.querySelector("td")?.textContent).toBe("1");
    expect(container.querySelector("pre")?.textContent).toBe("kubectl get pods");
    expect(screen.getByRole("button", { name: "Copy" })).toBeTruthy();
  });

  it("keeps HTML as text", () => {
    const { container } = render(<Markdown text={'<img src=x onerror="alert(1)"> <script>alert(2)</script>'} />);
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("script")).toBeNull();
    expect(container.textContent).toContain("<script>");
  });

  it.each(["javascript:alert(1)", "JaVaScRiPt:alert(1)", "data:text/html,x", "vbscript:x", "//evil.example", "java\tscript:alert(1)"])("refuses %s", (u) => {
    expect(safeUrl(u)).toBeNull();
  });

  it("drops an unsafe link but keeps its label", () => {
    const { container } = render(<Markdown text="[click](javascript:alert(1))" />);
    expect(container.querySelector("a")).toBeNull();
    expect(container.textContent).toContain("click");
  });

  it("opens safe links without leaking the opener", () => {
    const { container } = render(<Markdown text="[docs](https://example.com/x)" />);
    expect(container.querySelector("a")?.getAttribute("rel")).toContain("noopener");
  });
});
