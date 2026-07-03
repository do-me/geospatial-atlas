<!-- Copyright (c) 2025 Apple Inc. Licensed under MIT License. -->
<script lang="ts">
  import { imageToDataUrl } from "@embedding-atlas/utils";
  import { coordinator as defaultCoordinator, isSelection, makeClient, type MosaicClient } from "@uwdata/mosaic-core";
  import * as SQL from "@uwdata/mosaic-sql";
  import { untrack } from "svelte";

  // Side-effect import: registers a zstd codec with flechette so the
  // Mosaic IPC decoder can inflate the compressed Arrow buffers the
  // backend now emits. Imported here (not just from lib/index.ts) to
  // cover viewer consumers that pull this component via the
  // ``@embedding-atlas/component/svelte`` subpath, which bypasses the
  // package's main entry and would otherwise hit a missing-codec
  // error on the first scatter query. See ipc_codec.ts for context.
  import "../ipc_codec.js";

  import EmbeddingViewImpl from "./EmbeddingViewImpl.svelte";

  import { deepEquals, type Point, type Rectangle, type ViewportState } from "../utils.js";
  import type { EmbeddingViewMosaicProps } from "./embedding_view_mosaic_api.js";
  import { IMAGE_LABEL_SIZE } from "./labels.js";
  import {
    DataPointQuery,
    predicateForDataPoints,
    predicateForRangeSelection,
    queryApproximateDensity,
  } from "./mosaic_client.js";
  import type { DataPoint, DataPointID, Label, LabelContent } from "./types.js";
  import {
    textSummarizerAdd,
    textSummarizerCreate,
    textSummarizerDestroy,
    textSummarizerSummarize,
  } from "./worker/index.js";

  let {
    coordinator = defaultCoordinator(),
    table,
    x,
    y,
    bounds = null,
    precomputed = null,
    viewportHint = null,
    category = null,
    survivor = null,
    text = null,
    image = null,
    importance = null,
    identifier = null,
    filter = null,
    categoryColors = null,
    tooltip = null,
    additionalFields = null,
    selection = null,
    rangeSelection = null,
    rangeSelectionValue = null,
    width = null,
    height = null,
    pixelRatio = null,
    config = null,
    theme = null,
    viewportState = null,
    labels = null,
    customTooltip = null,
    customOverlay = null,
    onViewportState = null,
    onTooltip = null,
    onSelection = null,
    onRangeSelection = null,
    cache = null,
    lines = null,
    linesVisibleTypes = null,
  }: EmbeddingViewMosaicProps = $props();

  // ---- Match Lines (matcher-eval) data client ----------------------------
  // A Mosaic client that fetches the viewport's Match Lines and, when a
  // cross-filter selection is active, keeps only lines whose `id` endpoint
  // survives it (via an `id IN (SELECT ... FROM points WHERE <selection>)`
  // subquery). Being a real client, it re-queries automatically when the
  // selection changes; the embedding view pushes viewport bboxes which trigger
  // `requestQuery()`. Lines are <=400 m, so a gated viewport holds a bounded
  // number; the cap guards against a pathologically dense metro.
  const LINES_VIEWPORT_CAP = 50000;
  type LineRow = { x1: number; y1: number; x2: number; y2: number; pairType: string | null };

  let lineRows = $state.raw<LineRow[]>([]);
  let linesBbox: { xMin: number; xMax: number; yMin: number; yMax: number } | null = null;
  let linesClient: MosaicClient | null = null;

  function buildLinesQuery(predicate: any): any {
    if (lines == null || linesBbox == null) {
      return null;
    }
    let select: Record<string, any> = {
      x1: SQL.column(lines.x1),
      y1: SQL.column(lines.y1),
      x2: SQL.column(lines.x2),
      y2: SQL.column(lines.y2),
    };
    if (lines.pairType != null) {
      select.pairType = SQL.column(lines.pairType);
    }
    let conditions: any[] = [
      SQL.isBetween(SQL.column(lines.x1), [linesBbox.xMin, linesBbox.xMax]),
      SQL.isBetween(SQL.column(lines.y1), [linesBbox.yMin, linesBbox.yMax]),
    ];
    // Cross-filter: keep lines whose `id` endpoint is among the points that
    // pass the active selection. The predicate is over the points table's
    // columns, so it is evaluated inside the subquery against `table`.
    if (predicate != null && String(predicate).trim().length > 0) {
      let pointsWithId = SQL.Query.from(table)
        .select({ id: SQL.column("id") })
        .where(predicate);
      conditions.push(SQL.sql`${SQL.column("id")} IN (${pointsWithId})`);
    }
    return SQL.Query.from(lines.table)
      .select(select)
      .where(SQL.and(...conditions))
      .limit(LINES_VIEWPORT_CAP);
  }

  function extractLineRows(data: any): LineRow[] {
    let x1 = data.getChild("x1").toArray();
    let y1 = data.getChild("y1").toArray();
    let x2 = data.getChild("x2").toArray();
    let y2 = data.getChild("y2").toArray();
    let pairTypeVec = lines?.pairType != null ? data.getChild("pairType") : null;
    let out: LineRow[] = [];
    for (let i = 0; i < x1.length; i++) {
      out.push({
        x1: x1[i],
        y1: y1[i],
        x2: x2[i],
        y2: y2[i],
        pairType: pairTypeVec != null ? String(pairTypeVec.get(i)) : null,
      });
    }
    if (out.length >= LINES_VIEWPORT_CAP) {
      console.warn(`Match Lines: viewport hit the ${LINES_VIEWPORT_CAP}-line cap; some lines not drawn.`);
    }
    return out;
  }

  // Connect/disconnect the lines client to the active selection.
  $effect(() => {
    if (lines == null) {
      return;
    }
    let client = makeClient({
      coordinator,
      selection: filter ?? undefined,
      query: (predicate) => buildLinesQuery(predicate),
      queryResult: (data: any) => {
        lineRows = extractLineRows(data);
      },
    });
    linesClient = client;
    return () => {
      coordinator.disconnect(client);
      if (linesClient === client) {
        linesClient = null;
      }
    };
  });

  // Called by the embedding view when the viewport changes (null = below the
  // zoom gate). Stores the bbox and re-runs the client query.
  function setLinesViewport(bbox: { xMin: number; xMax: number; yMin: number; yMax: number } | null) {
    linesBbox = bbox;
    if (bbox == null) {
      if (lineRows.length > 0) {
        lineRows = [];
      }
      return;
    }
    linesClient?.requestQuery();
  }

  // Stable empty sentinel. Reusing one identity across "no data" updates
  // lets the renderer's gpuBufferData identity check short-circuit
  // instead of zero-filling the destination on every refresh.
  const EMPTY_F32: Float32Array<ArrayBuffer> = new Float32Array(new ArrayBuffer(0));

  let xData: Float32Array<ArrayBuffer> = $state.raw(EMPTY_F32);
  let yData: Float32Array<ArrayBuffer> = $state.raw(EMPTY_F32);
  // Packed wire input — Uint32Array views into the arrow batch when the
  // u32 wire path is active. ``EmbeddingViewImpl`` forwards these to the
  // renderer, which uploads them to a u32 GPU storage buffer and unpacks
  // to f32 via a one-shot compute pass. Avoids the 2.576 GB JS-side
  // ``Float32Array(322M)`` allocation that previously held the heap at
  // 5+ GB on cold load and broke pan-release on stock 4 GB Chrome tabs.
  let xPackedData: Uint32Array<ArrayBuffer> | null = $state.raw(null);
  let yPackedData: Uint32Array<ArrayBuffer> | null = $state.raw(null);
  let coordsBoundsX: [number, number] | null = $state.raw(null);
  let coordsBoundsY: [number, number] | null = $state.raw(null);
  let categoryData: Uint8Array<ArrayBuffer> | null = $state.raw(null);
  // True when ``yData`` arrived already Mercator-projected from the server
  // (GIS Path C only). Prevents the ``EmbeddingViewImpl`` JS projection
  // loop from re-projecting and producing nonsense coords.
  let yIsAlreadyMercator: boolean = $state.raw(false);
  let categoryCount: number = $state.raw(1);
  let totalCount: number = $state.raw(1);
  let maxDensity: number = $state.raw(1);
  let defaultViewportState: ViewportState | null = $state.raw(null);

  let effectiveTooltip: DataPoint | null = $state.raw(null);
  let effectiveSelection: DataPoint[] | null = $state.raw(null);
  let effectiveRangeSelection: Rectangle | Point[] | null = $state.raw(null);

  let clientId: any | null = $state.raw(null);

  $effect(() => {
    // Let Svelte track the dependencies. Include `bounds` and `precomputed`
    // so changing from null → set (or back) rebuilds the client with the
    // right SQL path.
    let deps = {
      coordinator: coordinator,
      source: { table, x, y, category, survivor },
      bounds,
      precomputed,
      viewportHint,
    };

    let client: { destroy: () => void } | null = null;
    let didDestroy = false;

    async function initClient() {
      let source = deps.source;
      // Fast path: when the data source advertises a ``viewportHint`` (the
      // server already knows the bbox + row count from the parquet
      // footer), we can synthesise a centerX / centerY / scaler /
      // totalCount without any query at all. The remaining unknown is
      // ``maxDensity``, which the density ramp uses for color saturation —
      // we kick off the same TABLESAMPLE query but in parallel with the
      // scatter mount, so the ramp converges shortly after first paint
      // instead of blocking the embedding view's mount on a 5 s
      // APPROX_QUANTILE+STDDEV round trip.
      let approxDensity: Awaited<ReturnType<typeof queryApproximateDensity>>;
      if (viewportHint != null) {
        const range = Math.max(viewportHint.rangeX, viewportHint.rangeY, 1e-9);
        const hintedScaler = 1.0 / (range * 0.5);
        approxDensity = {
          centerX: viewportHint.centerX,
          centerY: viewportHint.centerY,
          scaler: hintedScaler,
          totalCount: viewportHint.rowCount ?? 0,
          // ``categoryCount`` is overwritten below if a category column is
          // bound; leave a benign default for the no-category case.
          categoryCount: 1,
          // Approximate the upper bound of points-per-bin so the density
          // colour ramp has something sensible until the real value
          // arrives. Worst case ~5 % of points in the densest bin.
          maxDensity: viewportHint.rowCount
            ? (viewportHint.rowCount * 0.05) / Math.max(0.01 / hintedScaler, 1e-12) ** 2
            : 1,
        };
        // Refine ``maxDensity`` in the background AFTER the scatter query
        // has had a clear runway. Firing immediately would compete with
        // the scatter SQL on DuckDB's thread pool — on a 75 M-row file
        // both queries scan the same ~600 MB of column data, so a
        // sequential schedule is actually faster end-to-end. 2 s is long
        // enough for the scatter to land first on cold loads.
        // Skip the deferred refinement entirely when the loader signals
        // it (very-large datasets — APPROX_QUANTILE on 322 M rows is
        // 200 s of DB load for sub-percent accuracy gain in the colour
        // ramp; the ``viewportHint`` heuristic is good enough at that
        // density).
        if (!viewportHint.skipDeferredRefine) {
          setTimeout(() => {
            if (didDestroy) return;
            const t0 = performance.now();
            queryApproximateDensity(deps.coordinator, source)
              .then((real) => {
                if (didDestroy) return;
                const dt = performance.now() - t0;
                console.info(`[atlas-stage] deferred-density-refine done in ${dt.toFixed(0)}ms`, {
                  categoryCount: real.categoryCount,
                  totalCount: real.totalCount,
                  maxDensity: real.maxDensity,
                });
                totalCount = real.totalCount || totalCount;
                maxDensity = real.maxDensity;
                categoryCount = real.categoryCount;
              })
              .catch(() => {});
          }, 2000);
        } else {
          console.info("[atlas-stage] deferred-density-refine SKIPPED (viewportHint.skipDeferredRefine)");
        }
      } else {
        approxDensity = await queryApproximateDensity(deps.coordinator, source);
      }
      if (didDestroy) {
        return;
      }
      let scaler = approxDensity.scaler * 0.95; // shrink a bit so the point is not exactly on the edge.
      defaultViewportState = { x: approxDensity.centerX, y: approxDensity.centerY, scale: scaler };
      totalCount = approxDensity.totalCount;
      maxDensity = approxDensity.maxDensity;
      categoryCount = approxDensity.categoryCount;

      // Wire-packing: when axis-aligned bounds are advertised by the data
      // source, pack x/y as u32 on the wire (linear min→0, max→2³² − 1)
      // and unpack to f32 on the GPU. u32 is the same wire size as raw
      // f32 but quantises to ~range / (2³² − 1) — at the eubucco 40°-lon
      // span that is 1.5 cm per step, sub-pixel at any zoom (the prior
      // u16 cut quantised to ~110 m and produced a visible street-level
      // grid the moment the user zoomed past city scale).
      //
      // Wire size at 322 M without category: 2.576 GB. Modern V8/Chrome
      // handle this above the historic 2 GB ceiling on 64-bit hosts; if
      // a future dataset trips it the fallback is server-side IPC zstd
      // compression (the table is sorted on the precomputed columns so
      // adjacent u32 deltas are tiny — zstd squashes the wire to ~10 %).
      //
      // Three SQL paths:
      //   (A) precomputed + bounds  → SELECT __x_u32__, __y_u32__ — pure
      //       scan, no per-row arithmetic. Loader baked the cast at CTAS.
      //   (B) bounds only           → ((x - xMin) * xScale)::UINTEGER —
      //       single FMA per row, no GREATEST/LEAST clamps. Bounds are
      //       trusted (the loader computed them); near-bound floats may
      //       round to 2³² but UINTEGER cast wraps to 0 — for our
      //       use-case (sub-pixel quant noise in display coords) that
      //       is invisible. We still defend against NULL via COALESCE.
      //   (C) no bounds             → f32 passthrough, identical wire
      //       bytes to (B) but no GPU unpack pass.
      const packed = bounds != null;
      const packedBounds = bounds;
      const precomputedCols = precomputed;
      // GIS detection: prefer the explicit ``config.isGis`` (set by
      // ``Embedding.svelte``) but fall back to the ``viewportHint``
      // signal — when the loader advertised one, the dataset is GIS by
      // construction. ``server-side mercator`` is a major win only on
      // the Path-C f32 wire (Path A/B coords are pre-quantised to the
      // same linear bounds, no projection needed there).
      const gisProjectInQuery = !packed && (config?.isGis === true || viewportHint != null);
      // Path A's precomputed y column may already encode Mercator-projected
      // values (loader pre-projected at view-definition time). When it
      // does, advertise it through ``yIsAlreadyMercator`` so
      // ``EmbeddingViewImpl`` skips its 5.9 s JS Mercator loop on 322 M
      // rows.
      const precomputedYIsMerc =
        packed && precomputedCols != null && (precomputedCols as { y_is_mercator?: boolean }).y_is_mercator === true;
      yIsAlreadyMercator = gisProjectInQuery || precomputedYIsMerc;
      client = makeClient({
        coordinator: deps.coordinator,
        selection: filter ?? undefined,
        query: (predicate) => {
          // NOTE: We previously pre-released ``xPackedData``/``yPackedData``
          // here on filter change to give V8 ArrayBuffer headroom for the
          // incoming response. That backfired: when the subsequent
          // ``queryResult`` toArray() also OOMs (e.g. 164 M residential
          // with the c column needs 1412 MB and there is still a 6+ GB
          // Arrow-chunk leak retaining most of the heap), the renderer is
          // left with empty state and a blank canvas indefinitely. Better
          // to keep showing the last good data; the new fetch may still
          // fail but at least the prior 322 M render persists. Real fix
          // is to plug the chunk leak (D).
          let xExpr: any;
          let yExpr: any;
          if (packed && packedBounds != null && precomputedCols != null) {
            // Path (A) — fastest. ~6× the on-the-fly cast at 300 M.
            xExpr = SQL.column(precomputedCols.x_u16);
            yExpr = SQL.column(precomputedCols.y_u16);
          } else if (packed && packedBounds != null) {
            // Path (B) — drop clamps, fold to single FMA. ~2.4× the
            // GREATEST/LEAST/ROUND form measured at 300 M.
            const [xMin, xMax] = packedBounds.x;
            const [yMin, yMax] = packedBounds.y;
            const U32_MAX = 4_294_967_295;
            const xScale = U32_MAX / (xMax - xMin);
            const yScale = U32_MAX / (yMax - yMin);
            xExpr = SQL.sql`((COALESCE(${SQL.column(source.x)}, ${xMin}) - ${xMin}) * ${xScale})::UINTEGER`;
            yExpr = SQL.sql`((COALESCE(${SQL.column(source.y)}, ${yMin}) - ${yMin}) * ${yScale})::UINTEGER`;
          } else {
            xExpr = SQL.sql`${SQL.column(source.x)}::FLOAT`;
            // Server-side Mercator projection (GIS Path C only). Saves
            // ~1.8 s on 75 M-row cold loads — DuckDB's vectorised
            // tan/log is C-level fast, while the equivalent JS loop
            // single-threads through ``Math.tan`` 75 M times.
            // ``yIsAlreadyMercator`` tells ``EmbeddingViewImpl`` to
            // skip its own projection so we don't double-project.
            yExpr = gisProjectInQuery
              ? SQL.sql`(LN(TAN(PI()/4 + ${SQL.column(source.y)} * PI() / 360))*180/PI())::FLOAT`
              : SQL.sql`${SQL.column(source.y)}::FLOAT`;
          }
          return SQL.Query.from(source.table)
            .select({
              x: xExpr,
              y: yExpr,
              ...(source.category != null ? { c: SQL.sql`${SQL.column(source.category)}::UTINYINT` } : {}),
              ...(source.category != null && source.survivor != null
                ? { s: SQL.sql`COALESCE((${SQL.column(source.survivor)} = 1)::UTINYINT, 0)` }
                : {}),
            })
            .where(predicate);
        },
        queryResult: async (data: any) => {
          // Browser-side scatter pipeline timing — surfaces in DevTools.
          // ``getChild().toArray()`` is the suspect: if DuckDB exports
          // multi-chunk Arrow (default), this allocates a fresh
          // Float32Array of N rows and memcpy's every chunk into it. With
          // server-side ``combine_chunks`` we get a single chunk, and
          // toArray returns the underlying buffer view at zero cost.
          const t0 = performance.now();
          const numRowsHint = data && typeof data.numRows === "number" ? data.numRows : -1;
          console.info(`[atlas-stage] scatter-queryResult arrived numRows=${numRowsHint} at ${t0.toFixed(0)}`);
          // Drop the previous batch's heap refs BEFORE allocating the next.
          // Each ``getChild().toArray()`` materialises a fresh contiguous
          // buffer (DuckDB emits multi-chunk Arrow at this size, so toArray
          // memcpy-concatenates into a new ArrayBuffer). At 322 M rows that
          // is two 1.29 GB Uint32Arrays + a 322 MB Uint8Array. If the
          // previous batch's arrays are still anchored in the renderer
          // signals, peak heap doubles to ~5.8 GB — which trips V8's
          // ArrayBuffer allocator on zoom-out (full-extent re-fetch).
          xPackedData = null;
          yPackedData = null;
          xData = EMPTY_F32;
          yData = EMPTY_F32;
          categoryData = null;
          // Yield to a macrotask so Svelte 5's ``$state.raw`` flush can
          // propagate the nulls into ``EmbeddingViewImpl`` and from there
          // into ``renderer.setProps`` BEFORE we ask V8 for the next 1.3
          // GB. Heap snapshot under the bug showed 4 × 627 MiB
          // Uint32Arrays pinned via ``renderer.lastXPacked``/
          // ``lastYPacked``; the renderer-side fix in ``renderer.ts``
          // releases them when ``setProps({xPacked: null})`` fires, but
          // that fire is microtask-deferred. Without this yield we run
          // ``toArray()`` while the renderer still holds the prior
          // 1.3 GB+1.3 GB and OOM. ``setTimeout(0)`` is enough to drain
          // both Svelte's flush microtask and the subsequent setProps
          // call. ~1 ms latency, negligible against a multi-second
          // scatter.
          await new Promise((r) => setTimeout(r, 0));
          // Force a major GC between releasing the prior render's
          // buffers and allocating the new ones. With the yield above
          // the renderer has just released its lastXPacked/lastYPacked,
          // so this sweep can actually reclaim them. Requires
          // Electron's ``--expose-gc`` js-flag (set in
          // apps/desktop/electron/main.ts). Gated by row count so the
          // dev/standalone packages (where ``gc`` is never exposed) and
          // small datasets pay no cost.
          if (numRowsHint > 50_000_000 && typeof (globalThis as any).gc === "function") {
            (globalThis as any).gc();
          }
          let xArray, yArray, categoryArray, survivorArray;
          try {
            xArray = data.getChild("x").toArray();
            yArray = data.getChild("y").toArray();
            categoryArray = data.getChild("c")?.toArray() ?? null;
            survivorArray = data.getChild("s")?.toArray() ?? null;
          } catch (err) {
            console.error(`[atlas-stage] scatter-queryResult toArray() failed:`, err);
            throw err;
          }
          const t1 = performance.now();

          // Wire format dictates the renderer's fill path. When the
          // server hands back u32-packed coordinates, we forward the
          // Uint32Array directly to the renderer so it can upload to a
          // u32 GPU storage buffer and run the one-shot unpack compute
          // pass. Skipping the JS-side ``new Float32Array(N)`` round
          // trip avoids allocating a *second* 1.288 GB-per-axis copy of
          // the wire payload on the JS heap — on a 322 M dataset that
          // alone used to push a stock 4 GB Chrome tab into a
          // blank-canvas state after pan-release. The packed path keeps
          // the JS-side footprint at the wire size (one Uint32Array
          // per axis), held only long enough to writeBuffer it to GPU.
          let nextXPacked: Uint32Array<ArrayBuffer> | null = null;
          let nextYPacked: Uint32Array<ArrayBuffer> | null = null;
          let nextBoundsX: [number, number] | null = null;
          let nextBoundsY: [number, number] | null = null;
          let nextX: Float32Array<ArrayBuffer> = EMPTY_F32;
          let nextY: Float32Array<ArrayBuffer> = EMPTY_F32;
          if (packed && packedBounds != null && (xArray instanceof Uint32Array || yArray instanceof Uint32Array)) {
            // Coerce in case only one axis came back u32 (Path B can
            // emit either; Path A always emits both).
            if (!(xArray instanceof Uint32Array)) {
              xArray = xArray != null ? new Uint32Array(xArray) : null;
            }
            if (!(yArray instanceof Uint32Array)) {
              yArray = yArray != null ? new Uint32Array(yArray) : null;
            }
            nextXPacked = xArray as Uint32Array<ArrayBuffer>;
            nextYPacked = yArray as Uint32Array<ArrayBuffer>;
            nextBoundsX = [packedBounds.x[0], packedBounds.x[1]];
            nextBoundsY = [packedBounds.y[0], packedBounds.y[1]];
          } else {
            if (xArray != null && !(xArray instanceof Float32Array)) {
              xArray = new Float32Array(xArray);
            }
            if (yArray != null && !(yArray instanceof Float32Array)) {
              yArray = new Float32Array(yArray);
            }
            nextX = (xArray ?? new Float32Array()) as Float32Array<ArrayBuffer>;
            nextY = (yArray ?? new Float32Array()) as Float32Array<ArrayBuffer>;
          }
          if (categoryArray != null && !(categoryArray instanceof Uint8Array)) {
            categoryArray = new Uint8Array(categoryArray);
          }
          if (categoryArray != null && survivorArray != null) {
            // Pack the survivor flag into bit 7 of the category byte (bits 0-6 =
            // category index). Both GPU renderers unpack it; avoids a 4th storage
            // buffer against the already-tight WebGPU 8-buffer budget.
            for (let i = 0; i < categoryArray.length; i++) {
              if (survivorArray[i] != 0) categoryArray[i] |= 0x80;
            }
          }
          const t2 = performance.now();
          xPackedData = nextXPacked;
          yPackedData = nextYPacked;
          coordsBoundsX = nextBoundsX;
          coordsBoundsY = nextBoundsY;
          xData = nextX;
          yData = nextY;
          categoryData = categoryArray;
          updateTooltip(null);
          updateSelection(null);
          const n = (xArray as any)?.length ?? 0;
          if (n > 1_000_000) {
            const mb =
              (((xArray as any)?.byteLength ?? 0) +
                ((yArray as any)?.byteLength ?? 0) +
                (categoryArray?.byteLength ?? 0)) /
              1024 /
              1024;
            const path = nextXPacked != null ? "u32-direct" : "f32";
            console.log(
              `[scatter] ${n.toLocaleString()} pts (${mb.toFixed(0)} MB JS heap, ${path}) | toArray=${(t1 - t0).toFixed(0)} ms | typed-array-coerce=${(t2 - t1).toFixed(0)} ms`,
            );
          }
        },
      });
      (client as any).reset = () => {
        reset();
      };
      clientId = client;
    }

    initClient();

    return () => {
      clientId = null;
      didDestroy = true;
      client?.destroy();
    };
  });

  // Tooltip
  $effect(() => {
    if (isSelection(tooltip)) {
      let client = clientId;
      if (client == null) {
        return;
      }
      let captured = tooltip;
      effectiveTooltip = (captured.valueFor(client) ?? null) as any;
      let listener = () => {
        effectiveTooltip = (captured.valueFor(client) ?? null) as any;
      };

      $effect(() => {
        let value = effectiveTooltip;
        let source = { x, y, category, identifier };
        captured.update({
          source: client,
          clients: new Set<MosaicClient>().add(client),
          predicate: value != null ? predicateForDataPoints(source, [value]) : null,
          value: value,
        });
      });

      captured.addEventListener("value", listener);
      return () => {
        captured.removeEventListener("value", listener);
        captured.update({
          source: client,
          clients: new Set<MosaicClient>().add(client),
          value: null,
          predicate: null,
        });
      };
    } else if (tooltip == null || typeof tooltip == "object") {
      effectiveTooltip = tooltip;
    } else {
      if (effectiveTooltip?.identifier == tooltip) {
        return;
      }
      let obsolete = false;
      queryPoints([tooltip]).then((value) => {
        if (obsolete) {
          return;
        }
        if (value.length > 0) {
          effectiveTooltip = value[0];
        } else {
          effectiveTooltip = null;
        }
      });
      return () => {
        obsolete = true;
      };
    }
  });

  function updateTooltip(value: DataPoint | null) {
    if (deepEquals(tooltip, value)) {
      return;
    }
    effectiveTooltip = value;
    onTooltip?.(value);
  }

  // Selection
  $effect(() => {
    if (isSelection(selection)) {
      let client = clientId;
      if (client == null) {
        return;
      }
      let captured = selection;
      effectiveSelection = (captured.valueFor(client) ?? null) as any;
      let listener = () => {
        effectiveSelection = (captured.valueFor(client) ?? null) as any;
      };

      $effect(() => {
        let value = effectiveSelection;
        let source = { x, y, category, identifier };
        captured.update({
          source: client,
          clients: new Set<MosaicClient>().add(client),
          predicate: value != null ? predicateForDataPoints(source, value) : null,
          value: value,
        });
      });

      captured.addEventListener("value", listener);
      return () => {
        captured.removeEventListener("value", listener);
        captured.update({
          source: client,
          clients: new Set<MosaicClient>().add(client),
          value: null,
          predicate: null,
        });
      };
    } else if (selection == null) {
      effectiveSelection = null;
    } else if (selection.length == 0) {
      effectiveSelection = [];
    } else {
      if (selection.every((x) => typeof x == "object")) {
        effectiveSelection = selection;
      } else {
        let obsolete = false;
        queryPoints(selection).then((value) => {
          if (obsolete) {
            return;
          }
          effectiveSelection = value;
        });
        return () => {
          obsolete = true;
        };
      }
    }
  });

  function updateSelection(value: DataPoint[] | null) {
    if (deepEquals(selection, value)) {
      return;
    }
    effectiveSelection = value;
    onSelection?.(value);
  }

  // Range Selection
  $effect(() => {
    let client = clientId;
    if (client == null) {
      return;
    }
    let captured = rangeSelection;
    if (captured == null) {
      return;
    }

    $effect(() => {
      let value = effectiveRangeSelection;
      let source = { x, y };
      let clause = {
        source: client,
        clients: new Set<MosaicClient>().add(client),
        predicate: value != null ? predicateForRangeSelection(source, value) : null,
        value: value,
      };
      captured.update(clause);
      captured.activate(clause);
    });

    return () => {
      captured.update({
        source: client,
        clients: new Set<MosaicClient>().add(client),
        value: null,
        predicate: null,
      });
    };
  });

  $effect(() => {
    if (
      !deepEquals(
        untrack(() => effectiveRangeSelection),
        rangeSelectionValue,
      )
    ) {
      effectiveRangeSelection = rangeSelectionValue;
    }
  });

  // Reset tooltip, selection, and range selection.
  function reset() {
    updateSelection(null);
    updateTooltip(null);
    onRangeSelection?.(null);
    effectiveRangeSelection = null;
  }

  // Point query
  let pointQuery = $derived(
    new DataPointQuery(coordinator, { table, x, y, category, text, identifier, additionalFields }),
  );

  async function querySelection(px: number, py: number, unitDistance: number): Promise<DataPoint | null> {
    return await pointQuery.queryClosestPoint(filter?.predicate?.(clientId), px, py, unitDistance);
  }

  async function queryPoints(identifiers: DataPointID[]): Promise<DataPoint[]> {
    return await pointQuery.queryPoints(identifiers);
  }

  // Cluster Labels
  async function queryClusterLabels(clusters: Rectangle[][]): Promise<(LabelContent | null)[]> {
    // If we have image + importance columns, query for representative images
    if (image != null && importance != null) {
      return await queryClusterImageLabels(clusters);
    }
    // Otherwise fall back to text summarization
    if (text == null) {
      return clusters.map(() => null);
    }
    // Create text summarizer (in the worker)
    let summarizer = await textSummarizerCreate({
      regions: clusters,
      stopWords: config?.autoLabelStopWords ?? null,
    });
    // Add text data to the summarizer
    let start = 0;
    let chunkSize = 10000;
    let lastAdd: Promise<unknown> | null = null;
    while (true) {
      let r = await coordinator.query(
        SQL.Query.from(table)
          .select({ x: SQL.column(x), y: SQL.column(y), text: SQL.column(text) })
          .offset(start)
          .limit(chunkSize),
      );
      let data = {
        x: r.getChild("x").toArray(),
        y: r.getChild("y").toArray(),
        text: r.getChild("text").toArray(),
      };
      if (lastAdd != null) {
        await lastAdd;
      }
      lastAdd = textSummarizerAdd(summarizer, data);
      if (r.getChild("text").length < chunkSize) {
        break;
      }
      start += chunkSize;
    }
    if (lastAdd != null) {
      await lastAdd;
    }
    let summarizeResult = await textSummarizerSummarize(summarizer);
    await textSummarizerDestroy(summarizer);

    return summarizeResult.map((words) => {
      if (words.length == 0) {
        return null;
      } else if (words.length > 2) {
        return words.slice(0, 2).join("-") + "-\n" + words.slice(2).join("-");
      } else {
        return words.join("-");
      }
    });
  }

  async function queryClusterImageLabels(clusters: Rectangle[][]): Promise<(LabelContent | null)[]> {
    if (image == null || importance == null) {
      return [];
    }
    // Build a VALUES table of all rectangles with their region index
    let values = clusters
      .flatMap((rects, regionId) =>
        rects.map(
          (r) => SQL.sql`(
            ${SQL.literal(regionId)},
            ${SQL.literal(r.xMin)}, ${SQL.literal(r.xMax)},
            ${SQL.literal(r.yMin)}, ${SQL.literal(r.yMax)}
          )`,
        ),
      )
      .join(", ");
    let sql = `
      WITH rectangles(regionId, xMin, xMax, yMin, yMax) AS (VALUES ${values})
      SELECT
        r.regionId AS regionId,
        arg_max(${SQL.column(image, "t")}, ${SQL.column(importance, "t")}) AS bestImage,
        arg_max(${SQL.column(x, "t")}, ${SQL.column(importance, "t")}) AS bestX,
        arg_max(${SQL.column(y, "t")}, ${SQL.column(importance, "t")}) AS bestY
      FROM rectangles r
      JOIN "${table}" AS t ON
        ${SQL.column(x, "t")} BETWEEN r.xMin AND r.xMax AND
        ${SQL.column(y, "t")} BETWEEN r.yMin AND r.yMax
      GROUP BY r.regionId
      ORDER BY r.regionId
    `;
    let result = await coordinator.query(sql);
    let rows = result.toArray();

    // Map results back by region_id, measuring image dimensions for aspect ratio
    let output: ({
      image: string;
      width: number;
      height: number;
      x: number;
      y: number;
    } | null)[] = clusters.map(() => null);

    for (let i = 0; i < rows.length; i++) {
      let { bestImage, bestX, bestY, regionId } = rows[i];
      if (bestImage == null) continue;
      let dataUrl = imageToDataUrl(bestImage);
      if (dataUrl == null) continue;
      output[regionId] = { image: dataUrl, width: 0, height: 0, x: bestX, y: bestY };
    }

    await Promise.all(
      output.map(async (item) => {
        if (item == null) {
          return;
        }
        let { width, height } = await measureImageSize(item.image);
        // Fit to IMAGE_LABEL_SIZE while maintaining aspect ratio
        let scale = Math.min(IMAGE_LABEL_SIZE / width, IMAGE_LABEL_SIZE / height);
        item.width = width * scale;
        item.height = height * scale;
      }),
    );

    return output;
  }

  // Per-point name labels. When ``config.showPointLabels`` is on and a ``text``
  // column is available, fetch the names of the points currently within the
  // viewport (capped) and render them directly above each dot in
  // EmbeddingViewImpl — always visible, no collision solving.
  //
  // This is a real Mosaic client (``selection: filter``) so it re-queries
  // automatically when the cross-filter changes — legend category selection,
  // cluster filter, range selection — meaning the labels always match exactly
  // the points the scatter is displaying. The viewport bbox is pushed from
  // EmbeddingViewImpl (``onPointLabelsViewport``) and triggers a re-query on
  // pan/zoom via ``requestQuery()``.
  const DEFAULT_POINT_LABELS_MAX = 750;
  let pointNameLabels = $state.raw<Label[] | null>(null);
  let pointLabelsBbox: { xMin: number; xMax: number; yMin: number; yMax: number } | null = null;
  let pointLabelsClient: MosaicClient | null = null;

  function buildPointLabelsQuery(predicate: any): any {
    if (text == null || pointLabelsBbox == null) {
      return null;
    }
    let cap = config?.pointLabelsMaxCount ?? DEFAULT_POINT_LABELS_MAX;
    let conditions: any[] = [
      SQL.sql`${SQL.column(text)} IS NOT NULL AND LENGTH(TRIM(${SQL.column(text)}::VARCHAR)) > 0`,
      SQL.isBetween(SQL.column(x), [pointLabelsBbox.xMin, pointLabelsBbox.xMax]),
      SQL.isBetween(SQL.column(y), [pointLabelsBbox.yMin, pointLabelsBbox.yMax]),
    ];
    // Same cross-filter predicate the scatter obeys — labels track the visible
    // points (e.g. a ``matched_candidate`` legend selection shows only those).
    if (predicate != null && String(predicate).trim().length > 0) {
      conditions.push(predicate);
    }
    return SQL.Query.from(table)
      .select({ x: SQL.column(x), y: SQL.column(y), text: SQL.column(text) })
      .where(SQL.and(...conditions))
      .limit(cap);
  }

  function extractPointNameLabels(data: any): Label[] {
    let xs = data.getChild("x").toArray();
    let ys = data.getChild("y").toArray();
    let ts = data.getChild("text").toArray();
    let out: Label[] = [];
    for (let i = 0; i < xs.length; i++) {
      let content = ts[i];
      if (content == null) {
        continue;
      }
      let str = String(content);
      if (str.length == 0) {
        continue;
      }
      out.push({ x: xs[i], y: ys[i], content: str, level: 0 });
    }
    return out;
  }

  // Connect/disconnect the point-labels client to the active selection.
  $effect(() => {
    if (config?.showPointLabels !== true || text == null) {
      pointNameLabels = null;
      return;
    }
    let client = makeClient({
      coordinator,
      selection: filter ?? undefined,
      query: (predicate) => buildPointLabelsQuery(predicate),
      queryResult: (data: any) => {
        pointNameLabels = extractPointNameLabels(data);
      },
    });
    pointLabelsClient = client;
    return () => {
      coordinator.disconnect(client);
      if (pointLabelsClient === client) {
        pointLabelsClient = null;
      }
      pointNameLabels = null;
    };
  });

  // Called by the embedding view when the viewport changes. Stores the bbox and
  // re-runs the client query (null = point labels off / no viewport yet).
  function setPointLabelsViewport(bbox: { xMin: number; xMax: number; yMin: number; yMax: number } | null) {
    pointLabelsBbox = bbox;
    if (bbox == null) {
      if (pointNameLabels != null) {
        pointNameLabels = null;
      }
      return;
    }
    pointLabelsClient?.requestQuery();
  }

  // The point-name labels shown above each dot when the toggle is on. Kept
  // separate from ``labels`` (cluster labels) — they render through a dedicated
  // always-visible path in EmbeddingViewImpl, not the collision solver.
  let effectivePointLabels = $derived(config?.showPointLabels === true ? pointNameLabels : null);

  function measureImageSize(src: string): Promise<{ width: number; height: number }> {
    return new Promise((resolve) => {
      let img = new Image();
      img.onload = () => resolve({ width: img.naturalWidth, height: img.naturalHeight });
      img.onerror = () => resolve({ width: IMAGE_LABEL_SIZE, height: IMAGE_LABEL_SIZE });
      img.src = src;
    });
  }
