import { expect, type Page, test } from "@playwright/test";
import { readFileSync } from "node:fs";
import { LONG_POD, seed, signIn, VIEWER } from "./seed";

// Milestone 1 flows, in Chrome, under /moks, against the real API with FIXTURE connectors (not mk4).
let crash: any, followUp: any;

test.beforeAll(async ({ baseURL }) => {
  test.setTimeout(180_000);
  const { conversations } = await seed(baseURL!);
  crash = conversations.find((c: any) => c.title.includes("crashlooping"));
  followUp = conversations.find((c: any) => c.title.includes("cart-redis")); // its own conversation: the follow-up test changes it
});

const phone = { width: 390, height: 844 };
const desktop = { width: 1440, height: 900 };

async function noSidewaysScroll(page: Page) {
  const [scroll, inner] = await page.evaluate(() => [document.documentElement.scrollWidth, window.innerWidth]);
  expect(scroll, "page-level horizontal overflow").toBeLessThanOrEqual(inner);
}

test("a deep link opens the conversation with question, answer, evidence, then history", async ({ page }) => {
  await page.setViewportSize(desktop);
  await signIn(page);
  await page.goto(`/moks/conversations/${crash.id}`);
  const headings = page.locator(".ws-messages .rp-section > h2, .ws-messages .rp-history > summary");
  await expect(headings.first()).toHaveText("Question");
  expect((await headings.allTextContents()).map((t) => t.replace(/\s*\(\d+\)$/, ""))).toEqual(["Question", "Answer", "Evidence", "Conversation history"]);
  await expect(page.locator(".rp-question")).toContainText(LONG_POD);
  await expect(page.getByTestId("scope-indicator")).toContainText("mk4 · shop");
  await expect(page.locator(".claim-observed").first()).toBeVisible();
  await expect(page.locator(".claim-missing").first()).toBeVisible();
});

test("an evidence citation opens its exact source", async ({ page }) => {
  await page.setViewportSize(desktop);
  await signIn(page);
  await page.goto(`/moks/conversations/${crash.id}`);
  // The log lines MOKS read: the exact pod, container and previous-container flag, and its time range
  await page.locator(".rp-ev button", { hasText: "Pod log lines" }).first().click();
  const panel = page.getByRole("complementary", { name: "Evidence" });
  const exact = panel.getByRole("region", { name: "Exact source" });
  await expect(exact).toContainText("Pod log lines");
  await expect(exact.getByTestId("exact-source-detail")).toContainText(LONG_POD);
  await expect(exact).toContainText("Time range:");
  await expect(panel).toContainText("ledger write failed"); // the line itself
  await exact.getByRole("link", { name: "Open the pod in the explorer" }).click();
  await expect(page).toHaveURL(new RegExp(`/moks/explorer\\?tab=pods&env=mk4&ns=shop&q=${LONG_POD}`));
  await expect(page.getByTestId("rt-row")).toHaveCount(1);
  // A citation in the report says what it points at before it is opened
  await page.goBack();
  await expect(page.locator(".cite-kind").first()).toBeVisible();
});

test("a follow-up draft survives navigation and reload, then sends", async ({ page }) => {
  await page.setViewportSize(phone);
  await signIn(page);
  await page.goto(`/moks/conversations/${followUp.id}`);
  const box = page.getByRole("textbox", { name: "Message" });
  await box.fill("Did the ledger writer restart after the database came back?");
  await page.goto("/moks/explorer");
  await page.goBack();
  await expect(box).toHaveValue("Did the ledger writer restart after the database came back?");
  await page.reload();
  await expect(box).toHaveValue("Did the ledger writer restart after the database came back?");
  const keyboard = await box.boundingBox();
  expect(keyboard!.y + keyboard!.height).toBeLessThanOrEqual(phone.height); // the composer is on screen
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.locator(".rp-question")).toHaveText("Did the ledger writer restart after the database came back?");
  await expect(box).toHaveValue("");
});

