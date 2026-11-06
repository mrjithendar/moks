import { test } from "@playwright/test";
import { seed, signIn } from "./seed";

// Screenshots for docs/redesign/{before,after}: `MOKS_CAPTURE=before npx playwright test capture`.
// Skipped in ordinary runs. Fixture data, not mk4.
const which = process.env.MOKS_CAPTURE;
const VIEWPORTS = [{ name: "390", width: 390, height: 844 }, { name: "768", width: 768, height: 1024 }, { name: "1440", width: 1440, height: 900 }];

test.skip(!which, "set MOKS_CAPTURE=before|after to capture screenshots");

test("capture screens", async ({ page, baseURL }) => {
  test.setTimeout(240_000);
  const { conversations } = await seed(baseURL!);
  const conv = conversations.find((c: any) => c.title.includes("crashlooping")) ?? conversations[0];
  const screens: [string, string][] = [
    ["overview", "/moks/"],
    ["investigations-list", "/moks/investigations"],
    ["investigation-detail", `/moks/conversations/${conv.id}`],
    ["kubernetes-explorer", "/moks/explorer?env=mk4&ns=shop"],
    ["incidents", "/moks/incidents"],
    ["metrics", "/moks/metrics"],
    ["logs", "/moks/logs"],
    ["rightsizing", "/moks/finops"],
  ];
  await signIn(page);
  for (const vp of VIEWPORTS) {
    await page.setViewportSize({ width: vp.width, height: vp.height });
    for (const [name, path] of screens) {
      await page.goto(path);
      await page.waitForLoadState("networkidle").catch(() => {});
      await page.waitForTimeout(800);
      if (name === "rightsizing") await page.getByRole("heading", { name: "Rightsizing" }).scrollIntoViewIfNeeded().catch(() => {});
      await page.screenshot({ path: `../docs/redesign/${which}/${name}-${vp.name}.png`, fullPage: name !== "rightsizing" });
    }
  }
});
