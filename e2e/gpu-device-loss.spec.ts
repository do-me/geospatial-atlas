import { expect, test, type Page } from "@playwright/test";
import { deflateRawSync } from "node:zlib";

const BASE_URL = process.env.GPU_LOSS_URL;

type ProbeWindow = Window &
  typeof globalThis & {
    __atlasPerf?: { summary(): { count: number; pointCount: number; gpu: { count: number } } | null };
    __atlasGpuErrors?: { kind: string; message: string }[];
    __geospatialAtlasViewport?: { scale: number };
    __gpuLossTest: {
      devices: GPUDevice[];
      destroyed: number[];
      deniedAdapters: number;
      adapterRequests: number;
      simulateLoss: boolean;
      holdDevice: boolean;
      releaseDevice?: () => void;
      maxDrawInstances: number;
      pointRadius: number;
      matrixScale: number;
    };
  };

test.use({ deviceScaleFactor: 2 });
test.skip(!BASE_URL, "Set GPU_LOSS_URL to an existing GIS backend (large datasets recommended).");
test.setTimeout(300_000);

async function openView(page: Page, pointSize: number) {
  await page.addInitScript(() => {
    const w = window as ProbeWindow;
    const probe: ProbeWindow["__gpuLossTest"] = (w.__gpuLossTest = {
      devices: [],
      destroyed: [],
      deniedAdapters: 0,
      adapterRequests: 0,
      simulateLoss: false,
      holdDevice: false,
      maxDrawInstances: 0,
      pointRadius: 0,
      matrixScale: 0,
    });
    const requestAdapter = GPU.prototype.requestAdapter;
    GPU.prototype.requestAdapter = async function (options) {
      probe.adapterRequests++;
      if (probe.deniedAdapters > 0) {
        probe.deniedAdapters--;
        return null;
      }
      return requestAdapter.call(this, options);
    };
    const requestDevice = GPUAdapter.prototype.requestDevice;
    GPUAdapter.prototype.requestDevice = async function (descriptor) {
      const device = await requestDevice.call(this, descriptor);
      const index = probe.devices.length;
      probe.devices.push(device);
      probe.destroyed.push(0);
      const destroy = device.destroy.bind(device);
      device.destroy = () => {
        probe.destroyed[index]++;
        destroy();
      };
      Object.defineProperty(device, "lost", {
        value: device.lost.then((info) =>
          probe.simulateLoss ? { reason: "unknown", message: "Simulated GPU connection loss" } : info,
        ),
      });
      if (probe.holdDevice) {
        await new Promise<void>((resolve) => {
          w.__gpuLossTest.releaseDevice = resolve;
        });
      }
      return device;
    };
    const draw = GPURenderPassEncoder.prototype.draw;
    GPURenderPassEncoder.prototype.draw = function (vertices, instances = 1, firstVertex, firstInstance) {
      if (vertices === 4) probe.maxDrawInstances = Math.max(probe.maxDrawInstances, instances);
      return draw.call(this, vertices, instances, firstVertex, firstInstance);
    };
    const writeBuffer = GPUQueue.prototype.writeBuffer;
    GPUQueue.prototype.writeBuffer = function (buffer, offset, source, dataOffset, size) {
      if (buffer.size === 4288 && source instanceof ArrayBuffer && source.byteLength >= 224) {
        const floats = new Float32Array(source);
        if (floats[7] > 0) {
          probe.pointRadius = floats[7];
          probe.matrixScale = floats[16];
        }
      }
      return Reflect.apply(writeBuffer, this, [buffer, offset, source, dataOffset, size]);
    };
  });
  const response = await page.request.get(`${BASE_URL}/data/metadata.json`);
  expect(response.ok()).toBe(true);
  const metadata = await response.json();
  const projection = metadata.props.data.projection;
  expect(projection.isGis).toBe(true);
  const state = {
    charts: { "1": { type: "embedding", data: projection, pointSize } },
    layouts: { "1": { type: "list", name: "GPU regression", chartIds: ["1"] } },
    layoutOrder: ["1"],
    currentLayout: "1",
  };
  const encoded = deflateRawSync(Buffer.from(JSON.stringify(state))).toString("base64url");
  await page.goto(`${BASE_URL}/?perf=1#?state=${encoded}`, { waitUntil: "domcontentloaded" });
  await completedFrame(page, 0);
  if (projection.viewportHint?.rowCount != null) {
    expect(await page.evaluate(() => (window as ProbeWindow).__atlasPerf?.summary()?.pointCount)).toBe(
      projection.viewportHint.rowCount,
    );
  }
}