test("Back restores the inbox filters, selection and scroll position", async ({ page }) => {
  await page.setViewportSize(phone);
  await signIn(page);
  await page.goto("/moks/investigations?env=mk4&state=completed");
  const list = page.locator(".ws-list-items");
  await expect(list.locator(".inbox-row").nth(10)).toBeVisible();
  await list.evaluate((el) => { el.scrollTop = 600; });
  const row = list.locator(".inbox-row").nth(12);
  const id = await row.getAttribute("data-id");
  await row.scrollIntoViewIfNeeded(); // where the list is when the row is tapped
  const before = await list.evaluate((el) => el.scrollTop);
  expect(before).toBeGreaterThan(300);
  await row.locator(".inbox-main").click();
  await expect(page).toHaveURL(new RegExp(`/moks/conversations/${id}\\?env=mk4&state=completed`));
  await page.goBack();
  await expect(page).toHaveURL(/\/moks\/investigations\?env=mk4&state=completed$/);
  await expect(list.locator(`[data-id="${id}"]`)).toHaveClass(/recent/);
  expect(Math.abs(await list.evaluate((el) => el.scrollTop) - before)).toBeLessThan(5);
  await expect(page.getByLabel("Active filters")).toContainText("mk4");
});

test("the mobile Menu traps focus, closes on Escape, backdrop and navigation, and returns focus", async ({ page }) => {
  await page.setViewportSize(phone);
  await signIn(page);
  await page.goto("/moks/");
  const menu = page.getByRole("button", { name: "Menu", exact: true });
  await menu.click();
  const drawer = page.getByRole("dialog", { name: "Navigation" });
  await expect(drawer).toBeVisible();
  expect(await drawer.evaluate((d) => d.contains(document.activeElement))).toBe(true);
  for (let i = 0; i < 40; i++) await page.keyboard.press("Tab");
  expect(await drawer.evaluate((d) => d.contains(document.activeElement))).toBe(true);
  await page.keyboard.press("Escape");
  await expect(drawer).toBeHidden();
  await expect(menu).toBeFocused();
  await menu.click();
  await page.mouse.click(380, 400); // the backdrop, right of the drawer
  await expect(drawer).toBeHidden();
  await menu.click();
  await drawer.getByRole("link", { name: "Kubernetes explorer" }).click();
  await expect(page).toHaveURL(/\/moks\/explorer/);
  await expect(drawer).toBeHidden();
  await expect(menu).toBeFocused();
  await noSidewaysScroll(page);
});

test("Kubernetes filters and scope come back from the URL, long names stay on one line", async ({ page }) => {
  await page.setViewportSize(desktop);
  await signIn(page);
  await page.goto("/moks/explorer?env=mk4&ns=shop&q=payments&only=1");
  const chips = page.getByLabel("Active filters");
  for (const c of ["mk4", "namespace shop", "matches payments", "problems only"]) await expect(chips).toContainText(c);
  await expect(page.getByRole("button", { name: "Clear all" })).toBeVisible();
  await expect(page.getByTestId("scope-indicator")).toContainText("mk4 · shop");
  await expect(page.locator("[data-state=partial]")).toContainText("showing"); // shop is bigger than the result bound
  const rows = page.getByTestId("rt-row");
  await expect(rows).toHaveCount(1);
  await expect(rows.first()).toContainText("CrashLoopBackOff");
  const name = rows.first().locator(".ident .trunc");
  const box = await name.boundingBox();
  expect(box!.height).toBeLessThan(28); // one line, never a character at a time
  await rows.first().getByRole("button", { name: /Show full pod name/ }).click();
  await expect(page.getByRole("dialog", { name: "Full pod name" })).toContainText(LONG_POD);
  // The scope carries to other scoped pages
  await page.getByRole("link", { name: "Investigations" }).click();
  await expect(page).toHaveURL(/\/moks\/investigations\?env=mk4&ns=shop$/);
  // 200+ rows across namespaces, still one line each, no sideways scroll on a phone
  await page.setViewportSize(phone);
  await page.goto("/moks/explorer?env=mk4");
  await expect(page.getByTestId("rt-row").nth(199)).toBeAttached();
  await noSidewaysScroll(page);
  await page.getByTestId("rt-row").first().locator("summary").click();
  await expect(page.getByTestId("rt-row").first().locator("dl")).toContainText("Namespace");
});

