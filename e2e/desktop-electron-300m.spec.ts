/**
 * Desktop electron-build smoke + fluency probe.
 *
 * Launches the actual Electron shell (in dev mode, pointed at a vite UI on
 * 127.0.0.1:1420 and the freshly-built PyInstaller sidecar) with the 300M
 * synthetic parquet pre-set via GEOSPATIAL_ATLAS_INITIAL_DATASET. Validates:
 *
 *   - Electron starts, the splash window loads
 *   - The sidecar is spawned and reports ready (window navigates from
 *     bootstrap URL to the sidecar's http://127.0.0.1:<port>)
 *   - The atlas WebGPU canvas appears at non-zero size
 *   - 10-second simulated pan triggers many CSS-pan applies and
 *     ≤ a handful of GPU re-renders (validates the recompute fix
 *     in the *packaged* viewer bundle)
 *
 * Pre-reqs (script driver handles these):
 *   - apps/desktop/python-sidecar/build.sh has produced
 *     apps/desktop/resources/sidecar/geospatial-atlas-sidecar
 *   - npm run build:ui && npm run build:electron under apps/desktop/
 *   - vite dev server is up on http://127.0.0.1:1420
 *
 * Run::
 *
 *   DATASET=/tmp/gsa_bench/europe_300m.parquet \
 *     npx playwright test e2e/desktop-electron-300m.spec.ts \
 *     --project=desktop-electron --workers=1
 */

import { test, expect, _electron as electron, type Page } from "@playwright/test";
import { createRequire } from "node:module";
import { existsSync } from "node:fs";
import { join } from "node:path";

const REPO = join(__dirname, "..");
const DESKTOP = join(REPO, "apps/desktop");
const DATASET = process.env.DATASET ?? "/tmp/gsa_bench/europe_300m.parquet";

// Resolve Electron from apps/desktop/node_modules — Playwright's bundled
// look-up only checks the root and finds nothing here.
const ELECTRON_BIN: string = (() => {
  const req = createRequire(join(DESKTOP, "package.json"));
  return req("electron") as unknown as string;
})();

test("desktop electron 300m fluency", async () => {
  test.setTimeout(420_000);
  expect(existsSync(DATASET), `dataset missing at ${DATASET}`).toBe(true);
  expect(
    existsSync(join(DESKTOP, "resources/sidecar/geospatial-atlas-sidecar")),
    "PyInstaller sidecar binary not built",
  ).toBe(true);

  const app = await electron.launch({
    cwd: DESKTOP,
    args: ["."],
    executablePath: ELECTRON_BIN,
    env: {
      ...process.env,
      GEOSPATIAL_ATLAS_INITIAL_DATASET: DATASET,
    },
  });
  try {
    const window = await app.firstWindow();
    // Bootstrap URL is the vite UI shell; once the user-supplied dataset
    // load completes the renderer navigates to http://127.0.0.1:<sidecarPort>.
    // Wait for that navigation by polling URL.
    const sidecarReadyDeadline = Date.now() + 240_000;
    let onSidecar = false;
    while (Date.now() < sidecarReadyDeadline) {
      await window.waitForTimeout(2_000);
      const url = window.url();
      if (/127\.0\.0\.1:\d+/.test(url) && !url.includes(":1420")) {
        onSidecar = true;
        break;
      }
    }
    expect(onSidecar, `window never navigated to sidecar URL (still ${window.url()})`).toBe(true);
    console.log(`window URL: ${window.url()}`);

    await window.addInitScript(() => {
      (window as any).__atlasPerfEnabled = true;
    });
    await window.reload();
    await window.bringToFront();

    // A canvas can exist before its coordinates upload or GPU work completes.
    await window.waitForFunction(
      () => {
        const w = window as any;
        const summary = w.__atlasPerf?.summary?.();
        return (
          w.__atlasFirstBigRenderGpuLogged === true &&
          summary?.pointCount > 0 &&
          summary?.count > 0 &&
          summary.gpu?.count >= summary.count
        );
      },
      null,
      { timeout: 120_000 },
    );

    // Reset pan dbg counters.
    await window.evaluate(() => {
      (window as any).__atlasPanDbg = undefined;
    });

    // Find the map region and pan inside it.
    const box = await (await window.locator("canvas").first()).boundingBox();
    if (!box) throw new Error("no canvas bbox");
    const cx = box.x + box.width / 2;
    const cy = box.y + box.height / 2;
    await window.mouse.move(cx, cy);
    await window.mouse.down();
    await window.mouse.move(cx + 12, cy + 12);
    await window.waitForTimeout(250);
    const held = await window.evaluate(() => (window as any).__atlasPanDbg ?? null);
    expect(held, "pan dbg never registered for the held drag").not.toBeNull();
    expect(held.renderCalls, "a held drag must not redraw after the input idle timeout").toBe(0);
    const t0 = Date.now();
    let dx = 0;
    while (Date.now() - t0 < 10_000) {
      dx = ((dx + 17) % 200) - 100;
      const dy = (dx * 0.3) | 0;
      await window.mouse.move(cx + dx, cy + dy);
      await window.waitForTimeout(50);
    }
    await window.mouse.up();
    await window.waitForTimeout(2_000);

    const dbg = await window.evaluate(() => (window as any).__atlasPanDbg ?? null);
    console.log(`pan dbg: ${JSON.stringify(dbg)}`);
    expect(dbg, "pan dbg never registered").not.toBeNull();
    expect(dbg.cssPanApplied, "CSS-pan never triggered").toBeGreaterThan(20);
    expect(dbg.renderCalls, `too many GPU re-renders (${dbg.renderCalls})`).toBeLessThan(
      Math.max(5, dbg.cssPanApplied / 5),
    );

    await window.screenshot({ path: "e2e/test-results/desktop-300m-after-pan.png" });
  } finally {
    await app.close();
  }
});
