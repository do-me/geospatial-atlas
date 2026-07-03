<!-- Copyright (c) 2025 Apple Inc. Licensed under MIT License. -->
<script module lang="ts">
  import { maxDensityModeCategories, type DataPoint, type ViewportState } from "@embedding-atlas/component";
  import { MosaicClient, type Coordinator } from "@uwdata/mosaic-core";
  import * as SQL from "@uwdata/mosaic-sql";

  import Overlay from "./Overlay.svelte";
  import Tooltip from "./Tooltip.svelte";

  import { formatColumnType, type EmbeddingLegend } from "../../utils/database.js";
  import { createCustomComponentClass } from "./custom_components.js";

  async function defaultViewportScale(coordinator: Coordinator, table: string, x: string, y: string): Promise<number> {
    let { stdX, stdY } = (
      await coordinator.query(
        SQL.Query.from(table).select({
          stdX: SQL.sql`STDDEV(${SQL.column(x)})::FLOAT`,
          stdY: SQL.sql`STDDEV(${SQL.column(y)})::FLOAT`,
        }),
      )
    ).get(0);
    let scale = 1.0 / (Math.max(stdX, stdY, 1e-3) * 3);
    return scale;
  }

  const CustomTooltip = createCustomComponentClass(Tooltip);
  const CustomOverlay = createCustomComponentClass(Overlay);
</script>

