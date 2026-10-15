// Copyright (c) 2025 Apple Inc. Licensed under MIT License.

import { afterEach, describe, expect, it, vi } from "vitest";

import { pointDrawChunkSize } from "../../component/src/lib/webgpu_renderer/draw_points.js";
import { runUnpack } from "../../component/src/lib/webgpu_renderer/unpack.js";
import { record, summary } from "../../component/src/lib/embedding_view/perf_recorder.js";
import {
  destroyGPUResource,
  gpuBuffer,
  gpuTexture,
  waitForGPUQueue,
} from "../../component/src/lib/webgpu_renderer/utils.js";

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}

function mockDevice() {
  const drain = deferred<void>();
  const loss = deferred<GPUDeviceLostInfo>();
  const onSubmittedWorkDone = vi.fn(() => drain.promise);
  const createBuffer = vi.fn<GPUDevice["createBuffer"]>();
  const createTexture = vi.fn<GPUDevice["createTexture"]>();
  const device = {
    queue: { onSubmittedWorkDone },
    lost: loss.promise,
    createBuffer,
    createTexture,
  } as GPUDevice;
  return { device, drain, loss, onSubmittedWorkDone, createBuffer, createTexture };
}

function lostInfo(): GPUDeviceLostInfo {
  return { reason: "unknown", message: "A valid external Instance reference no longer exists." };
}

describe("point draw batches", () => {
  it.each([0, 0.3, 2, 4])("preserves the million-point budget at %spx", (radius) => {
    expect(pointDrawChunkSize(radius)).toBe(1_000_000);
  });

  it.each([7.15, 14.3, 32, 128])("bounds fragment area at %spx without reducing total points", (radius) => {
    const chunkSize = pointDrawChunkSize(radius);
    expect(chunkSize).toBeGreaterThan(0);
    expect(chunkSize * radius ** 2).toBeLessThanOrEqual(16_000_000);
    const count = 81_455_423;
    let drawn = 0;
    for (let first = 0; first < count; first += chunkSize) {
      drawn += Math.min(chunkSize, count - first);
    }
    expect(drawn).toBe(count);
  });

  it("scales batches quadratically with device-pixel point size", () => {
    expect(pointDrawChunkSize(8)).toBe(250_000);
    expect(pointDrawChunkSize(16)).toBe(62_500);
  });

  it("never produces an empty chunk and reports invalid sizes", () => {
    expect(pointDrawChunkSize(100_000)).toBe(1);
    expect(() => pointDrawChunkSize(NaN)).toThrow("GPU point size must be finite");
    expect(() => pointDrawChunkSize(Infinity)).toThrow("GPU point size must be finite");
  });
});

describe("GPU queue lifetime", () => {
  it("waits for successful GPU completion", async () => {
    const { device, drain } = mockDevice();
    const done = waitForGPUQueue(device);
    drain.resolve();
    await expect(done).resolves.toBeUndefined();
  });

  it("rejects on device loss even when the queue never settles", async () => {
    const { device, loss } = mockDevice();
    const done = waitForGPUQueue(device);
    loss.resolve(lostInfo());
    await expect(done).rejects.toThrow("A valid external Instance reference no longer exists.");
  });

  it("rejects immediately on an already lost device without touching its queue", async () => {
    const { device, loss, onSubmittedWorkDone } = mockDevice();
    const done = waitForGPUQueue(device);
    loss.resolve(lostInfo());
    await expect(done).rejects.toThrow("WebGPU device was lost");
    onSubmittedWorkDone.mockClear();
    await expect(waitForGPUQueue(device)).rejects.toThrow("WebGPU device was lost");
    expect(onSubmittedWorkDone).not.toHaveBeenCalled();
  });

  it.each(["synchronous", "asynchronous"])("surfaces %s queue failures", async (kind) => {
    const { device, drain, onSubmittedWorkDone } = mockDevice();
    const failure = new DOMException("Invalid GPU instance", "OperationError");
    if (kind === "synchronous") {
      onSubmittedWorkDone.mockImplementation(() => {
        throw failure;
      });
    }
    const done = waitForGPUQueue(device);
    if (kind === "asynchronous") drain.reject(failure);
    await expect(done).rejects.toBe(failure);
  });
});

