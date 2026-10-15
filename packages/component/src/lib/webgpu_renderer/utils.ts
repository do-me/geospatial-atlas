// Copyright (c) 2025 Apple Inc. Licensed under MIT License.

export function isWebGPUAvailable(): boolean {
  return typeof navigator !== "undefined" && navigator.gpu != undefined && navigator.gpu.requestAdapter != undefined;
}

export interface WebGPUDeviceResult {
  device: GPUDevice;
  /** True if shader-f16 was successfully requested; the renderer can then use
   *  the f16 program variant for half-precision blur storage. False means the
   *  adapter doesn't expose shader-f16 (common on NVIDIA Pascal / Intel UHD
   *  via Dawn D3D12), so the renderer must fall back to the f32 variant. */
  useF16: boolean;
}

export async function requestWebGPUDevice(requiredFeatures: GPUFeatureName[] = []): Promise<WebGPUDeviceResult | null> {
  if (!isWebGPUAvailable()) {
    return null;
  }

  let adapter: GPUAdapter | null;
  try {
    adapter = await navigator.gpu.requestAdapter();
  } catch (error) {
    console.error("Could not request WebGPU adapter:", error);
    return null;
  }
  if (!adapter) {
    console.error("Could not request WebGPU adapter");
    return null;
  }

  const missing = requiredFeatures.filter((feature) => !adapter.features.has(feature));
  if (missing.length > 0) {
    console.error("WebGPU adapter does not support required features:", missing.join(", "));
    return null;
  }

  const limitPresets: (null | number)[] = [null, 512, 256, 128, 64, 32];
  function buildDescriptor(sz: number | null, features: GPUFeatureName[]): GPUDeviceDescriptor {
    const maxBuf = sz == null ? adapter!.limits.maxBufferSize : Math.min(sz * 1048576, adapter!.limits.maxBufferSize);
    const maxStor =
      sz == null
        ? adapter!.limits.maxStorageBufferBindingSize
        : Math.min(sz * 1048576, adapter!.limits.maxStorageBufferBindingSize);
    return {
      requiredLimits: { maxBufferSize: maxBuf, maxStorageBufferBindingSize: maxStor },
      requiredFeatures: features,
    };
  }

  // Try shader-f16 first (half the blur-buffer memory, small perf win on
  // capable GPUs). If the adapter doesn't expose it, fall back to f32.
  const adapterSupportsF16 = adapter.features.has("shader-f16");
  const attempts: Array<{ features: GPUFeatureName[]; useF16: boolean }> = [];
  if (adapterSupportsF16) {
    attempts.push({ features: [...new Set<GPUFeatureName>(["shader-f16", ...requiredFeatures])], useF16: true });
  }
  if (!requiredFeatures.includes("shader-f16")) {
    attempts.push({ features: requiredFeatures, useF16: false });
  }

  for (const { features, useF16 } of attempts) {
    for (const sz of limitPresets) {
      try {
        const device = await adapter.requestDevice(buildDescriptor(sz, features));
        installGpuErrorObservability(device, { useF16, sizePresetMiB: sz });
        return { device, useF16 };
      } catch (error) {
        console.error(error);
        continue;
      }
    }
  }
  return null;
}

interface AtlasGpuErrorRecord {
  t: number;
  kind: "uncaptured" | "lost";
  message: string;
  reason?: string;
}

declare global {
  interface Window {
    __atlasGpuErrors?: AtlasGpuErrorRecord[];
    __atlasGpuDeviceInfo?: { useF16: boolean; sizePresetMiB: number | null; t: number };
  }
}

function installGpuErrorObservability(device: GPUDevice, meta: { useF16: boolean; sizePresetMiB: number | null }) {
  deviceLifetime(device);
  if (typeof window === "undefined") return;
  if (!Array.isArray(window.__atlasGpuErrors)) window.__atlasGpuErrors = [];
  window.__atlasGpuDeviceInfo = { ...meta, t: performance.now() };
  // ``uncapturederror`` is the per-validation/OOM event channel — without
  // this listener every WebGPU error is silently swallowed and we only
  // see the downstream Metal "ignored submissions" cascade in stderr,
  // with no clue about the original triggering error.
  device.addEventListener("uncapturederror", (ev) => {
    const e = (ev as GPUUncapturedErrorEvent).error;
    const rec: AtlasGpuErrorRecord = {
      t: performance.now(),
      kind: "uncaptured",
      message: e?.message ?? String(e),
    };
    window.__atlasGpuErrors!.push(rec);
    console.warn("[atlas-gpu] uncapturederror:", rec.message);
  });
  device.lost.then((info) => {
    const rec: AtlasGpuErrorRecord = {
      t: performance.now(),
      kind: "lost",
      message: info.message,
      reason: info.reason,
    };
    window.__atlasGpuErrors!.push(rec);
    console.warn("[atlas-gpu] device.lost:", rec.reason, rec.message);
  });
}

interface DeviceLifetime {
  lost: GPUDeviceLostInfo | null;
  waiters: Set<(info: GPUDeviceLostInfo) => void>;
}

const deviceLifetimes = new WeakMap<GPUDevice, DeviceLifetime>();

