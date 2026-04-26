// Copyright (c) 2025 Apple Inc. Licensed under MIT License.
//
// One-shot u16 → f32 unpack for the scatter coordinate buffers.
//
// At 322 M rows the JS-side ``new Float32Array(N)`` for x and y was 2.576 GB
// of heap pressure and the single biggest reason a stock 4 GB Chrome tab
// went blank after pan. Sending u16 over the wire and unpacking on the
// GPU gets the JS heap back to ~1.3 GB peak.
//
// Lifetime model: the u16 source buffer + the 16-byte uniform buffer are
// **ephemeral** — created via ``mappedAtCreation: true``, written, dispatched,
// then defer-destroyed once the queue drains. They are never persisted
// across data loads.
//
// Why ephemeral: a previous version held the u16 source as a persistent
// dataflow node. At 322 M that's an extra ~644 MB per axis sitting in the
// GPU process on top of the persistent 1.288 GB f32 destinations — peak
// GPU-process residency hit ~5 GB and crashed the GPU process on cold load
// (every subsequent ``buffer.destroy()`` then threw "valid external
// Instance reference no longer exists"). Allocating fresh per dispatch
// keeps the steady-state GPU footprint at the f32 destinations only.
//
// Why ``mappedAtCreation: true`` instead of ``device.queue.writeBuffer``:
// ``writeBuffer`` allocates a same-size staging buffer inside the GPU
// process and copies through it. A 644 MB write means a 644 MB staging
// allocation in addition to the destination — same OOM trap. ``mappedAtCreation``
// hands us a renderer-process-mapped range, we ``set()`` into it, then
// ``unmap()`` transfers ownership to the GPU process — no double allocation.

import type { Dataflow, Node } from "../dataflow.js";

import unpackShaderCode from "./unpack.wgsl?raw";

/** Bounds for u16 → f32 unpack: ``f32 = min + u16 * (max - min) / 65535``. */
export interface CoordsBounds1D {
  min: number;
  max: number;
}

const UNPACK_WG_SIZE = 256;
// 2D-dispatch stride. WebGPU caps a single dispatch dimension at 65535,
// so the kernel walks ``id.y * STRIDE + id.x`` and we dispatch
// ``(STRIDE / wg_size, ceil(count / STRIDE))``. 65536 supports up to
// ~4 B points (65535 * 65536) before workgroups_y overflows. Must stay
// in sync with ``UNPACK_STRIDE`` in ``unpack.wgsl``.
const UNPACK_STRIDE = 65536;

/** Compile the unpack shader module. Cheap (<1 ms) and idempotent — the
 *  dataflow caches the result against ``device``. */
function makeUnpackModule(df: Dataflow, device: Node<GPUDevice>): Node<GPUShaderModule> {
  return df.derive([device], (device) => device.createShaderModule({ code: unpackShaderCode }));
}

/** Compile the unpack compute pipeline + bind group layout. The pipeline
 *  is reused across both axes and across data updates. */
export interface UnpackPipeline {
  pipeline: Node<GPUComputePipeline>;
  bindGroupLayout: Node<GPUBindGroupLayout>;
}

export function makeUnpackPipeline(df: Dataflow, device: Node<GPUDevice>): UnpackPipeline {
  const module = makeUnpackModule(df, device);
  const bindGroupLayout = df.derive([device], (device) =>
    device.createBindGroupLayout({
      entries: [
        { binding: 0, visibility: GPUShaderStage.COMPUTE, buffer: { type: "read-only-storage" } },
        { binding: 1, visibility: GPUShaderStage.COMPUTE, buffer: { type: "storage" } },
        { binding: 2, visibility: GPUShaderStage.COMPUTE, buffer: { type: "uniform" } },
      ],
    }),
  );
  const pipeline = df.derive([device, module, bindGroupLayout], (device, module, bindGroupLayout) =>
    device.createComputePipeline({
      layout: device.createPipelineLayout({ bindGroupLayouts: [bindGroupLayout] }),
      compute: {
        module,
        entryPoint: "unpack",
        constants: { wg_unpack: UNPACK_WG_SIZE, UNPACK_STRIDE: UNPACK_STRIDE },
      },
    }),
  );
  return { pipeline, bindGroupLayout };
}

