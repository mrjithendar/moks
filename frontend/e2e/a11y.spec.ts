import { expect, type Page, test } from "@playwright/test";
import { seed, signIn } from "./seed";

// Accessibility checks for the milestone 1 screens, in Chrome on FIXTURE data. No axe-core (that would be a new
// dependency): contrast is computed from the rendered colours, and focus, targets and overflow are measured.
let conv = "";
test.beforeAll(async ({ baseURL }) => {
  test.setTimeout(180_000);
  conv = (await seed(baseURL!)).conversations.find((c: any) => c.title.includes("crashlooping")).id;
});

const SCREENS = () => [
  ["inbox", "/moks/investigations?env=mk4"],
  ["conversation", `/moks/conversations/${conv}`],
  ["explorer", "/moks/explorer?env=mk4&ns=shop"],
  ["rightsizing", "/moks/finops?env=mk4&ns=kube-system"],
] as const;
const CHANGED = ".claim, .status-pill, .rs-field, .rs-rec, .rs-head, .state, .scope-ind, .inbox-row, .rp-section, .cite-wrap, .exact-source, .rt-card, .rt";

async function open(page: Page, path: string) {
  await page.goto(path);
  await page.waitForLoadState("networkidle").catch(() => {});
  await page.waitForTimeout(500);
}

/** Text in the changed components below 4.5:1 against its effective background. */
function lowContrast(page: Page) {
  return page.evaluate((sel) => {
    const rgb = (c: string) => (c.match(/[\d.]+/g) ?? []).map(Number);
    const lum = ([r, g, b]: number[]) => { const f = (v: number) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; }; return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b); };
    const bg = (el: Element | null): number[] => {
      for (; el; el = el.parentElement) { const c = rgb(getComputedStyle(el).backgroundColor); if (c.length >= 3 && (c[3] ?? 1) > 0.5) return c; }
      return rgb(getComputedStyle(document.body).backgroundColor);
    };
    const bad: string[] = [];
    for (const root of document.querySelectorAll(sel)) {
      const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
      for (let n = walker.nextNode(); n; n = walker.nextNode()) {
        const el = n.parentElement!;
        if (!n.textContent!.trim() || el.closest(".sr-only") || !el.getClientRects().length) continue;
        const a = lum(rgb(getComputedStyle(el).color)), b = lum(bg(el));
        const ratio = (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
        if (ratio < 4.5) bad.push(`${ratio.toFixed(2)} "${n.textContent!.trim().slice(0, 30)}" in .${el.className}`);
      }
    }
    return [...new Set(bad)].slice(0, 15);
  }, CHANGED);
}

for (const theme of ["dark", "light"]) {
  test(`text contrast is at least 4.5:1 in the changed screens (${theme})`, async ({ page }) => {
    await signIn(page);
    await page.addInitScript((t) => localStorage.setItem("moks.theme", t), theme);
    await page.setViewportSize({ width: 1440, height: 900 });
    for (const [name, path] of SCREENS()) {
      await open(page, path);
      expect(await lowContrast(page), `${name} (${theme})`).toEqual([]);
    }
  });
}

test("at 390px: no sideways page scroll, and controls are at least 44px", async ({ page }) => {
  await signIn(page);
  await page.setViewportSize({ width: 390, height: 844 });
  for (const [name, path] of SCREENS()) {
    await open(page, path);
    const [scroll, inner] = await page.evaluate(() => [document.documentElement.scrollWidth, window.innerWidth]);
    expect(scroll, `${name}: page-level horizontal overflow`).toBeLessThanOrEqual(inner);
    const small = await page.evaluate((sel) => [...document.querySelectorAll(`header button, ${sel.split(", ").map((s) => `${s} button, ${s} summary, ${s} select`).join(", ")}`)]
      .filter((e) => e.getClientRects().length && !e.closest("[hidden], .sr-only"))
      .map((e) => { const r = e.getBoundingClientRect(); return { e: `${e.tagName}.${e.className} "${(e.textContent ?? "").trim().slice(0, 20)}"`, w: Math.round(r.width), h: Math.round(r.height) }; })
      .filter((x) => x.h < 44 || x.w < 44).slice(0, 10), CHANGED);
    expect(small, `${name}: touch targets under 44px`).toEqual([]);
  }
});

test("a keyboard-only pass reaches every changed control with a visible focus ring", async ({ page }) => {
  await signIn(page);
  await page.setViewportSize({ width: 1440, height: 900 });
  for (const [name, path] of SCREENS()) {
    await open(page, path);
    await page.locator("body").click({ position: { x: 1, y: 1 } });
    const unseen: string[] = [];
    const reached = new Set<string>();
    for (let i = 0; i < 70; i++) {
      await page.keyboard.press("Tab");
      const f = await page.evaluate(() => {
        const el = document.activeElement as HTMLElement | null;
        if (!el || el === document.body) return null;
        const cs = getComputedStyle(el);
        const ring = (cs.outlineStyle !== "none" && parseFloat(cs.outlineWidth) >= 2) || (cs.boxShadow !== "none" && cs.boxShadow !== "");
        return { id: `${el.tagName}.${el.className} "${(el.getAttribute("aria-label") ?? el.textContent ?? "").trim().slice(0, 24)}"`, ring };
      });
      if (!f) continue;
      reached.add(f.id);
      if (!f.ring) unseen.push(f.id);
    }
    expect([...new Set(unseen)], `${name}: focus with no visible indicator`).toEqual([]);
    expect(reached.size, `${name}: keyboard reached controls`).toBeGreaterThan(10);
  }
});
