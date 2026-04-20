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
  const mode = params.get("renderMode");
  if (mode === "density" || mode === "points") {
    (overrides as any).renderMode = mode;
  }
  if (Object.keys(overrides).length > 0) {
    (window as any).__atlasPerfOverrides = overrides;
  }
}

const app = mount(App, { target: document.getElementById("app")! });

export default app;
