/**
 * E2E tests — Frontend-Only Mode
 *
 * Validates the browser-only flow where data is loaded client-side via
 * DuckDB WASM. The Vite dev server serves the viewer without any
 * Python backend.
 *
 * NOTE: Parquet file loading in dev mode may fail because DuckDB WASM
 * requires signed extensions and the dev server serves them unsigned.
 * The tests account for this by checking the UI transition (upload ->
 * messages/settings) rather than asserting full data render.
 */

import { test, expect } from "@playwright/test";
import { type ChildProcess } from "child_process";
import {
  getTestDataPath,
  startDevServer,
  waitForServer,
  teardown,
  waitForCanvas,
} from "./helpers.js";
import { E2E_CONSTANTS } from "../playwright.config.js";

const BASE_URL = `http://localhost:${E2E_CONSTANTS.DEV_PORT}`;

let devServer: ChildProcess;
let testDataPath: string;

test.beforeAll(async () => {
  testDataPath = await getTestDataPath();
  devServer = startDevServer();
  await waitForServer(`${BASE_URL}/`);
});

test.afterAll(async () => {
  await teardown(devServer);
});

// ---------------------------------------------------------------------------
// File upload flow
// ---------------------------------------------------------------------------

test.describe("File Upload", () => {
  test("upload page shows drop zone and URL input", async ({ page }) => {
    await page.goto(`${BASE_URL}/#/file`);

    const dropZone = page.locator("text=Drag & drop");
    await expect(dropZone).toBeVisible({ timeout: 10_000 });

    // URL input should also be present
    await expect(page.locator('input[type="text"]')).toBeVisible();
  });

  test("uploading a parquet file transitions past the drop zone", async ({
    page,
  }) => {
    await page.goto(`${BASE_URL}/#/file`);
    await expect(page.locator("text=Drag & drop")).toBeVisible({ timeout: 10_000 });

    const fileInput = page.locator('input[type="file"]');
    await fileInput.setInputFiles(testDataPath);

    // After upload the drop zone should disappear — replaced by either
    // the settings view (column picker) or the messages view (loading / error).
    await expect(page.locator("text=Drag & drop")).not.toBeVisible({ timeout: 20_000 });
  });
});

// ---------------------------------------------------------------------------
// Synthetic test data (no file required)
// ---------------------------------------------------------------------------

test.describe("Test Data Viewer", () => {
  test("renders scatter plot with synthetic data", async ({ page }) => {
    await page.goto(`${BASE_URL}/#/test`);
    await waitForCanvas(page);

    expect(await page.locator("canvas").count()).toBeGreaterThan(0);
  });

  test("UI controls are present", async ({ page }) => {
    await page.goto(`${BASE_URL}/#/test`);
    await waitForCanvas(page);

    expect(await page.locator("button").count()).toBeGreaterThan(0);
  });
});
