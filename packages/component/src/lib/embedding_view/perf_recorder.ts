/**
 * Frame-time recorder for the embedding view.
 *
 * Captures per-frame CPU-side render() durations into a ring buffer that
 * Playwright (or a manual user via the browser console) can inspect.
 *
 * Enable by setting `window.__atlasPerfEnabled = true` BEFORE the renderer
 * is constructed. After that, every frame appended via `record()` lands in
 * `window.__atlasPerfStats.samples` and the rolling summary is recomputed
 * lazily by `summary()`.
 *
 * Intentionally has zero allocations on the hot path when disabled:
 * a single boolean check and bail.
 */

const RING_SIZE = 4096;

interface PerfStats {
  enabled: boolean;
  // CPU-side cost of building the render command (renderer.render() return).
  cpuSamples: Float32Array;
  // Wall-clock interval between consecutive render() entries — proxies the
  // achievable framerate including GPU + presentation backpressure.
  intervalSamples: Float32Array;
  // GPU completion time (set asynchronously when queue.onSubmittedWorkDone
  // resolves). Same indexing as cpuSamples — entries that haven't completed
  // are NaN.
  gpuSamples: Float32Array;
  cursor: number;
  filled: number;
  lastRenderAt: number;
  resetAt: number;
  pointCount: number;
  downsampledFrames: number;
  totalFrames: number;
}

declare global {
  interface Window {
    __atlasPerfEnabled?: boolean;
    __atlasPerfStats?: PerfStats;
  }
}

function ensureStats(): PerfStats {
  if (typeof window === "undefined") {
    return makeStats();
  }
  if (!window.__atlasPerfStats) {
    window.__atlasPerfStats = makeStats();
  }
  return window.__atlasPerfStats;
}

function makeStats(): PerfStats {
  return {
    enabled: false,
    cpuSamples: new Float32Array(RING_SIZE),
    intervalSamples: new Float32Array(RING_SIZE).fill(NaN),
    gpuSamples: new Float32Array(RING_SIZE).fill(NaN),
    cursor: 0,
    filled: 0,
    lastRenderAt: NaN,
    resetAt: typeof performance !== "undefined" ? performance.now() : 0,
    pointCount: 0,
    downsampledFrames: 0,
    totalFrames: 0,
  };
}

export function isPerfEnabled(): boolean {
  if (typeof window === "undefined") return false;
  return window.__atlasPerfEnabled === true;
}

export interface FrameRecord {
  cpuMs: number;
  downsampled: boolean;
  /** Optional resolver that takes a GPU-completion timestamp (ms). */
  whenGpuDone?: Promise<number>;
}

export function record(rec: FrameRecord): void {
  if (!isPerfEnabled()) return;
  const s = ensureStats();
  s.enabled = true;
  const now = performance.now();
  const slot = s.cursor;
  s.cpuSamples[slot] = rec.cpuMs;
  s.gpuSamples[slot] = NaN;
  if (Number.isFinite(s.lastRenderAt)) {
    s.intervalSamples[slot] = now - s.lastRenderAt;
  } else {
    s.intervalSamples[slot] = NaN;
  }
  s.lastRenderAt = now;
  s.cursor = (s.cursor + 1) % RING_SIZE;
  if (s.filled < RING_SIZE) s.filled++;
  s.totalFrames++;
  if (rec.downsampled) s.downsampledFrames++;
  if (rec.whenGpuDone) {
    rec.whenGpuDone.then((gpuMs) => {
      s.gpuSamples[slot] = gpuMs;
    });
  }
}

export function setPointCount(n: number): void {
  if (!isPerfEnabled()) return;
  const s = ensureStats();
  s.pointCount = n;
}

export function reset(): void {
  if (typeof window === "undefined") return;
  const s = ensureStats();
  s.cursor = 0;
  s.filled = 0;
  s.totalFrames = 0;
  s.downsampledFrames = 0;
  s.resetAt = performance.now();
  s.lastRenderAt = NaN;
  s.intervalSamples.fill(NaN);
  s.gpuSamples.fill(NaN);
}

export interface ChannelSummary {
  count: number;
  meanMs: number;
  p50Ms: number;
  p95Ms: number;
  p99Ms: number;
  maxMs: number;
}

export interface PerfSummary {
  count: number;
  windowSeconds: number;
  fps: number;
  pointCount: number;
  downsampleRatio: number;
  cpu: ChannelSummary;
  interval: ChannelSummary;
  gpu: ChannelSummary;
}

function summarizeChannel(samples: Float32Array, filled: number): ChannelSummary {
  const arr: number[] = [];
  for (let i = 0; i < filled; i++) {
    const v = samples[i];
    if (!Number.isNaN(v)) arr.push(v);
  }
  if (arr.length === 0) {
    return { count: 0, meanMs: 0, p50Ms: 0, p95Ms: 0, p99Ms: 0, maxMs: 0 };
  }
  arr.sort((a, b) => a - b);
  const sum = arr.reduce((a, b) => a + b, 0);
  const pick = (q: number) => arr[Math.min(arr.length - 1, Math.floor(arr.length * q))];
  return {
    count: arr.length,
    meanMs: sum / arr.length,
    p50Ms: pick(0.5),
    p95Ms: pick(0.95),
    p99Ms: pick(0.99),
    maxMs: arr[arr.length - 1],
  };
}

export function summary(): PerfSummary | null {
  if (typeof window === "undefined") return null;
  const s = ensureStats();
  if (s.filled === 0) return null;
  const elapsed = (performance.now() - s.resetAt) / 1000;
  const interval = summarizeChannel(s.intervalSamples, s.filled);
  return {
    count: s.filled,
    windowSeconds: elapsed,
    // Use intervals to compute fps — totalFrames/elapsed is misleading when
    // most of the window was spent waiting for the renderer to start.
    fps: interval.count > 0 ? 1000 / Math.max(interval.meanMs, 1e-6) : s.totalFrames / Math.max(elapsed, 1e-6),
    pointCount: s.pointCount,
    downsampleRatio: s.totalFrames === 0 ? 0 : s.downsampledFrames / s.totalFrames,
    cpu: summarizeChannel(s.cpuSamples, s.filled),
    interval,
    gpu: summarizeChannel(s.gpuSamples, s.filled),
  };
}

if (typeof window !== "undefined") {
  // Expose helpers so they can be driven from the DevTools console too.
  (window as any).__atlasPerf = { record, setPointCount, reset, summary, isPerfEnabled };
}
