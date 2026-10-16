# Changelog

All notable changes to the Geospatial Atlas desktop app are documented here.

Desktop-app releases are tagged `vX.Y.Z` (from v0.0.7 onward; earlier
releases used the `app-vX.Y.Z` prefix). The static web viewer is deployed
manually; this fork does not publish Python or npm packages — see
[`docs/RELEASING.md`](docs/RELEASING.md).

## v0.0.10 — 2026-10-16

Source packages are synchronized with `apple/embedding-atlas` through
`ef64656` (26 commits since the previous upstream merge).

### Added

- GPU-rendered parallel coordinates with numeric, temporal, and categorical
  axes, coloring, and coordinated axis brushing.
- Optional neighbor-graph metrics: `--degree` adds in/out/total degree,
  `--kcore` adds core numbers, and `--clustering` adds local clustering
  coefficients. PageRank now lives alongside these in `graph_metrics`.
- Feature descriptions and lists of similar features in the features view.
- A programmatic `highlight` viewer prop, synchronized across coordinated
  views, and CLI `--state` for loading exported viewer state from JSON or
  a file. `--state` also works on the fork's fast GIS loading path.

### Fixed

- Large-point world-zoom GPU crashes on Retina displays: draw batches now
  scale with device-pixel point area without dropping points or changing
  their size. Lost-device cleanup, adapter retries, and unmount guards
  prevent cascading errors and stuck views.
- Default widget row IDs follow input row order, including data spanning
  multiple DuckDB row groups.
- NumPy scalar cache keys, Windows cache-file replacement, and automatic
  vector detection for NumPy/Polars list columns.
- `.ndjson` loading, WebP/HEIF/HEIC/AVIF image detection, and the MIME type
  sent to image embedding providers.
- Per-value timezone offsets when inferring date/time formatting.
- The viewer MCP `data_query` tool rejects non-readonly and multi-statement
  queries.
- Deep equality handles objects that shadow `hasOwnProperty`; shared
  dataflow contexts detach destroyed children.
- Cross-platform version consistency checks and documentation image
  loading; refreshed GitHub Actions dependencies.
- Large-data validation exposed a drag-idle timeout that scheduled full
  GPU redraws while the mouse remained held. Dragging now stays on the
  CSS-pan path until release or window blur, including slow input events.

### Maintenance

- Keep builds, type checks, formatting, JavaScript/Python/Rust tests, and
  Clippy in CI, with one Python runtime and an installed-wheel smoke test.
- Remove unused registry/Pages publishing, package uploads, the mobile
  workflow placeholder, and expensive demo generation from routine CI.
- Align desktop release tooling with the current WASM dependencies and
  use locked npm installs for all three supported platforms.
- Restore missing cross-platform desktop lockfile entries without
  upgrading existing dependencies. Routine CI now checks the desktop
  lockfile too. The failed, unpublished v0.0.9 build tag is unchanged.

### Fork compatibility

- Retained GIS detection, geocoding, Overture display support, wide-integer
  rendering, interrupted Arrow recovery, packed-coordinate GPU uploads,
  chunked drawing, gesture compaction, f32 fallback, and deferred GPU
  resource destruction.
- Adapted parallel coordinates to the fork's GPU-device result and retained
  its required `float32-blendable` feature through f16 fallback.
- Kept the macOS/Python 3.12 PageRank crash workaround in `graph_metrics`
  and compatibility imports from `embedding_atlas.pagerank`.
- Retained the `geospatial-atlas` package/CLI name and adapted upstream's
  version checker to the fork's Python lockfile.
- Upstream removed the obsolete Python `show_table`, `show_charts`, and
  `show_embedding` options. Configure layouts via `initial_state` instead.

## v0.0.8 — 2026-04-29

**322 M-row scaling.** Verified end-to-end on the eubucco buildings
parquet (322,562,870 points, 7.8 GiB on disk) on a 32 GiB Apple
Silicon laptop. Full color-by + filter interactivity holds at this
scale; memory pipeline hardened against three independent leaks
that compounded to OOM the renderer at filter time on v0.0.7.

### Performance — 322 M points

| stage                             | result                              |
| --------------------------------- | ----------------------------------- |
| 322 M cold scatter                | 2461 MB JS heap, 520 ms `toArray`   |
| 322 M with category column        | 2769 MB JS heap, 1.8 s `toArray`    |
| Filter click → 158 M residential  | 1205 MB JS heap, 338 ms `toArray`   |
| Tooltip pick at 322 M             | ~50 ms (was ~10 s pre-spatial-sort) |
| Cold load (parquet → first frame) | 12–15 s end-to-end                  |

### Fixed

