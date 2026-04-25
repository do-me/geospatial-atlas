// Copyright (c) 2025 Apple Inc. Licensed under MIT License.

/**
 * Workgroup-size tunables for the compute shaders in `program.wgsl`.
 *
 * Apple Silicon GPUs have a 32-wide SIMD and 8 SIMDs per core (256
 * concurrent threads max per threadgroup). Smaller groups give higher
 * thread-group-in-flight counts for latency hiding; larger groups
 * amortise dispatch overhead and workgroup-reduction fixed costs.
 *
 * The shader declares `override wg_*: u32 = ...` constants so the host
 * can set them at pipeline creation time — no WGSL recompile.
 *
 * Overrides come from `window.__atlasPerfOverrides.wg{Ds,Compact,Acc,
 * Blur}` (set by the viewer from URL params). Missing / invalid values
 * fall through to the defaults below. Strides must stay a multiple of
 * their workgroup size; we clamp dispatch to 65535 / dim.
 */

export interface WgConfig {
  downsampleCull: number; // default 256
  densitySample: number; // default 256
  compact: number; // default 256
  accumulate: number; // default 64
  gaussianBlur: number; // default 64
  /** Downsample stride (must be multiple of downsampleCull/densitySample/compact). */
  downsampleStride: number; // default 65536
  /** Accumulate stride (must be multiple of accumulate). */
  accumulateStride: number; // default 65536
}

// WebGPU caps any single dispatch dimension at 65535. The accumulate
// pass dispatches (stride/accumulate, ceil(count/stride)) — so for the
// Y axis to stay legal we need ``stride * 65535 >= count``. The old
// 4096 stride overflowed at ~268M points: every render at that scale
// produced an "Invalid CommandBuffer" warning from Dawn and a black
// canvas. 65536 takes us past 4 billion points while still leaving
// workgroupsX = 65536/64 = 1024 ≤ 65535.
const DEFAULT_CONFIG: WgConfig = {
  downsampleCull: 256,
  densitySample: 256,
  compact: 256,
  accumulate: 64,
  gaussianBlur: 64,
  downsampleStride: 65536,
  accumulateStride: 65536,
};

function intFromWindow(key: string): number | undefined {
  if (typeof window === "undefined") return undefined;
  const ov = (window as any).__atlasPerfOverrides;
  if (ov == null) return undefined;
  const v = ov[key];
  if (typeof v !== "number" || !Number.isFinite(v) || v <= 0) return undefined;
  return Math.floor(v);
}

function ensureStrideMultiple(stride: number, wg: number): number {
  // Round up to nearest multiple of wg.
  if (stride % wg === 0) return stride;
  return Math.ceil(stride / wg) * wg;
}

/** Resolve workgroup config at runtime. Called lazily so URL params set
 *  before component construction are picked up. */
export function resolveWgConfig(): WgConfig {
  const base = { ...DEFAULT_CONFIG };
  const ds = intFromWindow("wgDs");
  const sample = intFromWindow("wgSample") ?? ds;
  const compact = intFromWindow("wgCompact") ?? ds;
  const acc = intFromWindow("wgAcc");
  const blur = intFromWindow("wgBlur");
  if (ds) base.downsampleCull = ds;
  if (sample) base.densitySample = sample;
  if (compact) base.compact = compact;
  if (acc) base.accumulate = acc;
  if (blur) base.gaussianBlur = blur;

  // Strides must be divisible by each corresponding wg size so the 2D
  // dispatch maps cleanly. Grow them to the next multiple if the user's
  // chosen wg_size doesn't divide the default. downsample strides are
  // shared across three passes; must divide by all three.
  const dsLcm = lcm3(base.downsampleCull, base.densitySample, base.compact);
  base.downsampleStride = ensureStrideMultiple(base.downsampleStride, dsLcm);
  base.accumulateStride = ensureStrideMultiple(base.accumulateStride, base.accumulate);
  return base;
}

function gcd(a: number, b: number): number {
  while (b !== 0) {
    const t = b;
    b = a % b;
    a = t;
  }
  return a;
}

function lcm(a: number, b: number): number {
  return (a / gcd(a, b)) * b;
}

function lcm3(a: number, b: number, c: number): number {
  return lcm(lcm(a, b), c);
}

/** Pipeline-constant map matching the `override` declarations in
 *  `program.wgsl`. Pass to `createComputePipeline({ compute: { ...,
 *  constants }})`. */
export function downsamplePipelineConstants(cfg: WgConfig) {
  return {
    wg_downsample_cull: cfg.downsampleCull,
    wg_density_sample: cfg.densitySample,
    wg_compact: cfg.compact,
    DOWNSAMPLE_STRIDE: cfg.downsampleStride,
  };
}

export function accumulatePipelineConstants(cfg: WgConfig) {
  return {
    wg_accumulate: cfg.accumulate,
    ACCUMULATE_STRIDE: cfg.accumulateStride,
  };
}

export function gaussianBlurPipelineConstants(cfg: WgConfig) {
  return {
    wg_gaussian_blur: cfg.gaussianBlur,
  };
}
