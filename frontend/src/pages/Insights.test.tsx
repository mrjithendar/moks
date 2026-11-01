import { describe, expect, it } from "vitest";
import { parseUrls, toCsv } from "./Insights";

describe("knowledge URLs", () => {
  it("takes one per line, or a pasted list, without repeats", () => {
    expect(parseUrls("https://a.io/x\nhttps://b.io/y, https://a.io/x  https://c.io")).toEqual(["https://a.io/x", "https://b.io/y", "https://c.io"]);
  });
});

describe("FinOps export", () => {
  it("quotes cells and keeps cluster-supplied names from running as formulas", () => {
    const csv = toCsv([{ name: '=HYPERLINK("x")', cost: 1.5 }], [["Name", (r) => r.name], ["Cost", (r) => r.cost]]);
    expect(csv).toBe('"Name","Cost"\r\n"\'=HYPERLINK(""x"")","1.5"\r\n');
  });
});