- **Color-by + filter clicks no longer OOM at 322 M.** Three leaks
  combined to exhaust V8's ArrayBuffer pool: Mosaic's `QueryManager`
  LRU pinned every 2.5 GB Arrow Table for 3 hours, the renderer kept
  `lastXPacked` / `lastYPacked` alive past consumer null-out, and
  `queryResult` allocated the new `toArray()` copy in the same
  microtask that nulled the old buffers. Each survivable alone;
  together they OOM'd at 322 M. Now: cache disabled, renderer refs
  released on null-input, `queryResult` is async with a microtask
  yield + a gated forced GC.
- **`categoryCount` no longer hardcoded to 1.** Previously poisoned
  the WGSL category uniform whenever `categoryColors.length > 1`,
  rendering all points as a single category.
- **Auto-cleanup of DuckDB spill.** Sessions at this scale spill
  80–120 GB to `$TMPDIR/duckdb_gsa_*`; previously these leaked until
  macOS swept them on reboot. Now wiped on `atexit` and on
  SIGTERM/SIGINT, with stale orphans (>24 h) reclaimed at startup.
- **`Response.arrayBuffer()` ~2 GB ceiling unblocked.** Custom
  `streamingRestConnector` reads the response body chunk-by-chunk,
  bypassing Chrome's fetch-pipeline cap; the wire size limit is now
  V8's, not the fetch pipeline's.
- **u32 packed wire format.** u16 over a world-scale bbox quantised
  to ~5 km — visible coordinate grid at city zoom. u32 quantum is
  ~8 mm; sub-pixel at any zoom level.
- **Metal `MTLCommandBuffer` watchdog at 322 M.** Chunked
  `drawPoints` + WG-local atomic reduction in `viewport_cull` keep
  every command buffer under the macOS 5 s timeout.
- **GPU buffer destroy is now device-lost-safe.** Wrapped in
  `device.queue.onSubmittedWorkDone()` to defer past in-flight
  command buffers; rapid pan-release no longer poisons the
  `MTLDevice`.
- **Tooltip latency at 322 M: ~10 s → ~50 ms.** Background-
  materialise CTAS now appends `ORDER BY` on the precomputed u32
  quantised columns; row-group min/max stats become tight enough to
  prune >99 % of groups for any small-radius tooltip.
- **Frontend-mode side panel unblocks on plain URLs** — no
  `?perf=1` required.

### Changed

- **Electron V8 budget: 12 GB → 16 GB**, with `--expose-gc` so the
  renderer can force a major sweep between releasing the prior
  batch's typed arrays and allocating the next batch's. Pairs with
  the sidecar `memory_limit` clamp (50 % RAM, max 64 GB) to keep
  V8 and DuckDB from contesting RAM.
- **Mosaic LRU query cache disabled** in both `Viewer.svelte` and
  `FileViewer.svelte`. Histogram / count re-fetches on filter
  change cost <50 ms; pinning a 2.5 GB scatter Table for 3 h costs
  the entire ArrayBuffer pool.
- **Render every point at world view** — no automatic cap, no
  sampling. The watchdog is per-buffer, not per-frame; chunking
  splits the work, never drops it.
- **DuckDB `con.sql(...).fetch_arrow_table()`** replaces
  `cursor.execute(...)` for any large arrow export. ~10× faster on
  multi-GB results (the relation API streams; cursor materialises a
  full result-set buffer first).

### Added

- **[`docs/optimization_history.md`](docs/optimization_history.md)** —
  field journal of the 322 M scaling chapter: what worked, what did
  not, common bananaskins, current bottleneck (synchronous
  `Arrow.Vector.toArray()` on the category column at ~1.8 s), and
  prioritised open follow-ups.
- **README** — replaces hardcoded version-pinned download URLs with
  `/releases/latest/download/`, adds a Performance table per distro,
  Troubleshooting (V8 OOM workaround for vanilla Chrome, Metal
  watchdog freeze, stale `viewer-state.json`, leftover spill), and a
  Roadmap section organised by layer.

## v0.0.7 — 2026-04-25

**300 M-row scaling.** The loader, wire transport, and WebGPU renderer
now hold a fluent UX on parquet files with hundreds of millions of
points. Tested end-to-end through the packaged Electron desktop app
on a 300 M-row synthetic Europe parquet (6.6 GiB on disk).

### Performance — 300 M points

| stage                           | result                          |
| ------------------------------- | ------------------------------- |
| `fast_load_parquet` (cold)      | 2.4 s                           |
| Initial scatter (Arrow IPC)     | 9.4 s for 1.20 GB on the wire   |
| Color-by category fetch         | 11.9 s for 1.50 GB on the wire  |
| 10 s scripted pan inside window | 332 CSS-pans : 3 GPU re-renders |
| Zoom re-render                  | 1.0 – 1.5 s                     |