function deviceLifetime(device: GPUDevice): DeviceLifetime {
  let lifetime = deviceLifetimes.get(device);
  if (!lifetime) {
    const created: DeviceLifetime = {
      lost: null,
      waiters: new Set(),
    };
    device.lost.then((info) => {
      created.lost = info;
      for (const notify of created.waiters) notify(info);
      created.waiters.clear();
    });
    deviceLifetimes.set(device, created);
    lifetime = created;
  }
  return lifetime;
}

export async function waitForGPUQueue(device: GPUDevice): Promise<void> {
  const lifetime = deviceLifetime(device);
  return new Promise<void>((resolve, reject) => {
    const onLoss = (info: GPUDeviceLostInfo) => reject(new Error(`WebGPU device was lost: ${info.message}`));
    if (lifetime.lost) {
      onLoss(lifetime.lost);
      return;
    }
    lifetime.waiters.add(onLoss);
    try {
      device.queue.onSubmittedWorkDone().then(
        () => {
          lifetime.waiters.delete(onLoss);
          resolve();
        },
        (error) => {
          lifetime.waiters.delete(onLoss);
          reject(error);
        },
      );
    } catch (error) {
      lifetime.waiters.delete(onLoss);
      reject(error);
    }
  });
}

export async function destroyGPUResource(device: GPUDevice, resource: GPUBuffer | GPUTexture): Promise<void> {
  const lifetime = deviceLifetime(device);
  if (lifetime.lost) return;
  try {
    await waitForGPUQueue(device);
    // Device loss already releases native resources; calling destroy on
    // their invalid Dawn instance can itself throw during GPU recovery.
    if (!lifetime.lost) resource.destroy();
  } catch (error) {
    if (!lifetime.lost) console.warn("[atlas-gpu] Resource cleanup failed:", error);
  }
}

function correctedBufferSize(size: number): number {
  if (size == 0) {
    size = 4;
  }
  if (size % 4 != 0) {
    size += 4 - (size % 4);
  }
  return size;
}

export function gpuBuffer(
  state: {
    buffer?: GPUBuffer;
    byteSize?: number;
    usage?: GPUBufferUsageFlags;
    destroy?: () => void;
  },
  device: GPUDevice,
  byteSize: number,
  usage: GPUBufferUsageFlags,
): GPUBuffer {
  if (state.buffer == null || state.byteSize != byteSize || state.usage != usage) {
    if (state.buffer != null) {
      // Defer destroy until queued work that may still reference this
      // buffer drains. Without this, reactive size changes (e.g. a
      // post-mount density refinement updating ``categoryCount`` or
      // ``maxDensity``) free the buffer while a queued command buffer
      // is still holding a Metal page reference — that fault then
      // poisons the entire MTLDevice and every subsequent submit
      // (WebGPU, WebGL, compositor) reports
      // ``kIOGPUCommandBufferCallbackErrorSubmissionsIgnored``.
      const old = state.buffer;
      void destroyGPUResource(device, old);
    }
    state.buffer = device.createBuffer({ size: correctedBufferSize(byteSize), usage: usage });
    state.byteSize = byteSize;
    state.usage = usage;
    state.destroy = () => {
      const cur = state.buffer;
      state.buffer = undefined;
      if (cur != null) {
        void destroyGPUResource(device, cur);
      }
    };
  }
  return state.buffer;
}

export function gpuBufferData(
  state: {
    buffer?: GPUBuffer;
    data?: BufferSource | null;
    destroy?: () => void;
  },
  device: GPUDevice,
  buffer: GPUBuffer,
  data: BufferSource | null,
) {
  if (state.buffer !== buffer || state.data !== data) {
    if (data != null) {
      if (data.byteLength % 4 != 0) {
        let n = data.byteLength - (data.byteLength % 4);
        device.queue.writeBuffer(buffer, 0, data, 0, n);
        if (data instanceof Uint8Array) {
          let remaining = new Uint8Array(4);
          for (let i = 0; i < 4; i++) {
            if (n + i < data.length) {
              remaining[i] = data[n + i];
            }
          }
          device.queue.writeBuffer(buffer, n, remaining);
        }
      } else {
        device.queue.writeBuffer(buffer, 0, data, 0);
      }
    } else {
      device.queue.writeBuffer(buffer, 0, new ArrayBuffer(buffer.size));
    }
    state.buffer = buffer;
    state.data = data;
  }
  return buffer;
}

export function gpuTexture(
  state: {
    texture?: GPUTexture;
    width?: number;
    height?: number;
    format?: GPUTextureFormat;
    usage?: GPUTextureUsageFlags;
    destroy?: () => void;
  },
  device: GPUDevice,
  width: number,
  height: number,
  format: GPUTextureFormat,
  usage: GPUTextureUsageFlags,
): GPUTexture {
  if (
    state.texture == null ||
    state.width != width ||
    state.height != height ||
    state.format != format ||
    state.usage != usage
  ) {
    if (state.texture != null) {
      const old = state.texture;
      void destroyGPUResource(device, old);
    }
    state.texture = device.createTexture({ size: [width, height], format: format, usage: usage });
    state.width = width;
    state.height = height;
    state.format = format;
    state.usage = usage;
    state.destroy = () => {
      const cur = state.texture;
      state.texture = undefined;
      if (cur != null) {
        void destroyGPUResource(device, cur);
      }
    };
  }
  return state.texture;
}
