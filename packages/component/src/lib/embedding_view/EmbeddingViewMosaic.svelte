<!-- Copyright (c) 2025 Apple Inc. Licensed under MIT License. -->
<script lang="ts">
  import { imageToDataUrl } from "@embedding-atlas/utils";
  import { coordinator as defaultCoordinator, isSelection, makeClient, type MosaicClient } from "@uwdata/mosaic-core";
  import * as SQL from "@uwdata/mosaic-sql";
  import { untrack } from "svelte";

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
  import type { DataPoint, DataPointID, LabelContent } from "./types.js";
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
    category = null,
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
  }: EmbeddingViewMosaicProps = $props();

  let xData: Float32Array<ArrayBuffer> = $state.raw(new Float32Array());
  let yData: Float32Array<ArrayBuffer> = $state.raw(new Float32Array());
  let categoryData: Uint8Array<ArrayBuffer> | null = $state.raw(null);
  let categoryCount: number = $state.raw(1);
  let totalCount: number = $state.raw(1);
  let maxDensity: number = $state.raw(1);
  let defaultViewportState: ViewportState | null = $state.raw(null);

  let effectiveTooltip: DataPoint | null = $state.raw(null);
  let effectiveSelection: DataPoint[] | null = $state.raw(null);
  let effectiveRangeSelection: Rectangle | Point[] | null = $state.raw(null);

  let clientId: any | null = $state.raw(null);

  $effect(() => {
    // Let Svelte track the dependencies. Include `bounds` so changing from
    // null → set (or back) rebuilds the client with the right packed-SQL
    // path.
    let deps = { coordinator: coordinator, source: { table, x, y, category }, bounds };

    let client: { destroy: () => void } | null = null;
    let didDestroy = false;

    async function initClient() {
      let source = deps.source;
      let approxDensity = await queryApproximateDensity(deps.coordinator, source);
      if (didDestroy) {
        return;
      }
      let scaler = approxDensity.scaler * 0.95; // shrink a bit so the point is not exactly on the edge.
      defaultViewportState = { x: approxDensity.centerX, y: approxDensity.centerY, scale: scaler };
      totalCount = approxDensity.totalCount;
      maxDensity = approxDensity.maxDensity;
      categoryCount = approxDensity.categoryCount;

      // Wire-packing: when axis-aligned bounds are advertised by the data
      // source, pack x/y as u16 on the wire (linear min→0, max→65535) and
      // unpack to f32 on receipt. Payload drops from (f32 + f32 + u8) = 9
      // bytes/point to (u16 + u16 + u8) = 5 bytes/point — about 44 %
      // smaller for the 75 M-point scatter query. Quantization error at
      // the bbox scale is ≤ (range / 65535), well below one screen pixel
      // for global GIS views.
      const packed = bounds != null;
      const packedBounds = bounds;
      client = makeClient({
        coordinator: deps.coordinator,
        selection: filter ?? undefined,
        query: (predicate) => {
          let xExpr: any;
          let yExpr: any;
          if (packed && packedBounds != null) {
            const [xMin, xMax] = packedBounds.x;
            const [yMin, yMax] = packedBounds.y;
            const xRange = xMax - xMin;
            const yRange = yMax - yMin;
            // GREATEST/LEAST clamp so an out-of-bbox row (possible with
            // filters that inject synthetic rows) never overflows u16.
            xExpr = SQL.sql`GREATEST(0, LEAST(65535, ROUND((${SQL.column(source.x)} - ${xMin}) / ${xRange} * 65535)))::USMALLINT`;
            yExpr = SQL.sql`GREATEST(0, LEAST(65535, ROUND((${SQL.column(source.y)} - ${yMin}) / ${yRange} * 65535)))::USMALLINT`;
          } else {
            xExpr = SQL.sql`${SQL.column(source.x)}::FLOAT`;
            yExpr = SQL.sql`${SQL.column(source.y)}::FLOAT`;
          }
          return SQL.Query.from(source.table)
            .select({
              x: xExpr,
              y: yExpr,
              ...(source.category != null ? { c: SQL.sql`${SQL.column(source.category)}::UTINYINT` } : {}),
            })
            .where(predicate);
        },
        queryResult: (data: any) => {
          let xArray = data.getChild("x").toArray();
          let yArray = data.getChild("y").toArray();
          let categoryArray = data.getChild("c")?.toArray() ?? null;

          if (packed && packedBounds != null && (xArray instanceof Uint16Array || yArray instanceof Uint16Array)) {
            // Unpack u16 → f32 using the same linear map the server used
            // to quantize. Doing this once per batch is cheap compared to
            // the bytes we just saved on the wire.
            const [xMin, xMax] = packedBounds.x;
            const [yMin, yMax] = packedBounds.y;
            const xScale = (xMax - xMin) / 65535;
            const yScale = (yMax - yMin) / 65535;
            const n = xArray?.length ?? yArray?.length ?? 0;
            const xOut = new Float32Array(n);
            const yOut = new Float32Array(n);
            for (let i = 0; i < n; i++) {
              xOut[i] = xMin + (xArray as Uint16Array)[i] * xScale;
              yOut[i] = yMin + (yArray as Uint16Array)[i] * yScale;
            }
            xArray = xOut;
            yArray = yOut;
          } else {
            // Ensure that the arrays are typed arrays.
            if (xArray != null && !(xArray instanceof Float32Array)) {
              xArray = new Float32Array(xArray);
            }
            if (yArray != null && !(yArray instanceof Float32Array)) {
              yArray = new Float32Array(yArray);
            }
          }
          if (categoryArray != null && !(categoryArray instanceof Uint8Array)) {
            categoryArray = new Uint8Array(categoryArray);
          }
          xData = xArray;
          yData = yArray;
          categoryData = categoryArray;
          updateTooltip(null);
          updateSelection(null);
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
  data={{ x: xData, y: yData, category: categoryData }}
  totalCount={totalCount}
  maxDensity={maxDensity}
  categoryCount={categoryCount}
  categoryColors={categoryColors}
  defaultViewportState={defaultViewportState}
  querySelection={querySelection}
  queryClusterLabels={queryClusterLabels}
  labels={labels}
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
/>
