// Copyright (c) 2025 Apple Inc. Licensed under MIT License.
//
// One-shot u32 → f32 unpack. Input is ``array<u32>`` — one element per
// coordinate, no bit-packing — because u32 is already the natural WGSL
// storage-buffer element width. (The earlier u16 cut shared 2-per-u32
// to halve the wire bytes; with u32 we trade that wire saving for ~65k×
// finer quantisation, which is what kills the street-level grid at
// continental-extent GIS bbox.)
//
// The dispatch writes one f32 per invocation into ``f32_buffer``.
//
// Why a separate module instead of folding into ``program.wgsl``: the
// main program already has 5 pipeline variants (f16/f32 × points/density ×
// downsampled/full). Adding "packed input" as a sixth axis would explode
// the matrix and pollute the hot vertex/compute paths. The unpack pass
// runs once per data load (~1 ms on 322 M points on Apple GPU), so the
// extra dispatch cost is invisible compared to its bypass-the-JS-heap
// payoff.

struct UnpackParams {
  count: u32,
  // Linear inverse-map: f32 = min + u32 * scale, where scale = (max - min) / (2^32 - 1).
  min: f32,
  scale: f32,
  // Padding so the struct is 16-byte aligned (WGSL uniform buffer rule).
  _padding: u32,
}

@group(0) @binding(0) var<storage, read> u32_buffer: array<u32>;
@group(0) @binding(1) var<storage, read_write> f32_buffer: array<f32>;
@group(0) @binding(2) var<uniform> params: UnpackParams;

override wg_unpack: u32 = 256u;
// 2D-dispatch row stride. WebGPU caps any single dispatch dimension at
// 65535, so we tile the work across (workgroups_x = stride / wg_size,
// workgroups_y = ceil(count / stride)). Stride 65536 takes us past
// 4 billion points (workgroups_y ≤ 65535) while keeping
// workgroups_x = 65536 / 256 = 256 ≪ 65535. The host must keep
// ``UNPACK_STRIDE`` in sync — see ``unpack.ts``.
override UNPACK_STRIDE: u32 = 65536u;

@compute @workgroup_size(wg_unpack, 1)
fn unpack(@builtin(global_invocation_id) id: vec3<u32>) {
  let i = id.y * UNPACK_STRIDE + id.x;
  if (i >= params.count) { return; }
  // f32 → u32 promotion is lossy past 2^24 (mantissa width), but here
  // f32(u32) is only an intermediate for the FMA — the final f32 result
  // sits in the bbox span so the relative error stays well below the
  // u32 quantum's f32-representable precision.
  f32_buffer[i] = params.min + f32(u32_buffer[i]) * params.scale;
}