/** Encode + submit a one-shot compute pass that fills ``f32Dest`` from
 *  ``xPacked`` using the linear inverse-map advertised in ``bounds``.
 *
 *  Allocates the u16 source + uniform buffers fresh each call (via
 *  ``mappedAtCreation: true`` so no GPU-process staging copy) and
 *  defer-destroys them once the queue drains. See the file header
 *  for the GPU-memory rationale. */
export function runUnpack(
  device: GPUDevice,
  pipeline: GPUComputePipeline,
  bindGroupLayout: GPUBindGroupLayout,
  xPacked: Uint16Array,
  f32Dest: GPUBuffer,
  bounds: CoordsBounds1D,
): void {
  const count = xPacked.length;
  // u32-aligned: 4 bytes per pair of u16, plus a 2-byte tail when count
  // is odd. The shader bails past ``params.count`` so the trailing
  // zero-padded slot is never read.
  const u16Bytes = Math.max(4, Math.ceil(count / 2) * 4);
  const u16Buffer = device.createBuffer({
    size: u16Bytes,
    usage: GPUBufferUsage.STORAGE,
    mappedAtCreation: true,
  });
  new Uint16Array(u16Buffer.getMappedRange()).set(xPacked);
  u16Buffer.unmap();

  // Uniforms: count u32, min f32, scale f32, padding u32 (16 bytes).
  const uniformBuffer = device.createBuffer({
    size: 16,
    usage: GPUBufferUsage.UNIFORM,
    mappedAtCreation: true,
  });
  const uniformView = new DataView(uniformBuffer.getMappedRange());
  uniformView.setUint32(0, count, /* littleEndian */ true);
  uniformView.setFloat32(4, bounds.min, true);
  // scale = (max - min) / 65535. Encoded once on the host so the shader
  // is a single FMA per invocation.
  const scale = (bounds.max - bounds.min) / 65535;
  uniformView.setFloat32(8, scale, true);
  uniformView.setUint32(12, 0, true);
  uniformBuffer.unmap();

  const bindGroup = device.createBindGroup({
    layout: bindGroupLayout,
    entries: [
      { binding: 0, resource: { buffer: u16Buffer } },
      { binding: 1, resource: { buffer: f32Dest } },
      { binding: 2, resource: { buffer: uniformBuffer } },
    ],
  });

  const encoder = device.createCommandEncoder();
  const pass = encoder.beginComputePass();
  pass.setPipeline(pipeline);
  pass.setBindGroup(0, bindGroup);
  // 2D dispatch — see ``UNPACK_STRIDE`` comment above. The shader walks
  // ``id.y * STRIDE + id.x`` and bails when ``i >= count``, so it's safe
  // for the y-tiling to over-cover the tail of the buffer.
  const workgroupsX = UNPACK_STRIDE / UNPACK_WG_SIZE;
  const workgroupsY = Math.max(1, Math.ceil(count / UNPACK_STRIDE));
  pass.dispatchWorkgroups(workgroupsX, workgroupsY);
  pass.end();
  device.queue.submit([encoder.finish()]);

  // Defer-destroy: the in-flight dispatch still references both buffers
  // until the submit completes. ``onSubmittedWorkDone`` resolves once
  // every prior submit has executed — destroying earlier triggers the
  // ``MTLDevice``-poisoning fault that ``utils.ts:gpuBuffer`` already
  // works around for the persistent f32 destinations.
  device.queue.onSubmittedWorkDone().then(() => {
    u16Buffer.destroy();
    uniformBuffer.destroy();
  });
}
