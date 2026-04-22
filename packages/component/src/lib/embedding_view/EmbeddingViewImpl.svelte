<!-- Copyright (c) 2025 Apple Inc. Licensed under MIT License. -->
<script lang="ts" module>
  interface Props<Selection> {
    data: {
      x: Float32Array<ArrayBuffer>;
      y: Float32Array<ArrayBuffer>;
      category: Uint8Array<ArrayBuffer> | null;
    };
    categoryCount: number;
    categoryColors: string[] | null;
    width: number;
    height: number;
    pixelRatio: number;
    theme: ThemeConfig | null;
    config: EmbeddingViewConfig | null;
    totalCount: number | null;
    maxDensity: number | null;
    labels?: Label[] | null;
    queryClusterLabels: ((clusters: Rectangle[][]) => Promise<(LabelContent | null)[]>) | null;
    tooltip: Selection | null;
    selection: Selection[] | null;
    querySelection: ((x: number, y: number, unitDistance: number) => Promise<Selection | null>) | null;
    rangeSelection: Rectangle | Point[] | null;
    defaultViewportState: ViewportState | null;
    viewportState: ViewportState | null;
    customTooltip: CustomComponent<HTMLDivElement, { tooltip: Selection }> | null;
    customOverlay: CustomComponent<HTMLDivElement, { proxy: OverlayProxy }> | null;
    onViewportState: ((value: ViewportState) => void) | null;
    onTooltip: ((value: Selection | null) => void) | null;
    onSelection: ((value: Selection[] | null) => void) | null;
    onRangeSelection: ((value: Rectangle | Point[] | null) => void) | null;
    cache: Cache | null;
  }

  interface Cluster {
    x: number;
    y: number;
    sumDensity: number;
    rects: Rectangle[];
    bandwidth: number;
    content?: LabelContent | null;
  }

  function viewingParameters(
    maxDensity: number,
    minimumDensity: number,
    scale: number,
    pixelWidth: number,
    pixelHeight: number,
    pixelRatio: number,
    userPointSize: number | null,
  ) {
    // Convert max density to per unit point (aka., CSS px unit).
    let viewDimension = Math.max(pixelWidth, pixelHeight) / pixelRatio;
    let maxPointDensity = maxDensity / (scale * scale) / (viewDimension * viewDimension);
    let maxPixelDensity = maxPointDensity / (pixelRatio * pixelRatio);

    let densityScaler = (1 / maxPixelDensity) * 0.2;

    // The scale such that maxPointDensity == minDensity
    let threshold = Math.sqrt(maxDensity / minimumDensity / (viewDimension * viewDimension));
    let thresholdLevel = Math.log(threshold);
    let scaleLevel = Math.log(scale);

    let factor = (Math.min(Math.max((scaleLevel - thresholdLevel) * 2, -1), 1) + 1) / 2;

    let pointSize: number;
    if (userPointSize != null) {
      // Use user-provided point size, scaled by pixel ratio
      pointSize = userPointSize * pixelRatio;
    } else {
      // Use automatic calculation based on density
      let pointSizeAtThreshold = 0.25 / Math.sqrt(maxPointDensity);
      pointSize = Math.max(0.2, Math.min(5, pointSizeAtThreshold)) * pixelRatio;
    }

    let densityAlpha = 1 - factor;
    let pointsAlpha = 0.5 + factor * 0.5;

    return {
      densityScaler,
      densityAlpha,
      contoursAlpha: densityAlpha,
      pointSize,
      pointAlpha: 0.7,
      pointsAlpha: pointsAlpha,
      densityBandwidth: 20,
    };
  }
</script>