</script>

<EmbeddingViewImpl
  width={width ?? 800}
  height={height ?? 800}
  pixelRatio={pixelRatio ?? 2}
  theme={theme}
  config={config}
  data={{
    x: xData,
    y: yData,
    xPacked: xPackedData,
    yPacked: yPackedData,
    coordsBoundsX: coordsBoundsX,
    coordsBoundsY: coordsBoundsY,
    category: categoryData,
  }}
  yIsAlreadyMercator={yIsAlreadyMercator}
  totalCount={totalCount}
  maxDensity={maxDensity}
  categoryCount={categoryColors != null && categoryColors.length > 1 ? categoryColors.length : categoryCount}
  categoryColors={categoryColors}
  defaultViewportState={defaultViewportState}
  querySelection={querySelection}
  queryClusterLabels={queryClusterLabels}
  labels={labels}
  pointLabels={effectivePointLabels}
  onPointLabelsViewport={setPointLabelsViewport}
  customTooltip={customTooltip}
  customOverlay={customOverlay}
  tooltip={effectiveTooltip}
  onTooltip={updateTooltip}
  selection={effectiveSelection}
  onSelection={updateSelection}
  viewportState={viewportState}
  onViewportState={onViewportState}
  rangeSelection={effectiveRangeSelection}
  onRangeSelection={(v) => {
    effectiveRangeSelection = v;
    onRangeSelection?.(v);
  }}
  cache={cache}
  lines={lines}
  lineRows={lineRows}
  onLinesViewport={setLinesViewport}
  linesVisibleTypes={linesVisibleTypes}
/>
