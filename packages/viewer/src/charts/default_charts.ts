// Copyright (c) 2025 Apple Inc. Licensed under MIT License.

import type { Coordinator } from "@uwdata/mosaic-core";

import { columnDescriptions, distinctCountBatch, type ColumnDesc } from "../utils/database.js";
import type { BuiltinChartSpec } from "./chart_types.js";
import type { EmbeddingSpec } from "./embedding/types.js";
import type { InstancesSpec } from "./instances/types.js";
import type { ChartSpec } from "./spec/spec.js";

export interface DefaultChartsConfig {
  /** If specified, only include the given columns */
  include?: string[];

  /** Columns to exclude, applicable if `include` is not specified */
  exclude?: string[];

  /** Override the chart spec for certain columns. If the override is set to `null` the column will be skipped */
  override?: Record<string, BuiltinChartSpec | null>;

  /** Set to false to disable the instances table, or an object to override spec properties */
  table?: boolean | Partial<InstancesSpec>;

  /** Set to false to disable the embedding view, or an object to override spec properties */
  embedding?: boolean | Partial<EmbeddingSpec>;
}

interface ProjectionInput {
  x: string;
  y: string;
  text?: string;
  isGis?: boolean;
  image?: string;
  importance?: string;
  bounds?: { x: [number, number]; y: [number, number] } | null;
  /** Names of pre-computed u16-quantised x/y columns. When set, the
   *  scatter wire query becomes a pure scan — see `EmbeddingViewMosaic`. */
  precomputed?: { x_u16: string; y_u16: string; y_is_mercator?: boolean } | null;
  /** Viewport defaults from server-side bbox knowledge. See
   *  `EmbeddingSpec.data.viewportHint`. */
  viewportHint?: {
    centerX: number;
    centerY: number;
    rangeX: number;
    rangeY: number;
    rowCount?: number;
    skipDeferredRefine?: boolean;
  } | null;
}

/** Synchronous chart specs that need no DB queries: embedding, predicates,
 *  and the instances table. Mount these *first* so the embedding view's own
 *  density+scatter queries can race in parallel with the slower
 *  per-column distinct-count discovery. On 75 M-row datasets, the wide
 *  APPROX_COUNT_DISTINCT batch takes 5–10 s, and gating the embedding mount
 *  on it shifted first-paint by exactly that much. */
export function defaultPrimaryCharts(options: {
  projection?: ProjectionInput;
  config?: DefaultChartsConfig;
}): BuiltinChartSpec[] {
  let { projection } = options;
  let config = options.config ?? {};
  let charts: BuiltinChartSpec[] = [];

  if (projection != null && config.embedding !== false) {
    let spec: EmbeddingSpec = {
      type: "embedding",
      title: "Embedding",
      data: {
        x: projection.x,
        y: projection.y,
        text: projection.text,
        isGis: projection.isGis,
        image: projection.image,
        importance: projection.importance,
        bounds: projection.bounds,
        precomputed: projection.precomputed,
        viewportHint: projection.viewportHint,
      },
    };
    if (typeof config.embedding == "object") {
      spec = { ...spec, ...config.embedding };
    }
    charts.push(spec);
  }

  charts.push({ type: "predicates", title: "SQL Predicates" });

  if (config.table !== false) {
    let spec: InstancesSpec = {
      type: "instances",
      title: "Instances",
    };
    if (typeof config.table == "object") {
      spec = { ...spec, ...config.table };
    }
    charts.push(spec);
  }

  return charts;
}

/** Async per-column chart discovery. Issues one fused
 *  ``APPROX_COUNT_DISTINCT`` query and returns a histogram or count-plot
 *  per qualifying column. Caller is expected to merge these into the chart
 *  set after the primary charts have already mounted. */
