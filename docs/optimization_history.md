# Optimization history — scaling Embedding Atlas from 4 M to 322 M points

A field journal of the non-obvious findings that made 322 M-row geospatial
parquet (eubucco) renderable at interactive frame rates on a single laptop.
Each section documents a bug or bottleneck that consumed hours-to-days of
debugging, the root cause, the fix, and the lesson. Read this before
re-debugging any scaling regression.

The companion document [`PERF-75M.md`](./PERF-75M.md) covers the 4 M → 75 M
chapter (compaction + indirect draw, gesture-only downsample, workgroup-size
sweeps). This file picks up where that one ended and follows the journey to
322 M.

> **Headline finding (saved us most: cost us most):** at the 322 M scale,
> a single OOM is almost never one leak — it is **two or three small leaks
> compounding** to exceed V8's ArrayBuffer pool. Heap snapshots over time
> (not just at the moment of crash) are the only reliable diagnosis.

---

## 1. The wire and the column store

### 1.1 `con.sql(...).fetch_arrow_table()` is 10× faster than `cursor.execute(...).fetch_arrow_table()`

Symptom: cold load of a 4 GB parquet took 90 s in DuckDB, 9 s in `duckdb` CLI
on the same machine.

Root cause: `cursor.execute()` materialises a full result-set buffer
**before** Arrow extraction; the relation API (`con.sql(...)`) streams
straight into Arrow record batches.

Fix: viewer's sidecar uses `con.sql(query).fetch_arrow_table()` exclusively
for any query > 1 M rows.

Lesson: The Python DuckDB API has two paths that look identical and differ
by 10× on large arrow exports. Always use the relation API for big results.

### 1.2 `Response.arrayBuffer()` has a ~2 GB cap (NOT V8's `ArrayBuffer`)

Symptom: at 322 M rows, the `/api/scatter` POST returned 2.1 GB of arrow
zstd-decoded bytes; the browser threw `Failed to fetch` despite plenty of
free memory.

Root cause: Chrome's fetch pipeline imposes a hard ~2 GB ceiling on
`Response.arrayBuffer()` and `.blob()` — the underlying `Body` accumulates
all chunks into one contiguous buffer. The V8 `ArrayBuffer` constructor
itself supports much larger sizes (up to ~16 GB on 64-bit).

Fix: ship a custom `streamingRestConnector` (in `packages/component`) that
reads the `Response.body` ReadableStream chunk-by-chunk and concatenates
into a single `Uint8Array` — bypassing the fetch pipeline ceiling.

Lesson: When you hit a ceiling that "feels like V8 ArrayBuffer," check
**which** ArrayBuffer. Fetch's body accumulator and V8's allocator have
different limits. Don't regress the wire format to dodge the wrong wall.

### 1.3 Split-scatter queries scramble row order without `ORDER BY`

Symptom: at 322 M with split per-axis SELECT (one query for `x`, one for
`y`, joined client-side by row index), points landed at apparently random
positions — looked like a quantisation bug.

Root cause: DuckDB's `preserve_insertion_order=false` (set for memory
pressure under the 322 M cap) re-orders rows across separate SELECTs even
when the source table has a stable physical order.

Fix: every per-axis query in the split-scatter path now ends with
`ORDER BY __row_index__` to lock alignment. Costs ~5 % runtime; rescues
correctness.

Lesson: Any time you split a logical row into two separate queries, you
must serialise the join key explicitly. "Same table, two SELECTs" is
**not** order-stable under DuckDB's parallel exec.

### 1.4 u32 packed quantum at 1.5 cm

Symptom: at city zoom (e.g. Paris 7th arrondissement), the u16-packed
scatter showed a visible 5–10 m grid — the packed quantum was coarser
than building-level detail.

Root cause: u16 over a 360°×170° world bounding box gives ~5 km/quantum at
the equator. Even u16-per-tile would not help: the tile cost dominates the
wire.

Fix: switch to u32 packed columns (`packages/component/.../scatter_u32.ts`).
At 360° world span, u32 quantum is ~8 mm — well below sub-pixel even at
city zoom. The wire grew from 4 bytes/point (u16+u16) to 8, but zstd halves
that and the streaming connector handles the larger total.

Lesson: u16 is the wrong precision for any geospatial dataset that wants
city-zoom rendering. Use u32 from day one, even though it doubles the wire.

### 1.5 `arrow_cache.put()` silently drops buffers larger than `max_bytes`

Symptom: prewarming the eubucco scatter into the sidecar arrow_cache
appeared to succeed (no error); subsequent requests refetched from
parquet anyway.

Root cause: `arrow_cache.put(buf)` returns silently when `len(buf) > max_bytes`
(default 1 GB). The 322 M scatter is ~2 GB.

Fix: a new `put_pinned()` variant bypasses the size cap for prewarm
buffers. The cap is preserved for runtime put() to protect against
runaway caching.

Lesson: silent failures in caches are insidious. Audit any `put()` that
returns without raising — at scale it will hide bugs.

---

## 2. Browser memory and the Mosaic Coordinator (the recent days-long saga)

