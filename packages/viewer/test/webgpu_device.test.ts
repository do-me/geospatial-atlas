// Copyright (c) 2025 Apple Inc. Licensed under MIT License.

import { afterEach, describe, expect, it, vi } from "vitest";

import { requestWebGPUDevice } from "../../component/src/lib/webgpu_renderer/utils.js";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function mockAdapter(features: GPUFeatureName[]) {
  const device = {
    addEventListener: vi.fn(),
    lost: new Promise<GPUDeviceLostInfo>(() => {}),
  };
  const adapter = {
    features: new Set(features),
    limits: { maxBufferSize: 1024 * 1048576, maxStorageBufferBindingSize: 1024 * 1048576 },
    requestDevice: vi.fn().mockResolvedValue(device),
  };
  vi.stubGlobal("navigator", { gpu: { requestAdapter: vi.fn().mockResolvedValue(adapter) } });
  return { adapter, device };
}

describe("requestWebGPUDevice", () => {
  it("retains required chart features on adapters without shader-f16", async () => {
    const { adapter, device } = mockAdapter(["float32-blendable"]);
    await expect(requestWebGPUDevice(["float32-blendable"])).resolves.toEqual({ device, useF16: false });
    expect(adapter.requestDevice.mock.calls[0][0].requiredFeatures).toEqual(["float32-blendable"]);
  });

  it("preserves required features when falling back from f16", async () => {
    const { adapter, device } = mockAdapter(["shader-f16", "float32-blendable"]);
    vi.spyOn(console, "error").mockImplementation(() => {});
    adapter.requestDevice.mockImplementation(async (descriptor: GPUDeviceDescriptor) => {
      if ([...(descriptor.requiredFeatures ?? [])].includes("shader-f16")) {
        throw new Error("f16 device unavailable");
      }
      return device;
    });
    await expect(requestWebGPUDevice(["float32-blendable"])).resolves.toEqual({ device, useF16: false });
    expect(adapter.requestDevice).toHaveBeenCalledTimes(7);
    for (const [descriptor] of adapter.requestDevice.mock.calls) {
      expect(descriptor.requiredFeatures).toContain("float32-blendable");
    }
  });

  it("requests f16 when available without duplicating required features", async () => {
    const { adapter, device } = mockAdapter(["shader-f16"]);
    await expect(requestWebGPUDevice(["shader-f16"])).resolves.toEqual({ device, useF16: true });
    expect(adapter.requestDevice.mock.calls[0][0].requiredFeatures).toEqual(["shader-f16"]);
  });

  it("reports unsupported required features instead of requesting an incompatible device", async () => {
    const { adapter } = mockAdapter(["shader-f16"]);
    const error = vi.spyOn(console, "error").mockImplementation(() => {});
    await expect(requestWebGPUDevice(["float32-blendable"])).resolves.toBeNull();
    expect(error).toHaveBeenCalledWith("WebGPU adapter does not support required features:", "float32-blendable");
    expect(adapter.requestDevice).not.toHaveBeenCalled();
  });

  it("handles environments without navigator", async () => {
    vi.stubGlobal("navigator", undefined);
    await expect(requestWebGPUDevice()).resolves.toBeNull();
  });
});
