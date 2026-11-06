import { defineConfig } from "@playwright/test";

// Browser tests run the built portal under /moks against the real API and worker with FIXTURE connectors
// (e2e/fixture_server.py), never against mk4. Needs `npm run build` first, a Python with the backend's
// requirements (MOKS_PYTHON, default python3) and PostgreSQL (MOKS_E2E_PG). Chrome is the installed one.
const port = Number(process.env.MOKS_E2E_PORT ?? 8099);
const external = !!process.env.MOKS_E2E_URL; // point at an already running server instead of starting one

export default defineConfig({
  testDir: "e2e",
  timeout: 60_000,
  workers: 1, // one server and one database: tests share seeded data
  reporter: [["list"]],
  use: {
    baseURL: process.env.MOKS_E2E_URL ?? `http://127.0.0.1:${port}`,
    channel: "chrome",
    trace: "retain-on-failure",
  },
  webServer: external ? undefined : {
    command: `${process.env.MOKS_PYTHON ?? "python3"} -I e2e/fixture_server.py`,
    env: { PORT: String(port) },
    url: `http://127.0.0.1:${port}/moks/api/health`,
    timeout: 120_000,
    reuseExistingServer: !process.env.CI,
  },
});