<script lang="ts">
  import { EmbeddingViewMosaic } from "@embedding-atlas/component/svelte";
  import { cubicOut } from "svelte/easing";

  import Button from "../../widgets/Button.svelte";
  import Input from "../../widgets/Input.svelte";
  import PopupButton from "../../widgets/PopupButton.svelte";
  import Select from "../../widgets/Select.svelte";
  import Slider from "../../widgets/Slider.svelte";
  import Switch from "../../widgets/Switch.svelte";
  import Legend from "./Legend.svelte";

  import { IconSettings } from "../../assets/icons.js";
  import { isolatedWritable } from "../../utils/store.js";
  import type { ChartViewProps, RowID } from "../chart.js";
  import { resolveChartTheme } from "../common/theme.js";
  import { makeCategoryColumn } from "./category_column.js";
  import type { EmbeddingSpec, EmbeddingState } from "./types.js";
  import { interpolateViewport } from "./viewport_animation.js";

  const maxCategories = Math.min(20, maxDensityModeCategories());
  const defaultMinimumDensity = 1 / 16;
  // Infinity = render every point by default. The renderer reads null / Infinity
  // / NaN / negative as "downsampling disabled" and takes the full-draw path.
  // The slider fallback uses Math.min(default, totalPointCount), so display
  // still resolves to totalPointCount once it loads (shows "All").
  const defaultDownsampleMaxPoints = Infinity;
  const minDownsampleMaxPoints = 50000;
  const defaultDownsampleMaxPointsInteractive = 0;
  const minDownsampleMaxPointsInteractive = 0;
  const defaultMapStyle = "https://tiles.openfreemap.org/styles/liberty";

  // Match-Lines (matcher-eval) pair types, colors mirror the renderer.
  const MATCH_LINE_TYPES = [
    { key: "candidate->baseline", label: "Candidate → Baseline", color: "#2ca02c" },
    { key: "candidate->candidate", label: "Candidate → Candidate", color: "#98df8a" },
    { key: "baseline->baseline", label: "Baseline → Baseline", color: "#1f77b4" },
  ];

  // Cluster filtering (matcher-eval): clicking a point publishes a
  // ``cluster_id IN (…)`` clause to the global cross-filter so only that
  // point's cluster stays visible (and, since the Match-Lines client reads the
  // same filter, only that cluster's lines draw). Clicking empty map clears it.
  const CLUSTER_FILTER_COLUMN = "cluster_id";

  let {
    context,
    width,
    height,
    spec,
    state: chartState,
    onStateChange,
    onSpecChange,
  }: ChartViewProps<EmbeddingSpec, EmbeddingState> = $props();

  // svelte-ignore state_referenced_locally
  let { colorScheme, columnStyles, searchResult, theme: themeConfig } = context;

  let theme = $derived(resolveChartTheme($colorScheme, $themeConfig));

  // svelte-ignore state_referenced_locally
  let highlightStore = isolatedWritable(context.highlight);

  let categoryColumn = $derived(spec.data.category);

  let categoryLegend: EmbeddingLegend | null = $state.raw(null);
  let totalPointCount: number | null = $state.raw(null);

  // Query total point count for render limit slider
  $effect.pre(() => {
    context.coordinator
      .query(SQL.Query.from(context.table).select({ count: SQL.sql`COUNT(*)::INT` }))
      .then((result: any) => {
        totalPointCount = result.get(0).count;
      });
  });

  let tooltip = $state.raw<DataPoint | null>(null);
  let selection = $state.raw<DataPoint[] | null>(null);
  let overlayProps = $state.raw<{ center: DataPoint | null; points: DataPoint[] } | null>(null);

  let mapStyleInput = $state(spec.mapStyle ?? defaultMapStyle);
  $effect(() => {
    mapStyleInput = spec.mapStyle ?? defaultMapStyle;
  });

  // Match-Lines visibility (matcher-eval). Master toggle + per-pair-type.
  let matchLinesEnabled = $state(true);
  let visibleMatchLineTypes = $state<string[]>(MATCH_LINE_TYPES.map((t) => t.key));
  let effectiveVisibleLineTypes = $derived(matchLinesEnabled ? visibleMatchLineTypes : []);

  // Cluster filtering — only in matcher-eval views (lines present) that carry a
  // ``cluster_id`` column. Its own source identity in the cross-filter so the
  // clause replaces itself on each click; the scatter, lines, and table clients
  // are distinct sources, so they all filter to it.
  const clusterFilterClient = new MosaicClient();
  let clusterFilterEnabled = $derived(
    spec.data.lines != null && context.columns.some((c) => c.name === CLUSTER_FILTER_COLUMN),
  );

  // Survivor ring — only in matcher-eval views (lines present) that carry a
  // ``survivor`` column: points with survivor=1 get a light-red outer ring.
  let survivorColumn = $derived(
    spec.data.lines != null && context.columns.some((c) => c.name === "survivor") ? "survivor" : null,
  );
  // The cluster id(s) currently filtered to (null = show all). Held as state so
  // the actual context.filter mutation happens in the reactive effect below, at
  // a clean tick — NOT synchronously inside onSelection. Updating the Selection
  // re-entrantly from that callback (it fires during the data client's
  // queryResult cascade) made the table miss the first click; publishing from a
  // $effect.pre is the same pattern Legend.svelte uses, and it is reliable.
  let clusterFilterIds = $state.raw<any[] | null>(null);

  function publishClusterClause(predicate: any, value: any) {
    context.filter.update({
      source: clusterFilterClient,
      clients: new Set([clusterFilterClient]),
      predicate,
      value,
    });
  }

  // Map click → cluster filter target. ``points`` distinguishes:
  //   - ``null``  → a programmatic reset. The data client clears the highlight
  //     after EVERY re-query, including the one our own filter triggers, so a
  //     ``null`` here is NOT a user action. Ignore it — otherwise the filter
  //     would clear itself the instant it took effect (points flash then return).
  //   - ``[]``    → the user clicked empty map. Clear the filter (show all).
  //   - [p, …]    → the user clicked point(s); filter to their cluster(s).
  //     Multi-select (shift/⌘-click) unions the clusters. Points with no
  //     cluster_id (unmatched) leave the current target unchanged.
  function updateClusterFilter(points: DataPoint[] | null) {
    if (!clusterFilterEnabled || points == null) {
      return;
    }
    if (points.length == 0) {
      clusterFilterIds = null;
      return;
    }
    let ids = points.map((p) => p?.fields?.[CLUSTER_FILTER_COLUMN]).filter((v) => v != null);
    let distinct = Array.from(new Set(ids));
    if (distinct.length > 0) {
      clusterFilterIds = distinct;
    }
  }

  // Mirror clusterFilterIds onto context.filter reactively (re-runs only when the
  // target changes; updating the same source replaces its clause in place).
  $effect.pre(() => {
    if (!clusterFilterEnabled) {
      return;
    }
    let ids = clusterFilterIds;
    if (ids != null && ids.length > 0) {
      publishClusterClause(
        SQL.isIn(
          SQL.column(CLUSTER_FILTER_COLUMN),
          ids.map((v) => SQL.literal(v)),
        ),
        ids,
      );
    } else {
      publishClusterClause(null, null);
    }
  });

  // Drop the clause on unmount so a stale cluster filter doesn't outlive the view.
  $effect(() => () => publishClusterClause(null, null));

  // Update the category mapping and legend.
  $effect.pre(() => {
    let promise = context.cache.value(`embedding/category/${categoryColumn}`, () =>
      makeCategoryColumn(context.coordinator, context.table, categoryColumn, theme),
    );
    promise.then((v) => {
      categoryLegend = v;
      if ((categoryLegend?.legend.length ?? 0) > maxCategories) {
        onSpecChange({ mode: "points" });
      }
    });
  });

  $effect.pre(() => {
    let isOnMount = true;
    let previousValue: RowID[] | null = null;
    return highlightStore.subscribe((v) => {
      selection = v;

      // Don't animate immediately on mount.
      if (isOnMount) {
        isOnMount = false;
        previousValue = v;
        return;
      }
      // Animate when a single new point is added.
      let newIDs = v ?? [];
      let oldIDs = previousValue ?? [];
      let enteringIDs = newIDs.filter((x) => oldIDs.indexOf(x) < 0);
      if (enteringIDs.length == 1) {
        animateToPoint(enteringIDs[0]);
      }
      if (tooltip != null && newIDs.indexOf(tooltip) < 0) {
        tooltip = null;
      }
      previousValue = v;
    });
  });

  $effect.pre(() =>
    searchResult.subscribe(async (result) => {
      if (result == null || result.ids.length == 0) {
        overlayProps = null;
        return;
      }
      let centerId: RowID | null = null;
      if (result.mode == "neighbors") {
        centerId = result.query;
      }
      let r = Array.from(
        await context.coordinator.query(
          SQL.Query.from(context.table)
            .select({ identifier: SQL.column(context.id), x: SQL.column(spec.data.x), y: SQL.column(spec.data.y) })
            .where(
              SQL.isIn(
                context.id,
                result.ids.concat(centerId != null ? [centerId] : []).map((x) => SQL.literal(x)),
              ),
            ),
        ),
      ) as DataPoint[];
      overlayProps = {
        center: r.filter((p) => p.identifier === centerId)[0] ?? null,
        points: r.filter((p) => p.identifier !== centerId),
      };
    }),
  );

  async function animateToPoint(identifier: RowID): Promise<void> {
    let defaultScale = await context.cache.value(`embedding/default-viewport-scale/${spec.data.x},${spec.data.y}`, () =>
      defaultViewportScale(context.coordinator, context.table, spec.data.x, spec.data.y),
    );
    let scale = defaultScale * 2;
    // Query the x, y location.
    let result = await context.coordinator.query(
      SQL.Query.from(context.table)
        .select({
          x: SQL.column(spec.data.x),
          y: SQL.column(spec.data.y),
        })
        .where(SQL.eq(SQL.column(context.id), SQL.literal(identifier))),
    );
    let { x, y } = result.get(0) as { x: number; y: number };
    // Start animation and show tooltip.
    startViewportAnimation({ x: x, y: y, scale: scale });
    tooltip = identifier;
  }

  let currentViewportAnimation: number | null;
  let animatingViewport = $state.raw<ViewportState | null>(null);
  function startViewportAnimation(newState: ViewportState) {
    tooltip = null;
    let start = animatingViewport ?? chartState.viewport;
    if (start == null) {
      onStateChange({ viewport: newState });
      return;
    }
    animatingViewport = start;
    let duration = 800;
    let t0 = new Date().getTime();
    let callback = () => {
      let t = (new Date().getTime() - t0) / duration;
      if (t > 1) {
        t = 1;
      }
      animatingViewport = interpolateViewport(start, newState, cubicOut(t));
      if (t < 1) {
        currentViewportAnimation = requestAnimationFrame(callback);
      } else {
        onStateChange({ viewport: animatingViewport });
      }
    };
    if (currentViewportAnimation) {
      cancelAnimationFrame(currentViewportAnimation);
    }
    currentViewportAnimation = requestAnimationFrame(callback);
  }