This is the section that took the most time. The presenting symptom was
"color-by + filter clicks crash the renderer at 322 M with `Array buffer
allocation failed`." It turned out to be **three independent leaks**
compounding. Each alone is survivable; together they exhaust V8's
~16 GB ArrayBuffer pool the moment a category filter triggers a re-fetch.

### 2.1 Leak A — Mosaic's `QueryManager` LRU pins every Arrow Table for 3 hours

Symptom: V8 heap snapshot taken 4 minutes into a session showed 4093 small
Arrow `Vector` chunks totalling **8+ GiB** retained, all rooted at
`coordinator.manager.clientCache`.

Root cause: `@uwdata/mosaic-core` defaults its `QueryManager` cache to
`lruCache(max=1000, ttl=3h)`. Every successful query result Table is
pinned for 3 hours — including the 2.5 GB scatter result. A few filter
toggles enqueue new scatters and never evict the old ones (cache is far
below `max=1000`).

Fix: `coordinator.manager.cache(false)` in **both** `Viewer.svelte` and
`FileViewer.svelte`. Histogram and count queries are tiny — re-fetching
on filter change costs <50 ms; pinning a 2.5 GB scatter result for 3 h
costs the entire pool.

Lesson: read the upstream library's cache defaults before scaling.
Mosaic was designed for ≤10 M datasets where 1000 × small-result is fine.
At 322 M, "1000 × default" is "~2.5 TB hypothetical" and the LRU never
gets a chance to evict.

### 2.2 Leak B — the WebGPU renderer pinned `lastXPacked`/`lastYPacked` past consumer null-out

Symptom: heap snapshot showed **4 × 627 MiB Uint32Arrays** retained via
`EmbeddingRendererWebGPU.lastXPacked` and `lastYPacked` — even after the
consumer (EmbeddingViewMosaic) had set `xPacked = null`.

Root cause: `renderer.setProps({xPacked: null})` left the previous
identity refs on the renderer instance; they were only reassigned when
the next non-null `xPacked` arrived. Between filter clicks the old
1.3 GB+1.3 GB stayed alive alongside the new 1.3 GB allocation.

Fix (`packages/component/src/lib/webgpu_renderer/renderer.ts`):
1. In `setProps`, when `usePacked === false`, null out `this.lastXPacked`,
   `lastYPacked`, `lastCoordsBoundsX`, `lastCoordsBoundsY` immediately.
2. In `maybeRunUnpack`, if `runX`/`runY` will trigger a fresh unpack, drop
   the **previous** identity ref before the unpack chain awaits — the new
   `xPacked` param is already in `this.props`, so identity is restored
   when the await resolves.

Lesson: any "remember last value to detect identity change" pattern needs
an explicit clear path. The pattern is correct for fast equality checks;
it must release on null-input or the clear branch.

### 2.3 Leak C — `queryResult` allocated the new `toArray()` in the same microtask that nulled the old buffers

Symptom: even with leaks A and B fixed, a 158 M filter result still OOM'd
at `data.getChild("x").toArray()`.

Root cause: Svelte 5's `$state.raw` writes batch within a synchronous
callback. The flush propagating `xPackedData = null` into the renderer
only happens at the **next** microtask boundary. We were calling
`toArray()` in the same callback, so when V8 tried to allocate the new
1.3 GB Uint32Array, the old 1.3 GB was still ref-pinned by the renderer's
`setProps` not having run yet.

Fix (`packages/component/.../EmbeddingViewMosaic.svelte`):
1. Make `queryResult` async.
2. After nulling `xPackedData`/`yPackedData`/`xData`/`yData`/`categoryData`,
   `await new Promise(r => setTimeout(r, 0))` to let Svelte flush + the
   renderer's `setProps` consume the nulls.
3. Gated by `numRowsHint > 50_000_000`, force a major GC via
   `globalThis.gc()` to compact the heap before the next allocation.
   Requires Electron's `--expose-gc` js-flag.

Lesson: when you null a ref to free memory, the caller must yield control
**at least once** before allocating the replacement. Svelte 5's batched
flush + downstream effect chains need a macrotask boundary, not just a
microtask.

### 2.4 Electron V8 budget: 16 GB + `--expose-gc`

Symptom: after the three leaks above were fixed, a session that had
accumulated ~9 minutes of sidebar interactions still tripped a transient
allocation failure mid-scatter.

Root cause: V8's old-generation default is ~4 GB on 64-bit Electron. Even
12 GB (our prior setting) was tight against fragmentation: peak active
heap at 322 M during a re-fetch is ~5.8 GB, and accumulated retained Arrow
pinning + sidebar state pushed us to 9–10 GB during long sessions.

Fix (`apps/desktop/electron/main.ts`):
```ts
app.commandLine.appendSwitch("js-flags", "--max-old-space-size=16384 --expose-gc");
```

The `--expose-gc` is the second half: without it, the
`if (numRows > 50M) globalThis.gc()` in `queryResult` is a no-op.

Lesson: V8 *budget* is not resident memory — pages are only committed when
touched. There is no penalty for a generous old-generation cap; the
penalty is only paid when fragmentation forces a major GC. Be generous.

### 2.5 Hardcoded `categoryCount: 1` poisoned WGSL when `categoryColors` was present

Symptom: turning on a `c` (category) column made the canvas render
entirely blue at 322 M — the category buffer was ignored.

Root cause: `EmbeddingViewMosaic.svelte` line 752 hardcoded
`categoryCount={categoryCount}` (always 1) into the viewportHint mock
even when the user had selected `categoryColors.length > 1`. The
downstream WGSL compute uniform `category_count` was therefore 1, and
the shader's `category_idx % category_count` clamp degenerated all
points to category 0.

Fix:
```svelte
categoryCount={categoryColors != null && categoryColors.length > 1
  ? categoryColors.length
  : categoryCount}