### Changed

- **Renderer:** the downsample / compaction passes are now skipped for
  the entire duration of a drag or zoom gesture (they previously fired
  every ~200 ms during a long pan and ratcheted hundreds of MB of
  intermediate GPU buffers, eventually triggering macOS jetsam at
  100 M+ points). Re-compute happens once on gesture release.
- **Renderer:** the WebGPU accumulate pipeline's Y-stride was bumped
  from 4096 to 65536 so the dispatch axis stays under the 65535 cap
  past 4 billion points. Without this, datasets near 268 M overflowed
  the dispatch and rendered an empty canvas.
- **Loader:** `fast_load_parquet` now pins DuckDB's `memory_limit` and
  `temp_directory` before any heavy SQL, projects the parquet reader's
  `file_row_number` as `__row_index__` at load time (no separate
  `ALTER TABLE … UPDATE` pass), and surfaces `(x, y)` bounds on its
  result so the wire layer can quantise scatter to `u16` (~44 % wire
  savings).
- **Wire:** the FastAPI server now `StreamingResponse`s Arrow IPC
  bodies above 256 MiB in 4 MiB chunks. Uvicorn's default response
  path silently dropped multi-hundred-MB byte payloads at 300 M rows.

### Fixed

- `electron .` (dev launch) no longer mis-detects the literal `"."`
  argument as an initial dataset. `initialDatasetFromArgv()` now also
  requires `isSupportedDataset()`, so the env-var path takes over and
  any future "Open With" of an unsupported file gets a clean error
  from the shell instead of a confused `FileNotFoundError` from inside
  the sidecar.

### Internal / build

- New `scripts/bench/` directory with a DuckDB-backed
  random-points-over-Europe generator, structural validator, per-stage
  loader timer, HTTP+Arrow wire bench, and an end-to-end orchestrator.
- New Playwright projects: `perf-chrome` runs `e2e/europe-300m.spec.ts`
  in real Chrome; `desktop-electron` drives the actual packaged
  Electron shell (sidecar + UI) via `_electron` to assert the same
  CSS-pan-dominates-GPU-rerender contract end-to-end.
- Tag scheme switched from `app-vX.Y.Z` to `vX.Y.Z` so Zenodo's
  GitHub auto-DOI integration archives clean version strings.
  Historical `app-v0.0.1` … `app-v0.0.6` releases are left intact.

## app-v0.0.4 — 2026-04-20

**Shell migration: Tauri → Electron.** The native shell has been
rewritten from the Tauri 2 / WKWebView stack to Electron 41.2.1 /
Chromium 134. Same feature set, same Python sidecar, same 5-artifact
matrix (`.dmg`, `.deb`, `.rpm`, `.msi`, NSIS `.exe`) — but the renderer
now runs on the same Chromium + V8 + Dawn stack as the user's browser,
so WebGPU scatter performance matches `chrome.google.com` exactly.

### Why

Profiling the v0.0.3 macOS build against Chrome on the same hardware
(5 M points dataset) showed pan/zoom noticeably slower in the .app
than in Chrome, despite both using Metal-backed WebGPU. The gap
traced to the embedded webview: WKWebView runs MapLibre's per-frame
JavaScript on JavaScriptCore (vs V8 in Chrome), issues WebGL draw
calls through a less-tuned Metal bridge than Chromium's ANGLE, and
composites the canvas through Core Animation rather than Chromium's
Viz compositor. Switching to Electron collapses all three gaps; a
5 M-point dataset now pans at Chrome-native framerate.

### Changed

- **Shell:** Tauri 2.2 Rust binary → Electron 41 main process
  (TypeScript). Bundle size goes from ~500 MB (Tauri + sidecar) to
  ~800 MB (Electron + sidecar). Electron pays for itself in perf.
- **User state migration:** viewer state is still stored at
  `{appData}/io.github.do-me.geospatial-atlas/viewer-state.json`, so
  existing per-dataset view states from v0.0.1–v0.0.3 carry over.
- **Linux runtime deps** (`.deb` `Depends`): swapped WebKitGTK / GTK+3 /
  appindicator for the standard Chromium runtime set (`libgtk-3-0`,
  `libnss3`, `libxss1`, `libxtst6`, `libsecret-1-0`, `libatspi2.0-0`,
  `libnotify4`, `xdg-utils`, `libuuid1`). Most distros already have
  these because they're what Chrome / Edge / Firefox pull in.
- **CI:** dropped Rust for the shell (kept for density-clustering /
  UMAP WASM). Dropped `libwebkit2gtk-4.1-dev` + friends from the Linux
  runner. Build is ~35 % faster end-to-end.

### Fixed