</script>

<div class="relative">
  <EmbeddingViewMosaic
    width={width}
    height={height}
    coordinator={context.coordinator}
    table={context.table}
    filter={context.filter}
    rangeSelection={context.filter}
    identifier={context.id}
    x={spec.data.x}
    y={spec.data.y}
    bounds={spec.data.bounds}
    precomputed={spec.data.precomputed}
    viewportHint={spec.data.viewportHint}
    text={spec.data.text}
    image={spec.data.image}
    importance={spec.data.importance}
    category={categoryLegend?.indexColumn}
    survivor={survivorColumn}
    categoryColors={categoryLegend?.legend.map((x) => x.color) ?? [theme.embeddingColor]}
    lines={spec.data.lines}
    linesVisibleTypes={spec.data.lines != null ? effectiveVisibleLineTypes : null}
    config={{
      colorScheme: $colorScheme,
      ...context.embeddingViewConfig,
      mode: spec.mode ?? "points",
      isGis: spec.data.isGis,
      mapStyle: spec.mapStyle,
      ...(spec.minimumDensity != null ? { minimumDensity: spec.minimumDensity } : {}),
      pointSize: spec.pointSize ?? 2,
      survivorRingWidth: spec.survivorRingWidth ?? 0.1,
      showPointLabels: spec.showPointLabels ?? false,
      downsampleMaxPoints: spec.downsampleMaxPoints ?? defaultDownsampleMaxPoints,
      downsampleMaxPointsInteractive: spec.downsampleMaxPointsInteractive ?? defaultDownsampleMaxPointsInteractive,
    }}
    labels={context.embeddingViewLabels}
    cache={context.persistentCache}
    additionalFields={Object.fromEntries(context.columns.map((c) => [c.name, c.name]))}
    customTooltip={{
      class: CustomTooltip,
      props: {
        darkMode: $colorScheme,
        columnStyles: $columnStyles,
        onNearestNeighborSearch:
          (context.searchModes ?? []).indexOf("neighbors") >= 0 ? (id: any) => context.search?.(id, "neighbors") : null,
      },
    }}
    customOverlay={{
      class: CustomOverlay,
      props: { ...(overlayProps ?? { points: [], center: null }) },
    }}
    viewportState={animatingViewport ?? chartState.viewport}
    onViewportState={(v) => onStateChange({ viewport: v })}
    rangeSelectionValue={chartState.brush}
    onRangeSelection={(v) => onStateChange({ brush: v ?? undefined })}
    tooltip={spec.showPointLabels ? null : tooltip}
    onTooltip={(v) => {
      // When Point Labels is on, the name is shown as a standalone label above
      // each dot — suppress the hover popup so nothing else appears on hover.
      if (spec.showPointLabels) {
        return;
      }
      tooltip = v;
    }}
    selection={selection}
    onSelection={(points) => {
      selection = points;
      highlightStore.set(points?.map((p) => p.identifier) ?? null);
      updateClusterFilter(points);
    }}
  />
  <div class="absolute top-0 left-0 right-0 flex flex-wrap justify-between items-start pointer-events-none z-10">
    {#if categoryLegend != null}
      <div
        class="flex-none m-2 p-2 rounded-md bg-slate-100/75 dark:bg-slate-800/75 backdrop-blur-sm pointer-events-auto order-3"
      >
        <Legend
          context={context}
          spec={{ items: categoryLegend.legend }}
          state={chartState.legend ?? {}}
          mode="view"
          onSpecChange={() => {}}
          onStateChange={(update, mode) => {
            onStateChange({ legend: update });
          }}
        />
      </div>
    {/if}
    <div
      class="flex-none p-2 rounded-ss-md rounded-ee-md bg-white/75 dark:bg-black/75 backdrop-blur-sm flex items-center gap-2 pointer-events-auto order-1"
    >
      <Select
        class="max-w-64"
        label="Color"
        value={categoryColumn}
        onChange={(v) => onSpecChange({ data: { ...spec.data, category: v } })}
        options={[
          { value: undefined, label: "--" },
          ...context.columns
            .filter((c) => c.jsType == "string" || c.jsType == "number" || c.jsType == "Date")
            .map((c) => ({ value: c.name, label: `${c.name} (${formatColumnType(c.type)})` })),
        ]}
      />
      <PopupButton icon={IconSettings} title="Options">
        <div class="flex flex-col gap-2 w-64">
          {#if spec.data.isGis}
            <div class="flex items-center justify-between">
              <div class="text-slate-500 dark:text-slate-400 select-none">Basemap Style</div>
              <Switch
                label="Show"
                value={spec.mapStyle !== null}
                onChange={(v) => {
                  if (v) {
                    onSpecChange({ mapStyle: mapStyleInput || defaultMapStyle });
                  } else {
                    onSpecChange({ mapStyle: null });
                  }
                }}
              />
            </div>
            <div class="flex gap-2 items-center">
              <Input
                className="w-full"
                disabled={spec.mapStyle === null}
                bind:value={mapStyleInput}
                onEnter={() => onSpecChange({ mapStyle: mapStyleInput })}
              />
              <Button
                label="Set"
                disabled={spec.mapStyle === null}
                onClick={() => onSpecChange({ mapStyle: mapStyleInput })}
              />
            </div>
          {/if}
          {#if spec.data.lines != null}
            <div class="flex items-center justify-between">
              <div class="text-slate-500 dark:text-slate-400 select-none">Match Lines</div>
              <Switch label="Show" value={matchLinesEnabled} onChange={(v) => (matchLinesEnabled = v)} />
            </div>
            {#each MATCH_LINE_TYPES as t}
              <div class="flex items-center justify-between" class:opacity-50={!matchLinesEnabled}>
                <div class="flex items-center gap-2">
                  <span style="display:inline-block;width:14px;height:3px;border-radius:2px;background:{t.color}"
                  ></span>
                  <span class="select-none text-sm">{t.label}</span>
                </div>
                <Switch
                  value={visibleMatchLineTypes.includes(t.key)}
                  onChange={(v) => {
                    visibleMatchLineTypes = v
                      ? [...visibleMatchLineTypes, t.key]
                      : visibleMatchLineTypes.filter((x) => x !== t.key);
                  }}
                />
              </div>
            {/each}
          {/if}
          <div class="text-slate-500 dark:text-slate-400 select-none">Display Mode</div>
          <div class="flex gap-2 items-center">
            <Select
              value={spec.mode ?? "points"}
              onChange={(v) => onSpecChange({ mode: v })}
              disabled={categoryLegend != null && categoryLegend.legend.length > maxCategories}
              options={[
                { value: "points", label: "Points" },
                { value: "density", label: "Density" },
              ]}
            />
            {#if (spec.mode ?? "points") == "density"}
              <Slider
                bind:value={
                  () => Math.log((spec.minimumDensity ?? defaultMinimumDensity) / defaultMinimumDensity),
                  (v) => onSpecChange({ minimumDensity: defaultMinimumDensity * Math.exp(v) })
                }
                min={-4}
                max={4}
                step={0.05}
              />
            {/if}
          </div>
          <div class="text-slate-500 dark:text-slate-400 select-none">Point Size</div>
          <div class="flex gap-2 items-center">
            <Slider
              bind:value={() => spec.pointSize ?? 2, (v) => onSpecChange({ pointSize: v })}
              min={0.05}
              max={10}
              step={0.05}
            />
            <Button label="Auto" onClick={() => onSpecChange({ pointSize: 2 })} />
          </div>
          {#if survivorColumn != null}
            <div class="text-slate-500 dark:text-slate-400 select-none">Survivor Ring</div>
            <div class="flex gap-2 items-center">
              <Slider
                bind:value={() => spec.survivorRingWidth ?? 0.1, (v) => onSpecChange({ survivorRingWidth: v })}
                min={0.1}
                max={1}
                step={0.05}
              />
              <Button label="Auto" onClick={() => onSpecChange({ survivorRingWidth: 0.1 })} />
            </div>
          {/if}
          {#if spec.data.text != null}
            <div class="flex items-center justify-between">
              <div class="text-slate-500 dark:text-slate-400 select-none">Point Labels</div>
              <Switch
                label="Show"
                value={spec.showPointLabels ?? false}
                onChange={(v) => onSpecChange({ showPointLabels: v })}
              />
            </div>
          {/if}
          {#if totalPointCount != null && totalPointCount > minDownsampleMaxPoints}
            {@const effectiveLimit = spec.downsampleMaxPoints ?? Math.min(defaultDownsampleMaxPoints, totalPointCount)}
            {@const isMaxed = effectiveLimit >= totalPointCount}
            <div class="text-slate-500 dark:text-slate-400 select-none">
              Max Points: {isMaxed
                ? "All"
                : effectiveLimit >= 1000000
                  ? (effectiveLimit / 1000000).toFixed(1) + "M"
                  : (effectiveLimit / 1000).toFixed(0) + "K"}
              {#if !isMaxed}
                <span class="text-slate-400 dark:text-slate-500"
                  >/ {totalPointCount >= 1000000
                    ? (totalPointCount / 1000000).toFixed(1) + "M"
                    : (totalPointCount / 1000).toFixed(0) + "K"}</span
                >
              {/if}
            </div>
            <div class="flex gap-2 items-center">
              <Slider
                bind:value={
                  () =>
                    spec.downsampleMaxPoints ??
                    Math.min(defaultDownsampleMaxPoints, totalPointCount ?? defaultDownsampleMaxPoints),
                  (v) => onSpecChange({ downsampleMaxPoints: v })
                }
                min={minDownsampleMaxPoints}
                max={totalPointCount}
                step={Math.max(10000, Math.floor(totalPointCount / 100 / 10000) * 10000)}
              />
            </div>
            {@const interactiveLimit = spec.downsampleMaxPointsInteractive ?? defaultDownsampleMaxPointsInteractive}
            <div class="text-slate-500 dark:text-slate-400 select-none">
              While Zooming: {interactiveLimit >= 1000000
                ? (interactiveLimit / 1000000).toFixed(2) + "M"
                : (interactiveLimit / 1000).toFixed(0) + "K"}
            </div>
            <div class="flex gap-2 items-center">
              <Slider
                bind:value={
                  () =>
                    Math.max(
                      minDownsampleMaxPointsInteractive,
                      spec.downsampleMaxPointsInteractive ?? defaultDownsampleMaxPointsInteractive,
                    ),
                  (v) =>
                    onSpecChange({
                      downsampleMaxPointsInteractive: Math.max(minDownsampleMaxPointsInteractive, v),
                    })
                }
                min={minDownsampleMaxPointsInteractive}
                max={totalPointCount}
                step={1000}
              />
              <Button
                label="Reset"
                onClick={() => onSpecChange({ downsampleMaxPointsInteractive: defaultDownsampleMaxPointsInteractive })}
              />
            </div>
          {/if}
        </div>
      </PopupButton>
    </div>
  </div>
</div>