<script lang="ts">
  import { interactionHandler, type CursorValue } from "@embedding-atlas/utils";
  import { onDestroy, onMount } from "svelte";

  import EditableRectangle from "./EditableRectangle.svelte";
  import Lasso from "./Lasso.svelte";
  import StatusBar from "./StatusBar.svelte";
  import TooltipContainer from "./TooltipContainer.svelte";

  import maplibregl from "maplibre-gl";
  import "maplibre-gl/dist/maplibre-gl.css";
  import { Protocol } from "pmtiles";

  const pmtilesProtocol = new Protocol();
  maplibregl.addProtocol("pmtiles", pmtilesProtocol.tile);

  import { defaultCategoryColors } from "../colors.js";
  import type { EmbeddingRenderer } from "../renderer_interface.js";
  import { isPerfEnabled, record as perfRecord, setPointCount as perfSetPointCount } from "./perf_recorder.js";
  import {
    cacheKeyForObject,
    deepEquals,
    pointDistance,
    throttleTooltip,
    type Point,
    type Rectangle,
    type ViewportState,
  } from "../utils.js";
  import { Viewport } from "../viewport_utils.js";
  import { EmbeddingRendererWebGL2 } from "../webgl2_renderer/renderer.js";
  import { EmbeddingRendererWebGPU } from "../webgpu_renderer/renderer.js";
  import { requestWebGPUDevice } from "../webgpu_renderer/utils.js";
  import { customComponentAction, customComponentProps } from "./custom_component_helper.js";
  import type { EmbeddingViewConfig } from "./embedding_view_config.js";
  import { layoutLabels, type LabelWithPlacement } from "./labels.js";
  import { simplifyPolygon } from "./simplify_polygon.js";
  import { resolveTheme, type ThemeConfig } from "./theme.js";
  import type { Cache, CustomComponent, Label, LabelContent, OverlayProxy } from "./types.js";
  import { findClusters } from "./worker/index.js";

  interface SelectionBase {
    x: number;
    y: number;
    category?: number;
    text?: string;
  }

  type Selection = $$Generic<SelectionBase>;

  let {
    data = { x: new Float32Array(), y: new Float32Array(), category: null },
    categoryCount = 1,
    categoryColors = null,
    width = 800,
    height = 800,
    pixelRatio = 2,
    theme = null,
    config = null,
    totalCount = null,
    maxDensity = null,
    labels = null,
    queryClusterLabels = null,
    tooltip = null,
    selection = null,
    querySelection = null,
    rangeSelection = null,
    defaultViewportState = null,
    viewportState = null,
    customTooltip = null,
    customOverlay = null,
    onViewportState = null,
    onTooltip = null,
    onSelection = null,
    onRangeSelection = null,
    cache = null,
  }: Props<Selection> = $props();

  let showClusterLabels = true;

  let colorScheme = $derived(config?.colorScheme ?? "light");
  let resolvedTheme = $derived(resolveTheme(theme, colorScheme));
  let resolvedCategoryColors = $derived(categoryColors ?? defaultCategoryColors(categoryCount));

  let isGis = $derived(config?.isGis ?? false);
  let internalDataY = $derived.by(() => {
    if (isGis && data.y) {
      const y = new Float32Array(data.y.length);
      for (let i = 0; i < data.y.length; i++) {
        y[i] = Viewport.projectLat(data.y[i]);
      }
      return y;
    }
    return data.y;
  });

  // In GIS mode, the minimum scale corresponds to MapLibre zoom level 1.
  // Below this, MapLibre's jumpTo can't keep up and the map/point layers diverge.
  let gisMinScale = $derived(1024 * 2 / (360 * width));

  let resolvedViewportState = $derived.by(() => {
    let state = viewportState ?? defaultViewportState ?? { x: 0, y: 0, scale: 1 };
    if (isGis) {
      let clamped = false;
      let scale = state.scale;
      let y = state.y;
      if (scale < gisMinScale) {
        scale = gisMinScale;
        clamped = true;
      }
      // Clamp Y so the view stays within Mercator bounds (±85.05° lat → ±180 projected)
      const mercatorMax = 180;
      const sy = width >= height ? scale * width / height : scale;
      const visibleHalfY = 1 / sy;
      const yMin = -mercatorMax + visibleHalfY;
      const yMax = mercatorMax - visibleHalfY;
      if (yMin < yMax) {
        if (y < yMin) { y = yMin; clamped = true; }
        if (y > yMax) { y = yMax; clamped = true; }
      }
      if (clamped) {
        return { ...state, scale, y };
      }
    }
    return state;
  });
  let resolvedViewport = $derived(new Viewport(resolvedViewportState, width, height, isGis));
  let pointLocation = $derived(resolvedViewport.pixelLocationFunction());
  let coordinateAtPoint = $derived(resolvedViewport.coordinateAtPixelFunction());

  let preventHover = $state(false);
  // True while the user is actively interacting (drag or recent wheel).
  // Used to drop the effective downsample cap so high-frame-rate pan stays
  // fluent at very large datasets — the configured cap only kicks in once
  // the gesture ends and the renderer has time to draw the full sample.
  let isInteracting = $state(false);
  // Set to true after the first full (non-interactive) render. Gating
  // skipDownsampleCompute on this avoids drawing from an empty
  // compact_indices buffer when a user starts panning before the initial
  // render lands.
  let hasInitialFrame = $state(false);
  // Wall-clock time of the last full downsample compute, used to
  // periodically re-sample during long pans so the visible point set
  // doesn't drift toward "only the points that were in viewport when
  // the gesture began". Refresh interval matches a comfortable visual
  // update cadence (~5Hz).
  let lastFullComputeAt = $state(0);
  const COMPUTE_REFRESH_INTERVAL_MS = 200;
  let interactionDecayTimer: ReturnType<typeof setTimeout> | null = null;
  function bumpInteraction() {
    isInteracting = true;
    if (interactionDecayTimer != null) clearTimeout(interactionDecayTimer);
    interactionDecayTimer = setTimeout(() => {
      isInteracting = false;
      interactionDecayTimer = null;
    }, 150);
  }

  function compareSelection(a: Selection, b: Selection) {
    return a.x == b.x && a.y == b.y && a.category == b.category && a.text == b.text;
  }

  let lockTooltip = $derived(selection?.length == 1 && tooltip != null && compareSelection(selection[0], tooltip));

  function setViewportState(state: ViewportState) {
    if (deepEquals(viewportState, state)) {
      return;
    }
    viewportState = state;
    onViewportState?.(state);
  }

  function setTooltip(newValue: Selection | null) {
    if (deepEquals(tooltip, newValue)) {
      return;
    }
    tooltip = newValue;
    onTooltip?.(newValue);
  }

  function setSelection(newValue: Selection[] | null) {
    if (deepEquals(selection, newValue)) {
      return;
    }
    selection = newValue;
    onSelection?.(newValue);
  }

  function setRangeSelection(newValue: Rectangle | Point[] | null) {
    if (deepEquals(rangeSelection, newValue)) {
      return;
    }
    rangeSelection = newValue;
    onRangeSelection?.(newValue);
  }

  let clusterLabels: LabelWithPlacement[] = $state([]);
  let statusMessage: string | null = $state(null);

  let selectionMode = $state<"marquee" | "lasso" | "none">("none");

  let pixelWidth = $derived(width * pixelRatio);
  let pixelHeight = $derived(height * pixelRatio);

  let canvas: HTMLCanvasElement | null = $state(null);
  let renderer: EmbeddingRenderer | null = $state(null);
  let webGPUPrompt: string | null = $state(null);

  let mapContainer: HTMLElement | undefined = $state();
  let map: maplibregl.Map | undefined = $state();

  let minimumDensity = $derived(config?.minimumDensity ?? 1 / 16);
  // Allow URL params (?downsampleMax=, ?densityWeight=, ?pointSize=) to override
  // the spec for ad-hoc perf experiments without rebuilding.
  let perfOverrides = (typeof window !== "undefined" ? (window as any).__atlasPerfOverrides : null) ?? null;
  let userPointSize = $derived(perfOverrides?.pointSize ?? config?.pointSize ?? null);
  let mode = $derived(perfOverrides?.renderMode ?? config?.mode ?? "points");
  let autoLabelEnabled = $derived(config?.autoLabelEnabled);
  let downsampleMaxPoints = $derived(perfOverrides?.downsampleMaxPoints ?? config?.downsampleMaxPoints ?? 4000000);
  let downsampleDensityWeight = $derived(perfOverrides?.downsampleDensityWeight ?? config?.downsampleDensityWeight ?? 5);
  // Cap to use during active drag/wheel. Defaults to a value that keeps
  // 75M-row Overture parquets at >20fps on Apple Silicon WebGPU; opt-out
  // via downsampleMaxPointsInteractive=null in config or ?interactiveCap=0
  // in the URL.
  let downsampleMaxPointsInteractive = $derived(
    // Explicit Infinity / very large value disables the adaptive cap.
    // ?? short-circuits on null and undefined, so we deliberately use a
    // sentinel here: the caller can pass Infinity to opt out without
    // having to know the configured `downsampleMaxPoints`.
    perfOverrides && "downsampleMaxPointsInteractive" in perfOverrides
      ? perfOverrides.downsampleMaxPointsInteractive
      : config?.downsampleMaxPointsInteractive ?? 200_000,
  );
  let effectiveDownsampleMaxPoints = $derived(
    isInteracting &&
      downsampleMaxPointsInteractive != null &&
      Number.isFinite(downsampleMaxPointsInteractive)
      ? Math.min(downsampleMaxPoints, downsampleMaxPointsInteractive as number)
      : downsampleMaxPoints,
  );
  let effectiveDownsampleDensityWeight = $derived(
    // Density weighting buys nothing while the user is dragging — the
    // renderer otherwise pays for accumulate + blur over all 75M points
    // every frame just to bias sampling that's about to be redrawn.
    isInteracting ? 0 : downsampleDensityWeight,
  );
  let mapStyle = $derived(
    isGis ? (config?.mapStyle !== undefined ? config.mapStyle : "https://tiles.openfreemap.org/styles/liberty") : null,
  );
  let basemapAttribution = $derived(
    typeof mapStyle === "string" && mapStyle.toLowerCase().includes("openfreemap")
      ? "OpenFreeMap © OpenMapTiles Data from OpenStreetMap"
      : null,
  );

  let viewingParams = $derived(
    viewingParameters(
      maxDensity ?? (totalCount ?? data.x.length) / 4,
      minimumDensity,
      resolvedViewportState.scale,
      pixelWidth,
      pixelHeight,
      pixelRatio,
      userPointSize,
    ),
  );

  let pointSize = $derived(viewingParams.pointSize);

  let needsUpdateLabels = true;

  // The viewport at time of the last actual WebGPU render. When a gesture
  // is a pure pan (scale unchanged), we skip the compute+draw pipeline
  // entirely and CSS-translate the already-rendered canvas by the pixel
  // delta between renderedViewport and the current viewport. This is
  // analogous to how maplibre's basemap tiles pan: the expensive work
  // (rasterising points at 4M cap) runs once, then pan is pure compositing.
  // On pan end, or if the pan distance exceeds ~half the viewport (at
  // which point the canvas would expose blank edges), we re-render.
  let renderedViewport: { x: number; y: number; scale: number } | null = null;

  function cssPanDelta() {
    if (!renderedViewport) return null;
    const dx = resolvedViewportState.x - renderedViewport.x;
    const dy = resolvedViewportState.y - renderedViewport.y;
    const s = renderedViewport.scale;
    // Same aspect-corrected scaling the Viewport matrix uses — keeps CSS
    // translate pixel-perfect against where a fresh render would have
    // placed the same world coordinates.
    const sx = width < height ? s * (height / width) : s;
    const sy = width < height ? s : s * (width / height);
    const dxCss = -dx * sx * (width / 2);
    const dyCss = dy * sy * (height / 2);
    return { dxCss, dyCss };
  }

  function applyCssPan() {
    if (!canvas) return;
    const d = cssPanDelta();
    if (!d) return;
    canvas.style.transform = `translate(${d.dxCss}px, ${d.dyCss}px)`;
  }

  function clearCssPan() {
    if (canvas) canvas.style.transform = "";
  }

  // Debug counters exposed on window for Playwright — lets a test assert
  // the CSS-pan path actually ran and the clear happened only after GPU
  // present. Cheap, stripped by tree-shaking if __atlasPerfEnabled is
  // never set on window.
  function bumpDbg(key: string) {
    if (typeof window === "undefined") return;
    const dbg = ((window as any).__atlasPanDbg ??= {
      cssPanApplied: 0,
      cssPanSkipped_noInteract: 0,
      cssPanSkipped_noRendered: 0,
      cssPanSkipped_scaleChanged: 0,
      cssPanSkipped_overLimit: 0,
      clearedViaGpuDone: 0,
      renderCalls: 0,
      lastTransform: "",
    });
    dbg[key] = (dbg[key] ?? 0) + 1;
  }

  $effect.pre(() => {
    // CSS-pan fast path: mid-gesture, scale unchanged, and the canvas
    // bitmap still covers the viewport. Zero GPU work — the user's
    // insight that "if I'm only panning, the points are already there".
    if (!isInteracting) {
      bumpDbg("cssPanSkipped_noInteract");
    } else if (renderedViewport == null) {
      bumpDbg("cssPanSkipped_noRendered");
    } else if (Math.abs(resolvedViewportState.scale - renderedViewport.scale) >= 1e-9) {
      bumpDbg("cssPanSkipped_scaleChanged");
    } else if (
      isInteracting &&
      renderedViewport != null &&
      canvas != null
    ) {
      const d = cssPanDelta();
      if (d != null) {
        // Always apply CSS-pan during interaction — never fall through
        // to a mid-gesture re-render, which causes a visible flash as
        // the canvas briefly shows old content at its un-translated DOM
        // position. Accept blank edges at extreme pan distances; the
        // user either stays within the cached region or tolerates a
        // bit of unrendered margin. The single re-render happens on
        // gesture release.
        const t = `translate(${d.dxCss}px, ${d.dyCss}px)`;
        canvas.style.transform = t;
        bumpDbg("cssPanApplied");
        if (typeof window !== "undefined") {
          ((window as any).__atlasPanDbg as any).lastTransform = t;
        }
        return;
      }
    }
    // Deliberately NOT clearing the CSS transform here. If we cleared it
    // before the new render lands on the canvas, there's a visible gap
    // (CSS style change is synchronous, GPU presentation is not) where
    // the old bitmap sits at its un-translated DOM position while the
    // basemap is already at the new viewport — that's the "points
    // disappear" flash during release. The render() function clears the
    // transform after the GPU has finished presenting the new frame.

    let needsRender = renderer?.setProps({
      mode: mode,
      colorScheme: colorScheme,
      viewportX: resolvedViewportState.x,
      viewportY: resolvedViewportState.y,
      viewportScale: resolvedViewportState.scale,
      width: pixelWidth,
      height: pixelHeight,
      x: data.x,
      y: internalDataY,
      category: data.category,
      categoryCount,
      categoryColors: resolvedCategoryColors,
      downsampleMaxPoints: effectiveDownsampleMaxPoints,
      downsampleDensityWeight: effectiveDownsampleDensityWeight,
      // Reuse the last compacted set during gestures — frees the GPU from
      // re-scanning all 75M points every frame. We refresh on a coarse
      // schedule (~5Hz) so points entering the viewport from outside the
      // last sample area still get picked up, just at lower cadence than
      // the per-frame draw.
      skipDownsampleCompute:
        isInteracting && hasInitialFrame &&
        // When cap is 0 the stale indirect-args from the last non-zero compute
        // would replay the previous frame's draw count — force compute so the
        // downsample early-return clears the args to zero.
        effectiveDownsampleMaxPoints > 0 &&
        performance.now() - lastFullComputeAt < COMPUTE_REFRESH_INTERVAL_MS,
      isGis,
      ...viewingParams,
    });
    if (needsRender) {
      // We can't tell from setProps whether the renderer chose the skip
      // path — the next render() does. Approximate: any frame that wasn't
      // configured to skip is a "full" frame.
      const willCompute =
        !isInteracting ||
        !hasInitialFrame ||
        performance.now() - lastFullComputeAt >= COMPUTE_REFRESH_INTERVAL_MS;
      if (willCompute) {
        lastFullComputeAt = performance.now();
        hasInitialFrame = true;
      }
      // Snapshot the viewport that this full render will reflect — the
      // CSS-pan fast path needs it to compute the translate delta on
      // the next mid-gesture frame.
      renderedViewport = {
        x: resolvedViewportState.x,
        y: resolvedViewportState.y,
        scale: resolvedViewportState.scale,
      };
    }

    if (needsRender) {
      setNeedsRender();
      if (
        (autoLabelEnabled !== false || labels != null) &&
        needsUpdateLabels &&
        renderer != null &&
        data.x != null &&
        data.x.length > 0 &&
        defaultViewportState != null
      ) {
        needsUpdateLabels = false;
        updateLabels(defaultViewportState);
      }
    }
  });

  function render() {
    _request = null;
    if (!canvas || !renderer) {
      return;
    }
    // Only assign width/height when they actually change. Per HTML spec,
    // even same-value assignment resets the canvas bitmap, which on WebGPU
    // invalidates the swap chain and causes a 1-frame blank at the moment
    // of release.
    if (canvas.width !== renderer.props.width) canvas.width = renderer.props.width;
    if (canvas.height !== renderer.props.height) canvas.height = renderer.props.height;
    const cssW = `${renderer.props.width / pixelRatio}px`;
    const cssH = `${renderer.props.height / pixelRatio}px`;
    if (canvas.style.width !== cssW) canvas.style.width = cssW;
    if (canvas.style.height !== cssH) canvas.style.height = cssH;
    const localCanvas = canvas;
    const dev = (renderer as any).gpuDevice as GPUDevice | undefined;
    if (isPerfEnabled()) {
      const t0 = performance.now();
      renderer.render();
      const dt = performance.now() - t0;
      const count = renderer.props.x?.length ?? 0;
      const cap = renderer.props.downsampleMaxPoints;
      const downsampled = cap != null && Number.isFinite(cap) && cap > 0 && count > cap;
      perfSetPointCount(count);
      const whenGpuDone = dev
        ? dev.queue.onSubmittedWorkDone().then(() => performance.now() - t0)
        : undefined;
      perfRecord({ cpuMs: dt, downsampled, whenGpuDone });
    } else {
      renderer.render();
    }
    // Clear the CSS-pan transform after the GPU has finished presenting
    // the fresh frame — only then is it safe to drop the old translated
    // bitmap. Clearing earlier causes a visible misalignment flash.
    // Guarded by !isInteracting so we don't clobber a new in-flight pan
    // that may have started before the prior GPU work finished.
    bumpDbg("renderCalls");
    if (dev) {
      // Defer the CSS-transform clear by one rAF after onSubmittedWorkDone.
      // The rAF fires just BEFORE the next compositor paint, so when the
      // new swap-chain texture and the cleared transform both get committed
      // they land on the same paint cycle — a stale transform on new
      // content would be a visible double-translate flash. Clearing
      // straight from the microtask lets one paint cycle go by with the
      // old texture plus cleared transform, which reads as a "snap back"
      // to pre-drag position.
      dev.queue.onSubmittedWorkDone().then(() => {
        if (localCanvas && !isInteracting) {
          requestAnimationFrame(() => {
            if (localCanvas && !isInteracting) {
              localCanvas.style.transform = "";
              bumpDbg("clearedViaGpuDone");
            }
          });
        }
      });
    } else if (!isInteracting) {
      requestAnimationFrame(() => {
        if (localCanvas && !isInteracting) {
          localCanvas.style.transform = "";
          bumpDbg("clearedViaGpuDone");
        }
      });
    }
  }

  let _request: number | null = null;
  function setNeedsRender() {
    if (_request == null) {
      _request = requestAnimationFrame(render);
    }
  }

  function setupWebGLRenderer(canvas: HTMLCanvasElement) {
    webGPUPrompt = "WebGPU is unavailable. Falling back to WebGL.";

    let context: WebGL2RenderingContext | null;

    function createRenderer() {
      context = canvas.getContext("webgl2", { antialias: false });
      if (context == null) {
        console.error("Could not get WebGL 2 context");
        return;
      }
      context.getExtension("EXT_color_buffer_float");
      context.getExtension("EXT_float_blend");
      context.getExtension("OES_texture_float_linear");
      renderer = new EmbeddingRendererWebGL2(context, pixelWidth, pixelHeight);
    }

    createRenderer();

    canvas.addEventListener("webglcontextlost", () => {
      renderer?.destroy();
      renderer = null;
      context = null;
    });

    canvas.addEventListener("webglcontextrestored", () => {
      createRenderer();
    });
  }

  function setupWebGPURenderer(canvas: HTMLCanvasElement) {
    let canFallbackToWebGL = true;

    async function createRenderer() {
      let device = await requestWebGPUDevice();
      if (device == null) {
        console.error("Could not get WebGPU device");
        if (canFallbackToWebGL) {
          setupWebGLRenderer(canvas);
        }
        return;
      }

      let context = canvas.getContext("webgpu");
      if (context == null) {
        console.error("Could not get WebGPU canvas context");
        if (canFallbackToWebGL) {
          setupWebGLRenderer(canvas);
        }
        return;
      }

      // Once we get the context, we can't fallback to setupWebGLRenderer.
      canFallbackToWebGL = false;

      device.lost.then(async (info) => {
        console.info(`WebGPU device was lost: ${info.message}`);
        if (info.reason != "destroyed") {
          renderer?.destroy();
          renderer = null;
          context.unconfigure();
          await createRenderer();
        }
      });

      let format = navigator.gpu.getPreferredCanvasFormat();

      context.configure({
        device: device,
        format: format,
        alphaMode: "premultiplied",
      });

      renderer = new EmbeddingRendererWebGPU(context, device, format, pixelWidth, pixelHeight);
    }

    createRenderer();
  }

  function syncViewportState(defaultViewportState: ViewportState | null) {
    if (defaultViewportState != null && viewportState == null) {
      setViewportState(defaultViewportState);
    }
  }

  $effect.pre(() => syncViewportState(defaultViewportState));

  $effect(() => {
    if (map && resolvedViewportState) {
      const { x, y, scale } = resolvedViewportState;
      const zoom = Math.log2((360 * scale * width) / 1024);

      map.jumpTo({
        center: [x, isGis ? Viewport.unprojectLat(y) : y],
        zoom: zoom,
      });

      // Expose viewport state for E2E drift testing
      if (typeof window !== "undefined") {
        (window as any).__geospatialAtlasViewport = { x, y, scale, width, height, isGis };
      }
    }
  });

  $effect(() => {
    if (map && width && height) {
      map.resize();
    }
  });

  $effect(() => {
    if (mapContainer && mapStyle) {
      if (!map) {
        map = new maplibregl.Map({
          container: mapContainer,
          style: mapStyle,
          center: [
            resolvedViewportState.x,
            isGis ? Viewport.unprojectLat(resolvedViewportState.y) : resolvedViewportState.y,
          ],
          zoom: Math.log2((360 * resolvedViewportState.scale * width) / 1024),
          minZoom: 1,
          interactive: false,
          attributionControl: false,
          // Needed so MCP `get_map_screenshot` (and any other toDataURL-
          // based capture) includes the rendered basemap tiles. Without
          // this, the WebGL drawing buffer is cleared after each frame
          // and the screenshot shows only the overlay.
          preserveDrawingBuffer: true,
        });
        // Expose map instance for E2E testing (no-op in production bundles via tree-shaking)
        if (typeof window !== "undefined") {
          (window as any).__geospatialAtlasMap = map;
        }
      } else {
        map.setStyle(mapStyle);
      }
    } else if (map && !mapStyle) {
      map.remove();
      map = undefined;
    }
  });

  onMount(() => {
    if (canvas == null) {
      return;
    }
    // Setup WebGPU renderer (with fallback to WebGL)
    setupWebGPURenderer(canvas);

    // Override toDataURL. This is because we must submit the render commands before
    // calling toDataURL, to ensure the current image is populated with contents.
    let _toDataURL = canvas.toDataURL;
    canvas.toDataURL = (...args) => {
      render();
      return _toDataURL.apply(canvas, args);
    };
  });

  onDestroy(() => {
    renderer?.destroy();
    renderer = null;
    if (map) {
      map.remove();
      map = undefined;
    }
  });

  function localCoordinates(e: { clientX: number; clientY: number }): Point {
    let rect = canvas?.getBoundingClientRect() ?? { left: 0, top: 0 };
    return { x: e.clientX - rect.left, y: e.clientY - rect.top };
  }

  function onWheel(e: WheelEvent) {
    e.preventDefault();
    bumpInteraction();
    let { x, y } = localCoordinates(e);
    let scaler = Math.exp(-e.deltaY / 200);
    onZoom(scaler, { x, y });
  }

  function onZoom(scaler: number, position: Point) {
    let { x, y, scale } = resolvedViewportState;
    setTooltip(null);
    let maxScale: number;
    let minScale: number;
    if (isGis) {
      maxScale = Infinity;
      minScale = gisMinScale;
    } else {
      maxScale = (defaultViewportState?.scale ?? 1) * 1e2;
      minScale = (defaultViewportState?.scale ?? 1) * 1e-2;
    }
    let newScale = Math.min(maxScale, Math.max(minScale, scale * scaler));
    let rect = canvas!.getBoundingClientRect();
    let sz = Math.max(rect.width, rect.height);
    let px = ((position.x - rect.width / 2) / sz) * 2;
    let py = ((rect.height / 2 - position.y) / sz) * 2;
    let newX = x + px / scale - px / newScale;
    let newY = y + py / scale - py / newScale;
    setViewportState({
      x: newX,
      y: newY,
      scale: newScale,
    });
  }

  function onDrag(e1: CursorValue) {
    setTooltip(null);

    let mode: "marquee" | "lasso" | "pan" = "pan";
    if (selectionMode != "none") {
      if (!e1.modifiers.shift) {
        mode = selectionMode;
      }
    } else {
      if (e1.modifiers.shift) {
        mode = e1.modifiers.meta ? "lasso" : "marquee";
      }
    }

    let p1 = localCoordinates(e1);

    switch (mode) {
      case "marquee": {
        return {
          move: (e2: CursorValue) => {
            setTooltip(null);
            if (renderer == null) {
              return;
            }
            let p2 = localCoordinates(e2);
            let l1 = coordinateAtPoint(p1.x, p1.y);
            let l2 = coordinateAtPoint(p2.x, p2.y);
            setRangeSelection({
              xMin: Math.min(l1.x, l2.x),
              yMin: Math.min(l1.y, l2.y),
              xMax: Math.max(l1.x, l2.x),
              yMax: Math.max(l1.y, l2.y),
            });
          },
        };
      }
      case "lasso": {
        let points = [coordinateAtPoint(p1.x, p1.y)];
        return {
          move: (e2: CursorValue) => {
            setTooltip(null);
            if (renderer == null) {
              return;
            }
            let p2 = localCoordinates(e2);
            points = [...points, coordinateAtPoint(p2.x, p2.y)];
            if (points.length >= 3) {
              setRangeSelection(simplifyPolygon(points, 24));
            }
          },
        };
      }
      case "pan": {
        let c0 = coordinateAtPoint(0, 0);
        let c1 = coordinateAtPoint(1, 1);
        let sx = c0.x - c1.x;
        // In GIS mode, the y coordinate return by coordinateAtPoint is unprojected (latitude).
        // However, the viewport state uses projected y coordinate (Mercator).
        // Since the projection is isotropic, we can use the x scale (which is linear) to determine the y scale.
        let sy = isGis ? -sx : c0.y - c1.y;
        let x0 = resolvedViewportState.x;
        let y0 = resolvedViewportState.y;
        bumpInteraction();
        return {
          move: (e2: CursorValue) => {
            bumpInteraction();
            setViewportState({
              x: x0 + (e2.clientX - e1.clientX) * sx,
              y: y0 + (e2.clientY - e1.clientY) * sy,
              scale: resolvedViewportState.scale,
            });
          },
          up: () => {
            // With the CSS-pan fast path no GPU work is in flight during
            // the gesture, so we can snap isInteracting false immediately
            // — the next $effect.pre run does the full re-render at the
            // release viewport and clears the canvas CSS transform in one
            // frame. Skipping the decay makes the visual "settle" instant.
            if (interactionDecayTimer != null) {
              clearTimeout(interactionDecayTimer);
              interactionDecayTimer = null;
            }
            isInteracting = false;
          },
        };
      }
    }
  }

  async function onClick(pointer: CursorValue) {
    if (rangeSelection != null) {
      setRangeSelection(null);
    } else {
      const newSelection = await selectionFromPoint(localCoordinates(pointer));
      if (newSelection == null) {
        setSelection([]);
        setTooltip(null);
      } else {
        if (pointer.modifiers.shift || pointer.modifiers.ctrl || pointer.modifiers.meta) {
          // Toggle the point from the selection
          let index = selection?.findIndex((item) => {
            return item.x == newSelection.x && item.y == newSelection.y && item.category == newSelection.category;
          });
          if (selection == null || index == null || index < 0) {
            setSelection([...(selection ?? []), newSelection]);
            setTooltip(newSelection);
          } else {
            setSelection([...selection.slice(0, index), ...selection.slice(index + 1)]);
            setTooltip(null);
          }
        } else {
          setSelection([newSelection]);
          setTooltip(newSelection);
        }
      }
    }
  }

  let onHoverThrottle = throttleTooltip(
    async (pointer: CursorValue | null) => {
      let position = pointer ? localCoordinates(pointer) : null;
      if (selection != null && selection.length == 1) {
        let cSelection = pointLocation(selection[0].x, selection[0].y);
        if (position != null && pointDistance(position, cSelection) < 10) {
          setTooltip(selection[0]);
        }
      } else {
        setTooltip(await selectionFromPoint(position));
      }
    },
    () => tooltip != null,
  );

  function onHover(e: CursorValue | null) {
    if (e != null) {
      if (!preventHover) {
        onHoverThrottle(e);
      }
    } else {
      onHoverThrottle(null);
    }
  }

  $effect.pre(() => {
    if (preventHover) {
      onHoverThrottle(null);
    }
  });

  async function selectionFromPoint(position: Point | null) {
    if (renderer == null || position == null || querySelection == null) {
      return null;
    }
    const { x, y } = coordinateAtPoint(position.x, position.y);
    const dLon = Math.abs(coordinateAtPoint(position.x + 1, position.y).x - x);
    const dLat = Math.abs(coordinateAtPoint(position.x, position.y + 1).y - y);
    const r = Math.max(dLon, dLat);
    return await querySelection(x, y, r);
  }

  async function generateClusters(
    renderer: EmbeddingRenderer,
    bandwidth: number,
    viewport: ViewportState,
    densityThreshold: number = 0.005,
  ): Promise<Cluster[]> {
    let map = await renderer.densityMap(1000, 1000, bandwidth, viewport);
    let cs = await findClusters(map.data, map.width, map.height);
    let collectedClusters: Cluster[] = [];
    for (let idx = 0; idx < cs.length; idx++) {
      let c = cs[idx];
      let coord = map.coordinateAtPixel(c.meanX, c.meanY);
      let rects: Rectangle[] = c.boundaryRectApproximation!.map(([x1, y1, x2, y2]) => {
        let p1 = map.coordinateAtPixel(x1, y1);
        let p2 = map.coordinateAtPixel(x2, y2);
        return {
          xMin: Math.min(p1.x, p2.x),
          xMax: Math.max(p1.x, p2.x),
          yMin: Math.min(p1.y, p2.y),
          yMax: Math.max(p1.y, p2.y),
        };
      });
      collectedClusters.push({
        x: coord.x,
        y: coord.y,
        sumDensity: c.sumDensity,
        rects: rects,
        bandwidth: bandwidth,
      });
    }
    let maxDensity = collectedClusters.reduce((a, b) => Math.max(a, b.sumDensity), 0);
    return collectedClusters.filter((x) => x.sumDensity / maxDensity > densityThreshold);
  }

  async function generateLabels(viewport: ViewportState): Promise<Label[]> {
    if (renderer == null || queryClusterLabels == null) {
      return [];
    }

    let cacheKey = await cacheKeyForObject({
      autoLabel: {
        version: 3,
        viewport,
        stopWords: config?.autoLabelStopWords,
        densityThreshold: config?.autoLabelDensityThreshold,
      },
    });

    if (cache != null) {
      let cached = await cache.get(cacheKey);
      if (cached != null) {
        return cached;
      }
    }

    let newClusters = await generateClusters(renderer, 10, viewport, config?.autoLabelDensityThreshold ?? 0.005);
    newClusters = newClusters.concat(await generateClusters(renderer, 5, viewport));

    let labels = await queryClusterLabels(newClusters.map((x) => x.rects));
    for (let i = 0; i < newClusters.length; i++) {
      let label = labels[i];
      newClusters[i].content = label;
      if (typeof label == "object" && label != null && "x" in label && "y" in label) {
        if (label.x != null && label.y != null) {
          newClusters[i].x = label.x;
          newClusters[i].y = label.y;
        }
      }
    }

    let result: Label[] = newClusters
      .filter((x) => x.content != null && (typeof x.content !== "string" || x.content.length > 0))
      .map((x) => ({
        x: x.x,
        y: x.y,
        content: x.content!,
        priority: x.sumDensity,
        level: x.bandwidth == 10 ? 0 : 1,
      }));

    if (cache != null) {
      await cache.set(cacheKey, result);
    }

    return result;
  }

  async function updateLabels(viewport: ViewportState) {
    let vp = new Viewport(viewport, 1000, 1000);
    if (renderer == null) {
      return;
    }
    if (labels != null) {
      clusterLabels = await layoutLabels(vp.scale(), labels, resolvedTheme.fontFamily);
    } else {
      statusMessage = "Generating labels...";
      try {
        let result = await generateLabels(viewport);
        clusterLabels = await layoutLabels(vp.scale(), result, resolvedTheme.fontFamily);
      } catch (e) {
        console.error("Error while generating labels", e);
      } finally {
        statusMessage = null;
      }
    }
  }

  class DefaultTooltipRenderer {
    content: HTMLElement;
    constructor(target: HTMLElement, props: { tooltip: Selection; colorScheme: "light" | "dark"; fontFamily: string }) {
      let content = document.createElement("div");
      this.content = content;
      this.update(props);
      target.appendChild(content);
    }

    update(props: { tooltip: Selection; colorScheme: "light" | "dark"; fontFamily: string }) {
      let content = this.content;
      content.style.fontFamily = props.fontFamily;
      if (colorScheme == "light") {
        content.style.color = "#000";
        content.style.background = "#fff";
        content.style.border = "1px solid #000";
      } else {
        content.style.color = "#ccc";
        content.style.background = "#000";
        content.style.border = "1px solid #ccc";
      }
      content.style.borderRadius = "2px";
      content.style.padding = "5px";
      content.style.fontSize = "12px";
      content.style.maxWidth = "300px";
      content.innerText = props.tooltip.text ?? JSON.stringify(props.tooltip);
    }
  }