describe("GPU resource destruction", () => {
  it("defers replacement and final destruction until queued draws complete", async () => {
    const { device, drain, createBuffer } = mockDevice();
    const first = { destroy: vi.fn() } as GPUBuffer;
    const second = { destroy: vi.fn() } as GPUBuffer;
    createBuffer.mockReturnValueOnce(first).mockReturnValueOnce(second);
    const state: Parameters<typeof gpuBuffer>[0] = {};
    expect(gpuBuffer(state, device, 3, 128)).toBe(first);
    expect(createBuffer).toHaveBeenCalledWith({ size: 4, usage: 128 });
    expect(gpuBuffer(state, device, 8, 128)).toBe(second);
    state.destroy!();
    state.destroy!();
    expect(first.destroy).not.toHaveBeenCalled();
    expect(second.destroy).not.toHaveBeenCalled();
    drain.resolve();
    await vi.waitFor(() => {
      expect(first.destroy).toHaveBeenCalledTimes(1);
      expect(second.destroy).toHaveBeenCalledTimes(1);
    });
  });

  describe("GPU failure propagation", () => {
    it("does not count failed GPU frames as completed timing samples", async () => {
      vi.stubGlobal("window", { __atlasPerfEnabled: true });
      const failure = new Error("Device lost");
      const timing = deferred<number>();
      const warning = vi.spyOn(console, "warn").mockImplementation(() => {});
      record({ cpuMs: 2, downsampled: false, whenGpuDone: timing.promise });
      timing.reject(failure);
      await vi.waitFor(() => {
        expect(warning).toHaveBeenCalledWith("[atlas-perf] GPU frame did not complete:", failure);
      });
      expect(summary()?.count).toBe(1);
      expect(summary()?.gpu.count).toBe(0);
    });

    it.each(["completed", "lost"])("releases unpack staging safely when the GPU is %s", async (outcome) => {
      const { device, createBuffer, drain, loss } = mockDevice();
      vi.stubGlobal("GPUBufferUsage", { STORAGE: 128, UNIFORM: 64, COPY_DST: 8 });
      const buffers: GPUBuffer[] = [];
      createBuffer.mockImplementation((descriptor) => {
        const buffer = {
          getMappedRange: () => new ArrayBuffer(descriptor.size),
          unmap: vi.fn(),
          destroy: vi.fn(),
        } as GPUBuffer;
        buffers.push(buffer);
        return buffer;
      });
      device.createBindGroup = vi.fn().mockReturnValue({});
      device.createCommandEncoder = vi.fn().mockReturnValue({
        beginComputePass: () => ({
          setPipeline: vi.fn(),
          setBindGroup: vi.fn(),
          dispatchWorkgroups: vi.fn(),
          end: vi.fn(),
        }),
        finish: vi.fn(),
      });
      device.queue.submit = vi.fn();
      const unpack = runUnpack(
        device,
        {} as GPUComputePipeline,
        {} as GPUBindGroupLayout,
        new Uint32Array([0, 4_294_967_295]),
        {} as GPUBuffer,
        { min: 0, max: 1 },
      );
      if (outcome === "completed") {
        drain.resolve();
        await expect(unpack).resolves.toBeUndefined();
      } else {
        loss.resolve(lostInfo());
        await expect(unpack).rejects.toThrow("WebGPU device was lost");
      }
      expect(buffers).toHaveLength(2);
      for (const buffer of buffers) {
        expect(buffer.destroy).toHaveBeenCalledTimes(outcome === "completed" ? 1 : 0);
      }
    });
  });

  it("defers texture replacements and makes final destruction idempotent", async () => {
    const { device, drain, createTexture } = mockDevice();
    const first = { destroy: vi.fn() } as GPUTexture;
    const second = { destroy: vi.fn() } as GPUTexture;
    createTexture.mockReturnValueOnce(first).mockReturnValueOnce(second);
    const state: Parameters<typeof gpuTexture>[0] = {};
    gpuTexture(state, device, 100, 100, "rgba16float", 16);
    gpuTexture(state, device, 200, 100, "rgba16float", 16);
    state.destroy!();
    state.destroy!();
    expect(first.destroy).not.toHaveBeenCalled();
    expect(second.destroy).not.toHaveBeenCalled();
    drain.resolve();
    await vi.waitFor(() => {
      expect(first.destroy).toHaveBeenCalledTimes(1);
      expect(second.destroy).toHaveBeenCalledTimes(1);
    });
  });

  it("does not call native destroy after device loss, even if the queue hangs", async () => {
    const { device, loss } = mockDevice();
    const resource = { destroy: vi.fn() } as GPUBuffer;
    const warning = vi.spyOn(console, "warn").mockImplementation(() => {});
    const cleanup = destroyGPUResource(device, resource);
    loss.resolve(lostInfo());
    await cleanup;
    await destroyGPUResource(device, resource);
    expect(resource.destroy).not.toHaveBeenCalled();
    expect(warning).not.toHaveBeenCalled();
  });

  it("reports unexpected queue failures without leaking a rejected cleanup promise", async () => {
    const { device, onSubmittedWorkDone } = mockDevice();
    const failure = new Error("Queue unavailable");
    onSubmittedWorkDone.mockImplementation(() => {
      throw failure;
    });
    const resource = { destroy: vi.fn() } as GPUBuffer;
    const warning = vi.spyOn(console, "warn").mockImplementation(() => {});
    await expect(destroyGPUResource(device, resource)).resolves.toBeUndefined();
    expect(warning).toHaveBeenCalledWith("[atlas-gpu] Resource cleanup failed:", failure);
  });

  it("reports native destruction failures instead of creating unhandled rejections", async () => {
    const { device, drain } = mockDevice();
    const failure = new DOMException("Invalid external instance", "OperationError");
    const resource = {
      destroy: vi.fn(() => {
        throw failure;
      }),
    } as GPUTexture;
    const warning = vi.spyOn(console, "warn").mockImplementation(() => {});
    const cleanup = destroyGPUResource(device, resource);
    drain.resolve();
    await expect(cleanup).resolves.toBeUndefined();
    expect(warning).toHaveBeenCalledWith("[atlas-gpu] Resource cleanup failed:", failure);
  });
});
