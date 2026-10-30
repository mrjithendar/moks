/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// MOKS is served under /moks by the FastAPI backend (no prefix stripping anywhere).
// MOKS_BASE_PATH must match the backend's MOKS_BASE_PATH.
const base = (process.env.MOKS_BASE_PATH ?? "/moks").replace(/\/?$/, "/");

export default defineConfig({
  base,
  plugins: [react()],
  build: { outDir: "dist", sourcemap: false, assetsDir: "assets" },
  server: {
    port: 5173,
    // `npm run dev` proxies the API to a local backend (moks serve on :8080).
    proxy: { [`${base}api`]: { target: "http://localhost:8080", changeOrigin: false } },
  },
  test: { environment: "jsdom", globals: false, env: { BASE_URL: base } },
});