</script>

<div style:width="{width}px" style:height="{height}px" style:position="relative">
  <div
    bind:this={mapContainer}
    style:width="100%"
    style:height="100%"
    style:position="absolute"
    style:top="0"
    style:left="0"
    style:z-index="0"
    style:background-color={isGis && !mapStyle ? "black" : undefined}
  ></div>
  <canvas
    bind:this={canvas}
    style:position="absolute"
    style:top="0"
    style:left="0"
    style:z-index="1"
    style:pointer-events="none"
  ></canvas>
  <div style:width="{width}px" style:height="{height}px" style:position="absolute" style:top="0" style:left="0">
    {#if customOverlay}
      {@const action = customComponentAction(customOverlay)}
      {@const proxy = { location: pointLocation, width: width, height: height }}
      {#key action}
        <div use:action={customComponentProps(customOverlay, { proxy: proxy })}></div>
      {/key}
    {/if}
  </div>
  <svg
    width={width}
    height={height}
    style:position="absolute"
    style:left="0"
    style:top="0"
    role="none"
    onwheel={onWheel}
    use:interactionHandler={{
      click: onClick,
      drag: onDrag,
      hover: onHover,
    }}
  >
    <!-- Tooltip point -->
    {#if tooltip != null && renderer != null}
      {@const { x, y } = pointLocation(tooltip.x, tooltip.y)}
      {@const r = Math.max(3, pointSize / pixelRatio) + 1}
      {#if isFinite(x) && isFinite(y) && isFinite(r)}
        <circle
          cx={x}
          cy={y}
          r={r}
          style:stroke={colorScheme == "light" ? "#000" : "#fff"}
          style:stroke-width={1}
          style:fill="none"
        />
      {/if}
    {/if}
    <!-- Selection point(s) -->
    {#if selection != null && renderer != null}
      {#each selection as point}
        {@const { x, y } = pointLocation(point.x, point.y)}
        {@const color = point.category != null ? resolvedCategoryColors[point.category] : resolvedCategoryColors[0]}
        {@const r = Math.max(3, pointSize / pixelRatio) + 1}
        {#if isFinite(x) && isFinite(y) && isFinite(r)}
          <circle
            cx={x}
            cy={y}
            r={r}
            style:stroke={colorScheme == "light" ? "#000" : "#fff"}
            style:stroke-width={2}
            style:fill={color}
          />
        {/if}
      {/each}
    {/if}
    <!-- Cluster labels -->
    {#if showClusterLabels}
      <g>
        {#each clusterLabels as label}
          {@const location = pointLocation(label.coordinate.x, label.coordinate.y)}
          {@const scale = resolvedViewport.scale()}
          {@const isVisible =
            label.placement != null && label.placement.minScale <= scale && scale <= label.placement.maxScale}
          <g transform="translate({location.x},{location.y})">
            {#if isVisible}
              {#if typeof label.content !== "string"}
                <image
                  href={label.content.image}
                  x={-label.content.width / 2}
                  y={-label.content.height / 2}
                  width={label.content.width}
                  height={label.content.height}
                  style:user-select="none"
                  style:-webkit-user-select="none"
                  style:opacity={resolvedTheme.clusterLabelOpacity}
                />
              {:else}
                {@const rows = label.content.split("\n")}
                <g>
                  {#each rows as row, index}
                    <text
                      style:paint-order="stroke"
                      style:stroke-width="4"
                      style:stroke-linejoin="round"
                      style:stroke-linecap="round"
                      style:text-anchor="middle"
                      style:fill={resolvedTheme.clusterLabelColor}
                      style:stroke={resolvedTheme.clusterLabelOutlineColor}
                      style:opacity={resolvedTheme.clusterLabelOpacity}
                      style:user-select="none"
                      style:-webkit-user-select="none"
                      style:font-family={resolvedTheme.fontFamily}
                      x={0}
                      y={(index - (rows.length - 1) / 2) * label.fontSize}
                      font-size={label.fontSize}
                      dominant-baseline="middle"
                    >
                      {row}
                    </text>
                  {/each}
                </g>
              {/if}
            {/if}
          </g>
        {/each}
      </g>
    {/if}
    <!-- Range selection interaction and display -->
    {#if rangeSelection != null && renderer != null}
      {#if rangeSelection instanceof Array}
        <Lasso value={rangeSelection} pointLocation={pointLocation} />
      {:else}
        <EditableRectangle
          value={rangeSelection}
          onChange={setRangeSelection}
          pointLocation={pointLocation}
          coordinateAtPoint={coordinateAtPoint}
          preventHover={(value) => {
            preventHover = value;
          }}
        />
      {/if}
    {/if}
  </svg>
  <!-- Tooltip popup -->
  {#if tooltip != null && renderer != null}
    {@const loc = pointLocation(tooltip.x, tooltip.y)}
    <TooltipContainer
      location={loc}
      allowInteraction={lockTooltip}
      targetHeight={Math.max(3, pointSize / pixelRatio)}
      customTooltip={customTooltip ?? {
        class: DefaultTooltipRenderer,
        props: { colorScheme: colorScheme, fontFamily: resolvedTheme.fontFamily },
      }}
      tooltip={tooltip}
    />
  {/if}
  <!-- Status bar -->
  {#if resolvedTheme.statusBar}
    <StatusBar
      resolvedTheme={resolvedTheme}
      statusMessage={statusMessage ?? webGPUPrompt}
      distancePerPoint={1 / (pointLocation(1, 0).x - pointLocation(0, 0).x)}
      pointCount={data.x.length}
      selectionMode={selectionMode}
      onSelectionMode={(v) => (selectionMode = v)}
      basemapAttribution={basemapAttribution}
    />
  {/if}
</div>