```

Lesson: when a value is "1 by default but overridden by another prop,"
write the override at the same site as the default. Don't trust a
disconnected setter to update a uniform that ships through a deep chain.

---

## 3. The WebGPU renderer

### 3.0 Spatial-sort the table at background-materialise time (tooltip 10 s → 50 ms)

Symptom: at 322 M, hovering over a point to see the tooltip stalled the
UI for 10+ seconds. The query was a simple `WHERE x BETWEEN px-r AND
px+r AND y BETWEEN py-r AND py+r LIMIT 1`, which should be a row-group
scan with predicate pushdown.

Root cause: parquet was read in **physical row order** (typically
ingestion order — not spatial). DuckDB's per-row-group min/max stats on
x/y therefore covered nearly the entire world bbox per group, so the
pushdown filter could not prune any group. The 322 M-row scan ran in
full for every tooltip.

Fix (`packages/backend/embedding_atlas/fast_load.py:594`): when
the loader's background `CREATE TABLE AS SELECT *` runs (the
view → table promotion that happens on first ALTER), append
`ORDER BY <quant_x_col>, <quant_y_col>` using the **precomputed u32
quantised columns** as the sort key. After the sort, row-group min/max
on x/y are tight enough that >99 % of groups prune for any small radius.

Result: tooltip latency at 322 M drops from ~10 s to ~50 ms.

Lesson: predicate pushdown is only as good as the row-group statistics.
For any column that will be filtered by a small range at query time,
sort on that column at write time. The cost (one extra ORDER BY in
materialise) is paid once; the win is paid on every tooltip and
per-cell histogram.

### 3.1 Metal's 5 s `MTLCommandBuffer` watchdog kills 322 M draw passes

Symptom: a single panning gesture at 322 M caused the Metal device to
reset (Chromium logs: `MTLCommandBuffer execution failed:
INTERNAL_ERROR — possibly due to MTLCommandBuffer execution time out`).
The page was permanently broken until reload.

Root cause: macOS Metal enforces a 5-second hard timeout per
`MTLCommandBuffer`. At 322 M instances, a single `drawPoints` pass over
the full set takes 6–8 s on M-series.

Fix (`packages/component/src/lib/webgpu_renderer/draw_points.ts` +
`renderer.ts`): chunk the draw into N sub-dispatches, each capped at
~64 M instances; submit each as a separate command buffer with its own
fence. WG-local atomic reduction in `viewport_cull` keeps the per-chunk
cost flat. Chunked dispatch also splits the downsample passes for the
same watchdog reason.

> CAPPING POINTS OR SAMPLING IS FORBIDDEN. Every point must render at
> world view. The watchdog is a per-buffer limit, not a per-frame limit
> — split the work across buffers, not across the dataset.

Lesson: GPU scheduling on macOS has a watchdog you cannot turn off. If a
single pass might exceed 5 s anywhere in the parameter space, design for
chunking from day one.

### 3.2 `gpuBuffer.destroy()` must defer past in-flight work

Symptom: at 322 M with rapid pan-release, the `MTLDevice` would
intermittently enter a poisoned state — every subsequent compute pass
returned NaN.

Root cause: the renderer was destroying the prior frame's `lastXPacked`/
`lastYPacked` GPU buffers synchronously in `setProps`. If those buffers
were still bound by an in-flight command buffer (likely under
backpressure), Metal silently corrupted the device.

Fix (`packages/component/src/lib/webgpu_renderer/renderer.ts`): wrap every
`gpuBuffer.destroy()` in `device.queue.onSubmittedWorkDone().then(...)`.
Enqueue all destroys; let them resolve only after all currently-submitted
command buffers complete.

Lesson: WebGPU's `destroy()` is fire-and-forget at the JS level but
**not** at the Metal level. Always defer through `onSubmittedWorkDone`
when the buffer might be in flight.

### 3.3 Zoom-out freeze: skip accumulate at huge N + device-lost-resilient backpressure

Symptom: zooming out from city to world at 322 M caused the renderer to
freeze for 10+ seconds.

Root cause: the viewport-cull → accumulate → draw chain was running
**all** passes per frame even at zoom levels where 99 % of points project
to the same pixel. The accumulate pass was the bottleneck — it allocates
a per-bin atomic counter array sized to the viewport.

Fix (`packages/component/.../renderer.ts`): skip the accumulate pass when
`pointCount > 50_000_000 && zoomScale < 0.5` (heuristic for "world view
where overdraw dominates anyway"). Combine with backpressure that no-ops
new frames if a device-lost recovery is in progress; this prevents a
stuck frame from cascading into a freeze.

Lesson: every render pass should have a "skip me at extreme N" gate. The
cost of running a pass that produces nothing visible is the same as
running it for real.

---

## 4. Build, deploy, and bundling pitfalls

These are not performance issues but every one of them silently shipped a
stale build to the desktop app and cost 30+ minutes per occurrence.

### 4.1 `@embedding-atlas/component` resolves to its prebuilt `dist/`

Symptom: edits to `packages/component/src/...` never showed up in the
desktop app despite a `npm run build`.

Root cause: `packages/viewer` imports `@embedding-atlas/component`, which
resolves to `packages/component/dist/`, not `src/`. You must rebuild
**both** packages — component first, then viewer — for source edits to
ship.

Lesson: in this monorepo, the build order is **component → viewer →
sidecar → sync to .app**. Skipping any step ships stale code.

### 4.2 Desktop renderer loads a prebuilt viewer bundle, not Vite

Symptom: edits to `packages/viewer/src/...` were live in `npm run dev`
but never reached the Electron app.

Root cause: the desktop sidecar embeds a prebuilt copy of the viewer at
`apps/desktop/release/.../sidecar/_internal/embedding_atlas/static/`.
Vite is only the dev-mode boot UI; production reads from the bundled
static dir. The sync is done by `npm run build:sidecar` (calls
PyInstaller).

Lesson: `npm run build:sidecar` is mandatory for any viewer change to
reach the .app. The sidecar repackage is what actually ships the new
JS bundle.

### 4.3 PyInstaller `--onedir` bytecode lives inside a PYZ archive

Symptom: editing a `.py` file inside
`apps/desktop/release/.../sidecar/_internal/embedding_atlas/` had no
effect on the running app.

Root cause: PyInstaller's `--onedir` mode bundles all Python source into
a single PYZ archive inside the launcher binary. The `_internal/*.py`
files visible on disk are *stale unpacked copies* used for imports that
PyInstaller couldn't statically resolve. The PYZ is the source of truth.

Lesson: only `npm run build:sidecar` (which re-runs PyInstaller) ships
Python changes. Hand-editing `_internal/*.py` is a no-op.

### 4.4 Desktop `viewer-state.json` carries stale per-dataset precomputed columns

Symptom: switching datasets in the Electron map showed empty data even
though the parquet had loaded successfully.

Root cause: the desktop app caches per-dataset column metadata in
`viewer-state.json` (e.g. precomputed `_proj_x` / `_proj_y` column names).
If a dataset is reloaded with new column names, the stale entry wins and
the renderer requests columns that no longer exist.

Lesson: when a dataset shows empty in the Electron map, **check
viewer-state.json before re-debugging the pipeline**. Most "empty map"
bugs are stale state, not renderer regressions. Purge the dataset's
entry and reload.

---

## 5. Debugging methodology that worked

The recent saga (section 2) was solved by chaining three tools that each
gave a different angle:

### 5.1 CDP heap snapshots at multiple time points

Connect to the Electron renderer via Chrome DevTools Protocol on a
debug port:
```bash
GEOSPATIAL_ATLAS_DEBUG_PORT=9222 ./Geospatial\ Atlas.app/...
```
Then take heap snapshots at: cold load, after first scatter, after first
filter click, after multiple filter clicks. Compare retained sizes per
class. The leak shows up as a constructor (e.g. `Uint32Array`) whose
retained bytes grow monotonically while the count grows in lockstep.

### 5.2 Retainer chains, not just retained sizes

`HeapProfiler.takeHeapSnapshot` returns a graph; the parser at
`/tmp/parse-heap.mjs` walks **back-edges** from the largest objects to
find what's pinning them. This is how we discovered:
- The 627 MiB Uint32Arrays were pinned via `lastXPacked@bRe` (the
  minified renderer instance).
- The 2-MiB Arrow Vector chunks were pinned via
  `clientCache@QueryManager`.

Without retainer chains we would have known what was big but not why.

### 5.3 Live monitor on stage logs

A persistent `Monitor` task watching the renderer console for
`[atlas-stage]`, `[scatter]`, `[error]`, `RangeError`, etc. surfaces
events the moment they happen — much faster than tailing logs by hand,
and the events arrive as task-notifications that can wake a `/loop`
agent.

This is how we caught the second 158 M scatter landing successfully
where it had previously OOM'd — proof the fix held.

---

## 6. What worked, with measured impact

| Change                                                            | Before                              | After                                | Notes                                                         |
| ----------------------------------------------------------------- | ----------------------------------- | ------------------------------------ | ------------------------------------------------------------- |
| `coordinator.manager.cache(false)`                                | 8+ GiB pinned in `clientCache`      | 0 retained Arrow Tables              | Single-line change; biggest absolute win                      |
| Drop `lastXPacked`/`lastYPacked` on null-input                    | 4 × 627 MiB stuck Uint32Arrays      | Arrays freed on next major GC        | Renderer-side identity-cache fix                              |
| Drop those refs again pre-unpack                                  | Old + new co-existed during await   | Only new lives during await          | Prevents 2× ArrayBuffer pressure window                       |
| `queryResult` async + `setTimeout(0)` yield                       | Same-microtask alloc OOM'd at 158 M | 1205 MB peak, 338 ms `toArray`       | The microtask-boundary fix                                    |
| Gated `globalThis.gc()` at >50 M rows                             | Major GC waited for alloc failure   | Forced sweep before next alloc       | Requires Electron `--expose-gc`                               |
| `--max-old-space-size=16384`                                      | 12 GB tight in long sessions        | Headroom against fragmentation       | V8 budget != resident memory                                  |
| `categoryCount` override at the prop site                         | Canvas blue with `c` column         | All categories render correctly      | Single misplaced default                                      |
| u32 packed wire (1.5 cm quantum)                                  | u16 grid visible at city zoom       | Sub-pixel at all zoom levels         | 8 bytes/pt over 4; zstd halves the cost                       |
| `streamingRestConnector`                                          | `Failed to fetch` at 2.1 GB         | 322 M scatter wire works             | Bypasses fetch-pipeline 2 GB cap                              |
| `con.sql(...).fetch_arrow_table()`                                | 90 s cold load                      | 9 s cold load                        | Use relation API for any large arrow export                   |
| `ORDER BY __row_index__` in split-scatter                         | Random point positions              | Stable alignment                     | DuckDB parallel exec re-orders SELECTs                        |
| `arrow_cache.put_pinned()` for prewarm                            | Silent drop at >1 GB                | Prewarm actually populates           | `put()` is size-capped silently                               |
| Chunked `drawPoints` (≤64 M instances/buffer)                     | Metal watchdog trips at 322 M       | All zooms render                     | Per-buffer 5 s limit, not per-frame                           |
| WG-local atomic reduction in `viewport_cull`                      | Per-pass cost grew with N           | Flat per-chunk cost                  | Pairs with chunked dispatch                                   |
| `device.queue.onSubmittedWorkDone()` before `gpuBuffer.destroy()` | Random NaN after pan-release        | Stable pan                           | Metal silently corrupts on in-flight destroy                  |
| Skip accumulate at `N > 50 M && zoomScale < 0.5`                  | 10+ s zoom-out freeze               | Smooth zoom-out                      | Pass produces nothing visible at world view anyway            |
| Spatial-sort table at materialise                                 | Tooltip pick lookup linear-scanned  | Sub-ms tooltip                       | Co-locates points that share a tile                           |

## 7. What did NOT work — failed attempts that ate time

These are concrete approaches that looked plausible, were tried, and were
reverted. Documented here so the same dead ends are not re-explored.

### 7.1 Pre-releasing buffers in the `query` callback (NOT `queryResult`)

**Idea:** null `xPackedData` / `yPackedData` at the moment Mosaic emits
the SQL, before the response arrives, to maximise the headroom for the
incoming `toArray()`.

**Why it failed:** when the subsequent `toArray()` itself OOM'd (which it
still did before the LRU-cache fix), the renderer was left with empty
state and a blank canvas — indefinitely, until the user reloaded. The
prior render was destroyed and the new one never landed. The right
trade-off is to keep showing the last good data and let the new fetch
fail visibly; the user can then narrow the filter and retry.

**Lesson:** never destroy the only surviving copy of the rendered state
in anticipation of an allocation that might fail. Release **after**
success, or release in `queryResult` where the new data is already in
hand.

### 7.2 Bumping V8 budget without `--expose-gc`

**Idea:** "the OOM is allocation pressure → just give V8 more headroom."

**Why it failed (alone):** even at 16 GB budget, V8 only triggers a major
GC reactively, when an allocation **fails**. Without `--expose-gc` we
could not force a sweep between releasing old buffers and allocating new
ones — fragmentation pinned the heap and the very next scatter still
threw `RangeError`. Budget was necessary but not sufficient.

**Lesson:** budget is allocator headroom; it does not change *when* GC
runs. Pair budget bumps with `--expose-gc` + explicit `globalThis.gc()`
sweeps at the points where you know you just released large buffers.

### 7.3 Trusting that `xPackedData = null` would free the GPU-side mirror

**Idea:** if the consumer nulls its ref, the renderer (which received a
ref) will also drop on its next setProps.

**Why it failed:** the renderer kept its **own** `lastXPacked` ref for
identity-equality checks. The consumer's null reached `setProps` but the
renderer's local copy stayed alive until reassigned by the next non-null
input. Heap snapshot proved the retainer chain pointed at the renderer
instance, not the consumer.

**Lesson:** "memoise the last value" patterns have a hidden lifetime. If
the value can be 1 GB, you must clear the memoised ref on null-input or
on the path that re-derives.

### 7.4 Fixing only `categoryCount`

**Idea:** the user's PhD-level diagnosis traced the empty render to
`categoryCount: 1` hardcoded in the viewportHint mock. Surely fixing
that one line solves it.

**Why it failed (alone):** it was correct — `categoryCount` was wrong —
but it was only one of three compounding bugs. The OOM was orthogonal
and dominated the symptom. The category fix shipped in the bundle but
the user still saw a blank canvas because the new scatter never landed.

**Lesson:** when a single fix doesn't resolve the symptom, do not assume
the fix was wrong. Verify it actually shipped (we confirmed the minified
bundle had the override expression), then look for a co-occurring bug.

### 7.5 Capping or sampling at huge N to dodge the Metal watchdog

**Idea (forbidden):** the watchdog kills passes >5 s; cap point count or
subsample to keep the pass under 5 s.

**Why it failed (philosophically):** every point must render at world
view. Capping silently lies about the dataset; subsampling lies
non-deterministically. Both ship a known-incorrect answer to dodge a
constraint.

**Why it failed (mechanically):** even at 322 M, chunked dispatch keeps
each command buffer well under 5 s, with no information loss.

**Lesson:** treat any "let's just cap N" suggestion as a regression. The
constraint is per-buffer, not per-frame; split the work, don't drop it.

### 7.6 Editing `_internal/*.py` directly in the .app bundle

**Idea:** quick patch to a Python file inside
`apps/desktop/release/.../sidecar/_internal/embedding_atlas/`.

**Why it failed:** PyInstaller `--onedir` ships compiled bytecode in a
PYZ archive. The `_internal/*.py` files are *stale unpacked copies* used
only as fallback for unresolved imports. Edits never run.

**Lesson:** only `npm run build:sidecar` ships Python changes. The visible
`.py` files in the bundle lie.

### 7.7 Editing component sources without rebuilding component

**Idea:** edit `packages/component/src/lib/...`, run `npm run build` in
the viewer, see the change.

**Why it failed:** the viewer imports `@embedding-atlas/component` from
the package's `dist/`, not `src/`. Without rebuilding the component
package first, the viewer ships against stale dist.

**Lesson:** build order is **component → viewer → sidecar → sync to
.app**. There is no shortcut.

### 7.8 Using `Response.arrayBuffer()` for the >2 GB scatter wire

**Idea:** standard fetch ergonomics; the V8 ArrayBuffer goes up to 16 GB
on 64-bit, so a 2.1 GB body should be fine.

**Why it failed:** Chrome's fetch pipeline imposes its own ~2 GB ceiling
on the body accumulator. The error surfaces as a generic `Failed to
fetch`, easy to misattribute to V8.

**Lesson:** always check **which** ceiling you hit. Fetch's body buffer
and V8's allocator share the name "ArrayBuffer" but have different limits.

### 7.9 u16 packed wire format (early implementation)

**Idea:** halve the wire by quantising x/y to u16 over the world bounding
box.

**Why it failed:** at 360° world span, u16 quantum is ~5 km — coarser
than building-level resolution. City zoom showed a visible coordinate
grid.

**Lesson:** u32 is the minimum precision for world-bbox geospatial. u16
is fine for tile-local quantisation but not for global.

### 7.10 Synchronous `gpuBuffer.destroy()` after a frame

**Idea:** GPU memory is precious; release as soon as JS no longer needs
the buffer.

**Why it failed:** Metal command buffers may still be in flight referencing
the buffer. Synchronous destroy poisons the device — every subsequent
compute pass returns NaN.

**Lesson:** `destroy()` must be deferred through
`device.queue.onSubmittedWorkDone()`. Do not destroy inside the same
event-loop turn as the submit that referenced the buffer.

---

## 8. Bananaskins — small slips that bit us repeatedly

Quick-scan list of unintuitive gotchas. Each one cost ≥30 minutes the
first time; documented to recognise on sight.

- **Mosaic's `QueryManager` LRU is on by default with `max=1000, ttl=3h`.**
  At small datasets it's invisible. At 322 M it pins ~2.5 GB per result.
  Disable explicitly per-coordinator.
- **Svelte 5 `$state` (reactive) vs `$state.raw` (no proxy).** `$state`
  on a 1 GB typed array doubles memory with the reactive Source wrapper.
  Use `$state.raw` for anything large.
- **Svelte 5 batched flush is microtask-deferred.** A null-out is not
  visible to downstream effects until the next microtask. Async-yield
  before the next allocation.
- **DuckDB `preserve_insertion_order=false` silently re-orders parallel
  SELECTs.** Two queries against the same table can return rows in
  different orders. Lock alignment with `ORDER BY __row_index__`.
- **`arrow_cache.put()` returns silently when buffer exceeds `max_bytes`.**
  No error, no log; cache stays empty. Use `put_pinned()` for prewarm.
- **PyInstaller `_internal/*.py` files are stale.** The PYZ archive is the
  source of truth. Hand edits to unpacked .py files are no-ops.
- **`viewer-state.json` retains per-dataset precomputed column names.**
  Switching datasets with the same parquet path but different columns
  shows empty data. Purge the entry first.
- **`@embedding-atlas/component` resolves to `dist/`, not `src/`.** Edits
  to component source need a component rebuild before the viewer picks
  them up.
- **Electron's debug port flag is not the standard Chromium one.** Use
  the project-specific `GEOSPATIAL_ATLAS_DEBUG_PORT=9222` env var, not
  `--remote-debugging-port`.
- **`globalThis.gc()` is undefined without `--expose-gc`.** The forced-GC
  branch in `queryResult` silently no-ops in dev/standalone where the
  flag isn't passed; only Electron triggers the sweep.
- **Mosaic's `packedBounds` are computed once per session.** Re-loading a
  dataset that changes its coordinate range without restarting the
  coordinator gives wrong unpacked positions.
- **`canvas.toDataURL()` closes over the renderer instance.** That
  closure becomes a separate retainer path for the entire renderer (and
  its packed buffers) — visible in heap snapshots as a duplicate
  retainer chain. Avoid `toDataURL` on the live render canvas.
- **Long-running CDP listeners hold heap snapshots in memory.** Take the
  snapshot, parse, free. Forgetting to release the snapshot from the
  listener side adds 200+ MB of phantom retention.
- **`Float32Array(N)` on N > 1.07 G fails before V8 ever asks the OS.**
  V8 caps individual `TypedArray` length at 2³⁰ - 1. Use `Uint32Array`
  for packed coordinate arrays at 322 M (2³² range covers it).
- **macOS Activity Monitor's "Memory" column is NOT JS heap.** It's
  resident set size — includes mapped GPU buffers, asar bundles, every
  Electron child process. Use Chromium's task manager (Cmd-Shift-Esc)
  for renderer-only JS heap.
- **`EmbeddingViewImpl.svelte:368` declares `let renderer = $state(null)`
  not `$state.raw(null)`.** Reactive Source wrapping a renderer that
  holds 1.3 GB+ of GPU-mirrored typed arrays creates a second retainer
  path (visible in heap snapshots as a duplicate retainer chain). The
  Mosaic LRU fix made this non-blocking, but the wrap still costs ~50 MB
  of proxy overhead at long-session steady state. Open follow-up: switch
  to `$state.raw`.
- **`canvas.toDataURL()` closes over the renderer instance.** That
  closure becomes a separate retainer path for the entire renderer (and
  its packed buffers) — visible in heap snapshots as a duplicate
  retainer chain even after the renderer is unmounted. Avoid
  `toDataURL` on the live render canvas; use `getImageData` against an
  off-screen copy instead.
- **WebGPU adapter retains its `device.lost` callback closure forever.**
  If the closure references the renderer instance, a single
  device-lost event during dev (e.g. closing DevTools) keeps the entire
  renderer + buffers alive past unmount. Bind the callback at
  module-scope or weak-ref the renderer.

---

## 8b. Multi-distro compatibility — what works where (the Chrome question)

The repo ships **three distros**:

| Distro                  | Entry                              | Browser                       | Sidecar              | DuckDB           |
| ----------------------- | ---------------------------------- | ----------------------------- | -------------------- | ---------------- |
| **standalone web**      | `FileViewer.svelte`                | any (Chrome/Safari/Firefox)   | none                 | DuckDB-WASM      |
| **backend-frontend**    | `Viewer.svelte` via `embedding-atlas` CLI | user-chosen (typically Chrome) | uvicorn HTTP, native | server-side, native |
| **desktop (standalone)** | `Viewer.svelte` inside Electron     | bundled Chromium              | embedded sidecar     | server-side, native |

Every fix from sections 1–8 ships to **all three** because they live in
shared packages (`packages/component`, `packages/viewer`,
`packages/backend`). The exception is the V8 tuning, which lives in
`apps/desktop/electron/main.ts` and **only applies to the desktop
distro**.

### 8b.1 Backend-frontend in vanilla Chrome (the user's question)

**Status: works at 322 M for cold load + first interaction; degrades
at sustained sessions.** All code-level fixes ship; the V8 tuning gap
makes long sessions unstable.

What ships to Chrome via `npm run build` → `backend/static/`:

| Fix                                          | Ships to Chrome? | Effective?                                  |
| -------------------------------------------- | ---------------- | ------------------------------------------- |
| `coordinator.manager.cache(false)`           | Yes              | Yes — biggest absolute win                  |
| Renderer drops `lastXPacked` on null-input   | Yes              | Yes                                         |
| Renderer drops refs pre-unpack               | Yes              | Yes                                         |
| `queryResult` async + `setTimeout(0)` yield  | Yes              | Yes                                         |
| Forced `globalThis.gc()` gated >50 M         | Yes              | **No** — `gc` is undefined; branch no-ops   |
| `--max-old-space-size=16384`                 | **No**           | Chrome defaults to ~4 GB old-gen per tab    |
| `--expose-gc`                                | **No**           | Same — Chrome flag, not page-level          |
| `categoryCount` override                     | Yes              | Yes                                         |
| u32 packed wire + streaming connector        | Yes              | Yes                                         |
| Spatial-sort at materialise (server-side)    | Yes              | Yes                                         |
| Chunked draw / WG-local atomic reduction     | Yes              | Yes                                         |
| `device.queue.onSubmittedWorkDone()` destroy | Yes              | Yes                                         |
| Skip accumulate at huge N                    | Yes              | Yes                                         |

**Practical Chrome behaviour at 322 M:**
- Cold load + first scatter: peak heap ~2.5 GB. **Fits Chrome's ~4 GB
  cap.** Works.
- First filter click with `c` column: transient peak ~3.5–4 GB without
  the forced GC sweep to compact between releases. **Tight; works on a
  freshly-opened tab, fails after multiple toggles.**
- Long session (>10 min, multiple filter changes, sidebar interactions):
  V8 fragmentation creeps; **`Array buffer allocation failed` is
  expected** within 30–60 minutes.
- Datasets <100 M: solid, no caveats.

**To make Chrome work reliably at 322 M:** the user must launch Chrome
with V8 flags. There is no way to set these from the page.

```bash
# macOS
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
  --js-flags="--max-old-space-size=16384 --expose-gc" \
  http://localhost:5055

# Linux
google-chrome --js-flags="--max-old-space-size=16384 --expose-gc" \
  http://localhost:5055
```

This is brittle UX. Better long-term fixes (in priority order):

1. **Lower the peak heap below 4 GB.** Stream-decode the wire directly
   into the final typed arrays (skip the intermediate Arrow Table).
   Brings peak from ~3.7 GB to ~2.3 GB; works in vanilla Chrome
   without flags. Estimated effort: medium (custom IPC reader).
2. **Move `toArray()` off the main thread.** Worker + transferred
   buffers; doesn't lower peak but stops blocking the UI for ~3 s
   per filter change. Lets Chrome's GC run during the worker copy.
3. **Detect the V8 budget at startup** (`performance.memory.jsHeapSizeLimit`)
   and surface a banner: "Your browser is configured with a 4 GB heap;
   datasets >100 M may crash. Recommend desktop app or launching
   Chrome with `--js-flags`." Cheap, ships now.
4. **Make `--expose-gc` optional but recommended.** The forced-GC
   branch already gates on `typeof globalThis.gc === "function"`, so
   it silently no-ops in Chrome. Keep it that way; just add a console
   warning at startup when running >100 M without `gc` exposed.

**Recommendation:** for users running 322 M datasets, the desktop
distro is the supported path. The backend-frontend distro is supported
for ≤100 M datasets in any browser; for >100 M users should be guided
to either the desktop app or Chrome-with-flags.

### 8b.2 Standalone web (DuckDB-WASM via FileViewer)

**Status: untested at 322 M.** The standalone path uses DuckDB-WASM,
which has different memory dynamics than the native server-side
DuckDB used by the other two distros:

- DuckDB-WASM runs inside the same renderer process as the viewer, so
  DuckDB's working memory **competes for** the same V8 4 GB cap (or
  16 GB in Electron).
- DuckDB-WASM has its own internal memory limit (default 2 GB);
  exceeding it fails the query, not the page.
- Parquet reads in DuckDB-WASM go through `fetch()` → ArrayBuffer,
  hitting the **2 GB body cap** for the parquet itself if loaded
  whole. Streaming parquet read does not exist in DuckDB-WASM today.

**Expected ceiling: ~75–100 M rows in standalone web.** Above that,
DuckDB-WASM either spills to virtual memory (slow) or fails the query.
The renderer-side fixes still apply but the bottleneck moves upstream
to the WASM database.

The `FileViewer.svelte` cache-disable (section 2.1) is in place, so
small-to-medium datasets in the standalone web distro are unaffected
by the LRU pinning bug. But 322 M in standalone web has not been
exercised end-to-end; treat as unsupported until measured.

### 8b.3 Desktop (Electron) — verified at 322 M

This is the supported path for 322 M. Numbers in section 9.1 are from
this distro on commit `99b73a6`.

---

## 9. The current bottleneck (as of 2026-04-27)

With the leaks plugged and 322 M rendering reliably, the remaining
performance ceiling is **synchronous Arrow `toArray()` on the category
column**, with a secondary ceiling at **end-to-end filter latency**.

### 9.1 Where the time goes (322 M with `c` column, filter click → render)

Measured on the working build (commit `99b73a6`, M-series, 32 GB):

| Phase                                       | Duration       | Blocking?           | Where                                                |
| ------------------------------------------- | -------------- | ------------------- | ---------------------------------------------------- |
| Sidecar SQL → arrow                         | ~3–5 s         | No (server-side)    | `embedding_atlas/connector` (DuckDB, fast_load)      |
| Wire transfer (zstd, ~1.5 GB)               | ~1.5–2 s       | No (streamed)       | `streamingRestConnector`                             |
| `data.getChild("c").toArray()`              | **~1.8 s**     | **Yes (main JS)**   | `EmbeddingViewMosaic.queryResult`                    |
| `data.getChild("x").toArray()`              | ~520 ms        | Yes (main JS)       | same                                                 |
| `data.getChild("y").toArray()`              | ~500 ms        | Yes (main JS)       | same                                                 |
| Forced `globalThis.gc()`                    | ~200–500 ms    | Yes (main JS)       | gated >50 M                                          |
| GPU upload (3 buffers, ~3 GB)               | ~300–600 ms    | No (queued)         | `renderer.setProps`                                  |
| First frame render                          | ~120 ms        | No                  | chunked `drawPoints`                                 |
| **Total wall clock filter → painted pixel** | **~7–10 s**    |                     |                                                      |

The category-column `toArray()` at 1.8 s is the longest blocking JS
phase. Filter clicks therefore have a ~3 s perceived latency floor (the
async phases overlap; the JS-blocking phases serialise).

### 9.2 Why it's slow

`Arrow.Vector.prototype.toArray()` materialises a contiguous typed array
from the column's internal chunked representation. At 322 M Uint8 values
that's a 322 MB Uint8Array allocation + a chunk-by-chunk `set()` copy.
The category column is u8 (categories ≤ 256) so the wire is small, but
the JS-side concatenation is what hurts.

The two coordinate columns are u32 packed → already arrive as a single
chunk most of the time, so their `toArray()` is ~520 ms (mostly zero-copy
when single-chunked).

### 9.3 Candidate fixes (in priority order)

1. **Avoid `toArray()` on the category column entirely.** Pass the
   `Vector`'s underlying typed-array view directly to the renderer. If
   the column is single-chunked, `.data[0].values` is already a
   Uint8Array — zero-copy. If multi-chunked, concatenate on a worker
   thread off the main JS event loop.

2. **Move all three `toArray()` calls to a Web Worker.** The Arrow Table
   crosses postMessage with `transfer:` zero-copy for the underlying
   buffers. This unblocks the main thread for ~3 s of latency hiding —
   the pan/zoom UI stays responsive during filter changes.

3. **Stream-decode the wire directly into the final typed arrays**,
   skipping the intermediate Arrow Table allocation. This requires a
   custom IPC reader; the upside is ~1.5 GB less peak heap and ~1 s less
   `toArray` time, but it's a structural change that bypasses Mosaic's
   abstractions.

4. **Sidecar-side category encoding.** Have DuckDB emit the category
   column as a single chunk by setting
   `pragma threads=1` on the export query, or by materialising the
   filtered result before arrow conversion. Trade-off: filter SQL gets
   slower, scatter prep gets faster. Worth measuring at next scale step.

### 9.4 Secondary ceilings

- **Long-session V8 fragmentation.** With cache disabled and refs
  released, we still see ~1 GB of "unaccounted" growth over a 30-minute
  session. Likely Mosaic's smaller per-cell state + DOM observers. Not
  user-visible yet at 16 GB budget; will be at 1 B rows.
- **Cold-load wall time.** Initial parquet → first render at 322 M is
  ~12–15 s end-to-end. Most of that is sidecar parquet → arrow; the
  viewer is already optimised. The sidecar's `con.sql()` read at
  multi-threaded parallelism is the floor.
- **GPU upload of u32 packed buffers.** ~300–600 ms is fine at 322 M;
  at 1 B it would be ~1–2 s and start to compete with frame budget. May
  need streaming uploads (chunked `writeBuffer` calls per frame).

### 9.5 What we're NOT bottlenecked on (anymore)

- WebGPU compute throughput — workgroup tuning + WG-local atomic
  reduction landed us at <2 ms/pass.
- WebGPU draw throughput — chunked `drawPoints` is display-capped at 120 fps
  even at 322 M.
- Wire format size — u32+u8+zstd lands ~1.5 GB for 322 M, fits the 16 GB
  budget with room.
- DuckDB query time — `con.sql()` materialises the scatter SELECT in
  ~3–5 s.
- Mosaic LRU pinning — disabled.
- Renderer ref-pinning — released on null-input.
- Metal watchdog — chunked dispatch holds.

---

## 10. Open follow-ups (worth doing, not blocking)

These are known issues that did not bite hard enough to fix during the
recent saga, but are documented here so they aren't rediscovered from
scratch.

1. **`EmbeddingViewImpl.svelte:368` should use `$state.raw`.** Currently
   declares `let renderer: EmbeddingRenderer | null = $state(null)`.
   The reactive Source proxy adds a duplicate retainer chain visible in
   heap snapshots. Non-blocking now (Mosaic LRU fix dominated), but
   ~50 MB of avoidable overhead at long-session steady state.

2. **DuckDB-WASM at 322 M is untested.** The standalone web distro
   (`FileViewer.svelte`) has not been exercised on the eubucco-scale
   parquet. Likely failure modes: 2 GB Response.arrayBuffer cap on the
   parquet itself, DuckDB-WASM 2 GB internal memory limit, or
   competition with V8 for the same 4 GB tab budget. Build a synthetic
   200–300 M parquet test against it to find the actual ceiling.

3. **Stream-decode the wire directly into typed arrays.** The current
   pipeline allocates an Arrow Table → calls `toArray()` per column →
   hands typed arrays to the renderer. Peak heap during this dance is
   1.8× the steady state. A custom Arrow IPC stream reader that
   writes directly into pre-allocated `Uint32Array` / `Uint8Array`
   buffers would cut peak by ~1.5 GB at 322 M. Most impactful for
   making the backend-frontend distro work in vanilla Chrome without
   `--js-flags`.

4. **Move `toArray()` to a Web Worker.** Won't lower peak heap (the
   memory still has to live somewhere) but unblocks the main thread for
   ~3 s per filter change. UI remains responsive; the user can
   interact with the side panel while the new scatter materialises.

5. **Cold-init profile (task #72).** End-to-end "click dataset → first
   pixel" at 322 M is ~12–15 s. Where does it go? Likely the parquet
   footer scan + the prewarm. A flame chart would tell us if there is
   parallelism left on the table.

6. **Long-session V8 fragmentation (~1 GB unaccounted)**. With cache
   disabled and renderer refs released, we still see ~1 GB of growth
   over 30 minutes. Heap snapshot suspects: Mosaic per-cell client
   state, DOM observers, retained event listeners, accumulated
   `console.log` strings (each scatter logs ~200 B). Audit and gate
   logging behind a debug flag.

7. **GPU buffer pool.** Every filter click currently destroys ~3 GB of
   GPU buffers and allocates ~3 GB of new ones. At 322 M this is
   ~600 ms of overhead per filter. A simple ring-buffer pool (2 slots
   per logical buffer) would let the new allocation reuse the old
   storage and the destroy fires only when the new render commits.

8. **Selection (lasso/box) at 322 M is untested.** The selection UI
   triggers a SQL `WHERE ST_Within(...)` which might return tens of
   millions of rows. Untested at this scale; likely fast (DuckDB
   spatial extension is well-optimised) but the **subsequent client-side
   re-render of the filtered scatter** goes through the same
   `toArray()` path and is subject to the same heap pressure. Probably
   works; needs an explicit test.

9. **Detect `device.lost` and offer reload.** Currently a Metal
   watchdog trip leaves the canvas blank with no UI affordance. Listen
   on `adapter.requestDevice().then(d => d.lost.then(...))` and surface
   a banner: "GPU recovered after timeout — click to re-render." Cheap;
   directly addresses the worst recoverable failure mode.

10. **`apps/desktop/electron/main.ts` heap cap is OS-blind.** Currently
    hardcoded to 16384 MB. On a 16 GB machine this is the entire RAM —
    an OOM-killer event waiting for any other process. Check
    `os.totalmem()` and clamp budget to `min(16384, totalmem * 0.5)`.

---

## Quick-reference cheat sheet

| Symptom                                       | First place to look                                                                                |
| --------------------------------------------- | -------------------------------------------------------------------------------------------------- |
| `Array buffer allocation failed` mid-session  | Heap snapshot retainer chain — almost always Mosaic LRU + renderer ref-pinning compounding         |
| Empty/blue scatter at 322 M with `c` column   | `EmbeddingViewMosaic.svelte` line 752 `categoryCount` override                                     |
| Empty Electron map after dataset switch       | `viewer-state.json` stale entry — purge before re-debugging the pipeline                           |
| Edits to component not reaching desktop app   | Rebuild order: component → viewer → sidecar → sync to .app                                         |
| `Failed to fetch` at 2 GB scatter wire        | `Response.arrayBuffer()` ~2 GB cap — use `streamingRestConnector`                                  |
| Visible coordinate grid at city zoom          | Wire format is u16; switch to u32 packed (1.5 cm quantum)                                          |
| `MTLCommandBuffer execution failed` on pan    | Chunk the offending compute or draw pass; cap chunk size at <64 M instances                        |
| NaN compute output after rapid pan-release    | `gpuBuffer.destroy()` must be wrapped in `device.queue.onSubmittedWorkDone()`                      |
| Slow cold parquet load                        | Use `con.sql(...).fetch_arrow_table()`, not `cursor.execute(...)`                                  |
| Random point positions in split-scatter mode  | Add `ORDER BY __row_index__` to every per-axis query                                               |
| Prewarm appears to succeed but cache is empty | `arrow_cache.put()` silently drops `len > max_bytes` — use `put_pinned()`                          |
| Filter click leaves canvas blank              | Async-yield required between releasing prior buffers and `toArray()`; check `--expose-gc` is set   |

---

## Verified working configuration (2026-04-27, commit `99b73a6`)

- **Dataset:** eubucco 322,562,870 buildings, ~7.8 GB parquet
- **Hardware:** Apple Silicon M-series, 32 GB unified memory
- **Cold load:** 322 M scatter renders at 2461 MB peak JS heap
- **With `c` column:** 2769 MB peak
- **Filter click → 158 M residential:** 1205 MB, 338 ms `toArray`
- **Filter click → 164 M non-residential:** 1255 MB, 356 ms `toArray`
- **No OOM, no Metal watchdog trips, no device-lost events** across a
  ~10 minute session of repeated filter toggling.

The settings that got us here:
- `coordinator.manager.cache(false)` in viewer entry points
- `--max-old-space-size=16384 --expose-gc` in Electron
- `lastXPacked`/`lastYPacked` released on null-input and pre-unpack
- `queryResult` async with `setTimeout(0)` yield + gated `gc()`
- `categoryCount` override at the prop site
- u32 packed wire + streaming connector + chunked dispatch + WG-local
  atomic reduction
- Sidecar memory cap (DuckDB `memory_limit`) tuned to leave V8 16 GB
  headroom

Treat any regression from these settings as a leak, not a tuning
problem. The numbers above are what "working" looks like.