test("a viewer sees no Admin, and a direct Admin URL explains the denial", async ({ page }) => {
  await page.setViewportSize(desktop);
  await signIn(page, VIEWER);
  await page.goto("/moks/");
  await expect(page.getByRole("group", { name: "Investigate" })).toBeVisible();
  await expect(page.getByRole("group", { name: "Admin" })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "Users & roles" })).toHaveCount(0);
  await page.goto("/moks/admin/users");
  await expect(page.getByRole("alert")).toContainText("You don't have access to this page");
  const api = await page.request.get("/moks/api/admin/users");
  expect(api.status()).toBe(403); // the server refuses it too; hiding it is only a convenience
});

test("rightsizing for kindnet-cni: four fields, separate CPU and memory, a reviewable proposal, matching CSV", async ({ page }) => {
  await page.setViewportSize(desktop);
  await signIn(page);
  await page.goto("/moks/finops?env=mk4&ns=kube-system");
  const item = page.getByTestId("rs-item").filter({ hasText: "kindnet-cni" });
  await expect(item).toBeVisible({ timeout: 30_000 });
  const field = (label: string) => item.locator(".rs-field", { hasText: label });
  await expect(field("CPU request")).toContainText(/100m\s*→.*10m/);
  await expect(field("CPU limit")).toContainText("100m");
  await expect(field("CPU limit")).not.toHaveClass(/rs-changed/);
  await expect(field("Memory request")).toContainText(/50Mi\s*→.*64Mi/);
  await expect(field("Memory limit")).toContainText(/50Mi\s*→.*96Mi/);
  await expect(field("Memory limit")).toContainText("OOM risk");
  await expect(item.locator(".rs-rec")).toHaveCount(2);
  await expect(item).toContainText("Memory: Increase");
  await expect(item).toContainText("CPU: Reduce");
  await expect(item).not.toContainText("$"); // fixed price: capacity, never money

  await item.getByRole("button", { name: /Review the Memory proposal/ }).click();
  const drawer = page.getByRole("dialog", { name: /Proposal preview: Memory/ });
  await expect(drawer.getByTestId("rs-diff")).toHaveText([
    "- containers[kindnet-cni].resources.requests.memory: 50Mi", "+ containers[kindnet-cni].resources.requests.memory: 64Mi",
    "- containers[kindnet-cni].resources.limits.memory: 50Mi", "+ containers[kindnet-cni].resources.limits.memory: 96Mi"].join("\n"));
  await expect(drawer).toContainText(/reserves 70 MiB memory across 5 replicas/i);
  await expect(drawer).toContainText("Changing a DaemonSet's resources");
  await expect(drawer).toContainText("Not proposed");
  const propose = drawer.getByRole("button", { name: "Propose" });
  await expect(propose).toBeDisabled();
  await expect(drawer.getByRole("note")).toContainText("Deployments only. DaemonSet kindnet");
  await page.keyboard.press("Escape");

  const [download] = await Promise.all([page.waitForEvent("download"), page.getByRole("button", { name: "Export CSV" }).last().click()]);
  const csv = readFileSync(await download.path(), "utf8").trim().split("\r\n").map((l) => l.slice(1, -1).split('","'));
  const head = csv[0];
  const rows = csv.filter((r) => r[head.indexOf("Container")] === "kindnet-cni");
  const col = (r: string[], n: string) => r[head.indexOf(n)];
  const mem = rows.find((r) => col(r, "Resource") === "Memory")!, cpu = rows.find((r) => col(r, "Resource") === "CPU")!;
  expect([col(mem, "Memory request now"), col(mem, "Memory request recommended"), col(mem, "Memory limit now"), col(mem, "Memory limit recommended")]).toEqual(["50Mi", "64Mi", "50Mi", "96Mi"]);
  expect([col(cpu, "CPU request now"), col(cpu, "CPU request recommended"), col(cpu, "CPU limit recommended")]).toEqual(["100m", "10m", "100m"]);
  expect(col(cpu, "Estimated saving per month")).toBe("");
});
