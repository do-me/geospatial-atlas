// Copyright (c) 2025 Apple Inc. Licensed under MIT License.

import type { Point, ViewportState } from "./utils.js";

export type RenderMode = "points" | "density";

export interface EmbeddingRendererProps {
  mode: RenderMode;
  colorScheme: "light" | "dark";

  x: Float32Array<ArrayBuffer>;
  y: Float32Array<ArrayBuffer>;
  /** Optional u32-packed coordinate input. When set, ``x``/``y`` are
   *  ignored and the renderer unpacks ``xPacked``/``yPacked`` on the GPU
   *  using ``coordsBoundsX``/``coordsBoundsY`` as the linear inverse-map.
   *  The u32 width gives sub-mm quantisation at any reasonable bbox so a
   *  street-level zoom never aliases onto a visible grid (the prior u16
   *  packing did at ~110 m / quantum on the eubucco 40°-lon span). */
  xPacked?: Uint32Array<ArrayBuffer> | null;
  yPacked?: Uint32Array<ArrayBuffer> | null;
  /** Inverse-map bounds for u32 unpack: ``f32 = min + (u32 / (2³² − 1)) * (max - min)``.
   *  Only consulted when ``xPacked``/``yPacked`` is set. */
  coordsBoundsX?: [number, number] | null;
  coordsBoundsY?: [number, number] | null;
  category: Uint8Array<ArrayBuffer> | null;

  categoryCount: number;
  categoryColors: string[] | null;

  viewportX: number;
  viewportY: number;
  viewportScale: number;

  pointSize: number;
  pointAlpha: number;
  pointsAlpha: number;

  densityScaler: number;
  densityBandwidth: number;
  densityQuantizationStep: number;
  densityAlpha: number;
  contoursAlpha: number;

  gamma: number;
  width: number;
  height: number;

  /** Approximate maximum points to render. null/Infinity = no limit. Default: 4,000,000 */
  downsampleMaxPoints: number | null;
  /** Density weight for downsampling (0-10). Default: 5 */
  downsampleDensityWeight: number;
  isGis: boolean;
  /**
   * When true, the renderer skips the per-frame downsample compute chain
   * (cull → sample → compact) and renders from the previous frame's
   * compacted point set. The viewport matrix uniform is still updated, so
   * the cached points reproject correctly under pure pan; only points
   * that should newly enter the viewport are momentarily missing until
   * skipDownsampleCompute drops back to false. Used to keep world-view
   * pan fluent on very large datasets — caller flips this on for the
   * duration of a drag.
   * Default: false.
   */
  skipDownsampleCompute?: boolean;
}

export interface DensityMap {
  data: Float32Array;
  width: number;
  height: number;
  coordinateAtPixel: (x: number, y: number) => Point;
}

export interface EmbeddingRenderer {
  readonly props: EmbeddingRendererProps;

  /** Set renderer props. Returns true if a render is needed. */
  setProps(newProps: Partial<EmbeddingRendererProps>): boolean;

  /** Render */
  render(): void;

  /** Destroy the renderer and free any resource */
  destroy(): void;

  /** Produce a density map */
  densityMap(width: number, height: number, radius: number, viewportState: ViewportState): Promise<DensityMap>;

  /** Promise that resolves when any in-flight u32 → f32 unpack chain has
   *  landed (X submit + drain + destroy, then Y same). Render-loop
   *  callers should ``await`` this before submitting a draw so the f32
   *  storage buffers are guaranteed populated. Optional — only the
   *  WebGPU renderer with the packed path emits it; WebGL renderers
   *  may omit it (the field is then undefined and the caller skips). */
  readonly unpackInFlight?: Promise<void>;
}
