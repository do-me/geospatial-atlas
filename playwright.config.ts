import { defineConfig } from "@playwright/test";
import path from "path";

const BACKEND_DIR = path.resolve(__dirname, "packages/backend");
const STATIC_DIR = path.resolve(__dirname, "packages/viewer/dist");

/**
 * E2E test configuration for Geospatial Atlas.
 *
 * Two test projects cover the two runtime modes:
 *   - "server-mode"   : Python backend serves data + pre-built frontend
 *   - "frontend-mode"  : Vite dev server only (DuckDB WASM in-browser)
 *
 * Run all:    npx playwright test
 * Run one:    npx playwright test --project server-mode
 * See report: npx playwright show-report
 */
export default defineConfig({
  testDir: "./e2e",
  outputDir: "./e2e/test-results",
  timeout: 120_000,
  expect: { timeout: 30_000 },
  fullyParallel: false,
  retries: 0,
  reporter: [["list"], ["html", { outputFolder: "e2e/playwright-report", open: "never" }]],
  use: {
    headless: true,
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
    viewport: { width: 1280, height: 900 },
  },
  projects: [
    {
      name: "server-mode",
      testMatch: "server-mode.spec.ts",
      use: { browserName: "chromium" },
    },
    {
      name: "frontend-mode",
      testMatch: "frontend-mode.spec.ts",
      use: { browserName: "chromium" },
    },
  ],
});

/** Shared constants re-exported for test files. */
export const E2E_CONSTANTS = {
  BACKEND_DIR,
  STATIC_DIR,
  SERVER_PORT: 5088,
  DEV_PORT: 5174,
  /** Max ms to wait for a server to become reachable. */
  SERVER_STARTUP_TIMEOUT: 60_000,
  /** Polling interval while waiting for a server. */
  POLL_INTERVAL: 500,
} as const;