- v0.0.3 macOS: on WKWebView, MapLibre's base-map canvas and the
  embedding-atlas overlay both ran slower than Chrome's equivalent.
  Migration to Chromium eliminates the gap.
- File-picker + drag-drop now use Electron's native `dialog` and HTML5
  `DataTransfer.files` path (via `webUtils.getPathForFile`) instead of
  Tauri's `plugin-dialog`. Behaviour is identical.

### Internal / build

- `apps/desktop/src-tauri/` removed. Icons relocated to
  `apps/desktop/icons/` (referenced by `electron-builder.yml`).
- `apps/desktop/electron/` — new main + preload + entitlements.
- `apps/desktop/electron-builder.yml` — packaging config for all 5
  targets; ad-hoc signing on macOS arm64 (same as before).
- `vite.config.js` — `base: "./"` so Vite emits relative asset URLs
  that resolve under the asar when the renderer loads via `file://`.

### Known issues

- Still unsigned everywhere. macOS Gatekeeper shows the "damaged"
  message; strip quarantine with `xattr -cr "/Applications/Geospatial Atlas.app"`.
  Windows SmartScreen shows "unrecognized publisher"; More info →
  Run anyway. Linux has no Gatekeeper equivalent.
- macOS Intel still not shipped (runner queue issue, same as v0.0.2+).
  Intel-Mac users: `uv run geospatial-atlas ...`.

---

## app-v0.0.1 — 2026-04-18

**First desktop release.** Native macOS app (Apple Silicon + Intel),
with Linux / Windows builds configured but not yet shipped. iOS / Android
tracked separately in [`docs/MOBILE.md`](docs/MOBILE.md).

### Added

- **Native macOS app** — Tauri 2 shell + PyInstaller Python sidecar.
  Bundle ≈ 490 MB (DuckDB, pyarrow, FastAPI, uvicorn all included).
- **GeoParquet fast path** — DuckDB `ST_X`/`ST_Y` over native `GEOMETRY` or
  WKB BLOB columns. 75 M-row / 14 GB Overture file loads in ~5 s warm.
- **Live load progress** — stage + percentage bar driven by DuckDB's
  `query_progress()`, polled from a worker thread.
- **Row limit** — SQL `LIMIT` pushdown to the parquet reader; glimpse
  1 000 rows of a 14 GB file in ~1 s.
- **Text column selector** — mirrors the `--text` CLI flag for tooltips
  and search.
- **WebGPU probe** — the viewer surfaces a dismissible banner when WebGPU
  is unavailable (benefits all three distros via `packages/viewer`).
- **OpenFreeMap attribution** — shown in the status bar whenever an
  OpenFreeMap basemap style is active (benefits all three distros via
  `packages/component`).
- **Per-dataset state persistence** — the URL hash (zoom, filters,
  selection) is auto-saved to
  `~/Library/Application Support/io.github.do-me.geospatial-atlas/`
  and restored on next open of the same file.
- **Home button** — inline icon in the viewer toolbar to return to the
  dataset picker (app-only UX, wired via injected JS).
- **Drag-and-drop** — drop a supported file anywhere on the window to
  load it; works on the home screen and over a running viewer.
- **Cross-platform release pipeline** — GitHub Actions matrix over
  macOS arm64, macOS x64, Linux x64, Windows x64.
  Tag `app-v*` to cut a draft release.

### Sibling-distro changes

Per the multi-distro convention, several desktop features required
upstream work:

- `packages/backend/embedding_atlas/fast_load.py` — the DuckDB-native
  loader used by both the desktop sidecar and the Python CLI
  (`geospatial-atlas` command auto-selects it for single-parquet GIS
  files).
- `packages/backend/embedding_atlas/server.py` — accepts an optional
  pre-built `duckdb_connection`, skipping the pandas materialization.
- `packages/viewer` — WebGPU banner + OpenFreeMap-aware FileViewer for
  native `GEOMETRY` columns.
- `packages/component` — `StatusBar.svelte` grew a `basemapAttribution`
  prop.

### Known limitations

- **Unsigned build.** First launch:
  - macOS: right-click → Open, or System Settings → Privacy & Security
    → "Open Anyway".
  - Windows: SmartScreen warning → _More info → Run anyway_.
  - Linux: no prompt; `chmod +x` the `.AppImage` before launching.
- **Only Apple Silicon has been smoke-tested.** The x64 / Linux /
  Windows builds are produced by CI but have not been manually verified.
- **No iOS / Android app.** Mobile needs a frontend-only WASM build;
  plan lives in `docs/MOBILE.md`.
- **First launch is slow (~15–20 s)** on a cold filesystem cache while
  PyInstaller unpacks 486 MB of native libs. Subsequent launches are
  ~2–3 s thanks to the macOS dyld cache.
