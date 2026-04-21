// Copyright (c) 2025 Apple Inc. Licensed under MIT License.

import { mount } from "svelte";

import "../app.css";

import App from "./Entrypoint.svelte";

if (typeof window !== "undefined") {
  const params = new URLSearchParams(window.location.search);
  if (params.get("perf") === "1" || params.get("perf") === "true") {
    (window as any).__atlasPerfEnabled = true;
  }
  const overrides: Record<string, number> = {};
  const numParam = (k: string) => {
    const v = params.get(k);
    if (v === null) return undefined;
    const n = Number(v);
    return Number.isFinite(n) ? n : undefined;
  };
  const dm = numParam("downsampleMax");
  if (dm !== undefined) overrides.downsampleMaxPoints = dm;
  const dw = numParam("densityWeight");
  if (dw !== undefined) overrides.downsampleDensityWeight = dw;
  const ps = numParam("pointSize");
  if (ps !== undefined) overrides.pointSize = ps;
  const ic = numParam("interactiveCap");
  if (ic !== undefined) {
    // 0 disables the adaptive cap entirely. We pass Infinity so the
    // renderer's "Number.isFinite" guard makes it a no-op while still
    // distinguishing "explicitly disabled" from "no override given".
    overrides.downsampleMaxPointsInteractive = ic === 0 ? (Infinity as any) : ic;
  }
  const mode = params.get("renderMode");
  if (mode === "density" || mode === "points") {
    (overrides as any).renderMode = mode;
  }
  // Compute-shader workgroup sizes. Useful for sweeping on Apple Silicon
  // (SIMD=32; 64/128/256 are all SIMD-aligned). `wgDs` sets all three
  // downsample passes in one go; pass wgSample / wgCompact to override
  // individual passes.
  for (const k of ["wgDs", "wgSample", "wgCompact", "wgAcc", "wgBlur"] as const) {
    const v = numParam(k);
    if (v !== undefined) (overrides as any)[k] = v;
  }
  if (Object.keys(overrides).length > 0) {
    (window as any).__atlasPerfOverrides = overrides;
  }
}

const app = mount(App, { target: document.getElementById("app")! });

export default app;