async function completedFrame(page: Page, after: number, world = true) {
  const handle = await page.waitForFunction(
    ({ after, world }) => {
      const w = window as ProbeWindow;
      const stats = w.__atlasPerf?.summary();
      const errors = w.__atlasGpuErrors ?? [];
      const scaleMatches = world ? w.__gpuLossTest.matrixScale < 0.01 : w.__gpuLossTest.matrixScale > 1;
      return errors.length ||
        (stats && stats.pointCount > 0 && stats.count > after && stats.gpu.count >= stats.count && scaleMatches)
        ? { errors, stats }
        : false;
    },
    { after, world },
    { timeout: 150_000 },
  );
  const result = await handle.jsonValue();
  expect(result).toBeTruthy();
  if (!result) throw new Error("No completed GPU frame");
  expect(result.errors).toEqual([]);
  expect(result.stats?.gpu.count).toBeGreaterThan(0);
  return result.stats!.count;
}

test("large Retina points survive abrupt high-zoom to world transitions without losing the GPU", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await openView(page, 7.15);
  for (const delay of [650, 0]) {
    const box = await page.locator("canvas").first().boundingBox();
    expect(box).not.toBeNull();
    await page.mouse.move(box!.x + box!.width / 2, box!.y + box!.height / 2);
    const before = await page.evaluate(() => (window as ProbeWindow).__atlasPerf!.summary()!.count);
    await page.mouse.wheel(0, -2800);
    await page.waitForTimeout(delay);
    if (delay) await completedFrame(page, before, false);
    const highZoom = await page.evaluate(() => (window as ProbeWindow).__atlasPerf!.summary()!.count);
    await page.mouse.wheel(0, 5000);
    await page.waitForTimeout(750);
    await completedFrame(page, highZoom);
  }
  const probe = await page.evaluate(() => {
    const p = (window as ProbeWindow).__gpuLossTest;
    return { maxInstances: p.maxDrawInstances, radius: p.pointRadius, devices: p.devices.length };
  });
  expect(probe.radius).toBeCloseTo(14.3, 4);
  expect(probe.maxInstances).toBeGreaterThan(0);
  expect(probe.maxInstances * probe.radius ** 2).toBeLessThanOrEqual(16_000_001);
  expect(probe.devices).toBe(1);
  expect(errors).toEqual([]);
});

test("lost devices recover through temporary adapter failures and cannot resurrect an unmounted view", async ({
  page,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await openView(page, 2);
  const before = await page.evaluate(() => {
    const w = window as ProbeWindow;
    const gpuFrames = w.__atlasPerf!.summary()!.gpu.count;
    w.__gpuLossTest.simulateLoss = true;
    w.__gpuLossTest.deniedAdapters = 2;
    w.__gpuLossTest.devices[0].destroy();
    return gpuFrames;
  });
  await page.waitForFunction(
    (previous) => {
      const w = window as ProbeWindow;
      return w.__gpuLossTest.devices.length === 2 && (w.__atlasPerf?.summary()?.gpu.count ?? 0) > previous;
    },
    before,
    { timeout: 150_000 },
  );
  expect(await page.evaluate(() => (window as ProbeWindow).__gpuLossTest.adapterRequests)).toBeGreaterThanOrEqual(4);
  expect(await page.evaluate(() => (window as ProbeWindow).__atlasGpuErrors)).toEqual([
    expect.objectContaining({ kind: "lost", message: "Simulated GPU connection loss" }),
  ]);
  await page.evaluate(() => {
    const probe = (window as ProbeWindow).__gpuLossTest;
    probe.holdDevice = true;
    probe.devices[1].destroy();
  });
  await page.waitForFunction(() => !!(window as ProbeWindow).__gpuLossTest.releaseDevice);
  const remove = await page.request.post(`${BASE_URL}/mcp_legacy`, {
    data: {
      jsonrpc: "2.0",
      id: 1,
      method: "tools/call",
      params: { name: "chart_remove", arguments: { layoutId: "1", chartId: "1" } },
    },
  });
  const removed = await remove.json();
  expect(removed.error).toBeUndefined();
  expect(removed.result.isError).not.toBe(true);
  await expect(page.locator("canvas")).toHaveCount(0);
  await page.evaluate(() => (window as ProbeWindow).__gpuLossTest.releaseDevice!());
  await page.waitForFunction(() => (window as ProbeWindow).__gpuLossTest.destroyed[2] === 1);
  await page.waitForTimeout(500);
  expect(await page.evaluate(() => (window as ProbeWindow).__gpuLossTest.devices.length)).toBe(3);
  expect(errors).toEqual([]);
});