export async function defaultColumnCharts(options: {
  coordinator: Coordinator;
  table: string;
  columns?: ColumnDesc[];
  projection?: ProjectionInput;
  config?: DefaultChartsConfig;
}): Promise<BuiltinChartSpec[]> {
  let { coordinator, table, projection } = options;
  let config = options.config ?? {};
  let exclude = [...(config.exclude ?? [])];
  if (projection != null && config.embedding !== false) {
    exclude.push(projection.x);
    exclude.push(projection.y);
    if (projection.text) {
      exclude.push(projection.text);
    }
  }

  let columns =
    options.columns ??
    (await columnDescriptions(coordinator, table)).filter((x) => !x.name.startsWith("__"));

  let charts: BuiltinChartSpec[] = [];

  let candidates = columns.filter((item) => {
    if (item.jsType == null) return false;
    if (item.jsType === "string[]") return false;
    if (config.include != undefined && config.include.indexOf(item.name) < 0) return false;
    if (exclude.indexOf(item.name) >= 0) return false;
    if (config.override?.[item.name] !== undefined) return false;
    return true;
  });
  let distinctMap = await distinctCountBatch(coordinator, table, candidates.map((c) => c.name));

  for (let item of columns) {
    if (item.jsType == null) {
      continue;
    }

    // If include is specified, only process columns in the include list.
    if (config.include != undefined && config.include.indexOf(item.name) < 0) {
      continue;
    }
    // If exclude is specified, skip excluded columns.
    if (exclude.indexOf(item.name) >= 0) {
      continue;
    }

    // If we have an override, use the override directly.
    let override = config.override?.[item.name];
    if (override !== undefined) {
      if (override !== null) {
        charts.push(override);
      }
      continue;
    }

    let distinct = distinctMap.get(item.name) ?? 2;
    // Skip the column if there's only a single unique value.
    if (distinct <= 1) {
      continue;
    }

    switch (item.jsType) {
      case "string": {
        if (distinct <= 1000) {
          charts.push({
            type: "count-plot",
            title: item.name,
            data: { field: item.name },
          });
        }
        break;
      }
      case "string[]": {
        charts.push({
          type: "count-plot",
          title: item.name,
          data: { field: item.name, isList: true },
        });
        break;
      }
      case "number":
      case "Date": {
        if (distinct <= 10) {
          charts.push({
            type: "count-plot",
            title: item.name,
            data: { field: item.name },
          });
        } else {
          charts.push(histogramSpec(item.name));
        }
        break;
      }
    }
  }
  return charts;
}

/** Backwards-compatible single-call API. Prefer the two-phase
 *  ``defaultPrimaryCharts`` + ``defaultColumnCharts`` for interactive UIs
 *  so the embedding scatter can race the column distinct-count discovery. */
export async function defaultCharts(options: {
  coordinator: Coordinator;
  table: string;
  id: string;
  projection?: ProjectionInput;
  config?: DefaultChartsConfig;
}): Promise<BuiltinChartSpec[]> {
  const primary = defaultPrimaryCharts({
    projection: options.projection,
    config: options.config,
  });
  const cols = await defaultColumnCharts({
    coordinator: options.coordinator,
    table: options.table,
    projection: options.projection,
    config: options.config,
  });
  return [...primary, ...cols];
}

export function histogramSpec(field: string, groupField?: string): ChartSpec {
  return {
    title: field,
    layers: [
      {
        mark: "bar",
        style: { fillColor: "$markColorFade" },
        encoding: {
          x: { field: field },
          y: { aggregate: "count" },
        },
      },
      {
        mark: "bar",
        filter: "$filter",
        encoding: {
          x: { field: field },
          y: { aggregate: "count" },
          ...(groupField ? { color: { field: groupField } } : {}),
        },
      },
    ],
    selection: { brush: { encoding: "x" } },
    widgets: [
      { type: "scale.type", channel: "x" },
      { type: "encoding.normalize", attribute: "y", layer: [0, 1], options: ["x"] },
    ],
  };
}
