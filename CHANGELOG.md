# Changelog

All notable changes to the Geospatial Atlas desktop app are documented here.

Desktop-app releases are tagged `vX.Y.Z` (from v0.0.7 onward; earlier
releases used the `app-vX.Y.Z` prefix). Other streams (Python package,
static web viewer) have their own changelogs / tag prefixes — see
[`docs/RELEASING.md`](docs/RELEASING.md).

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
  - Windows: SmartScreen warning → *More info → Run anyway*.
  - Linux: no prompt; `chmod +x` the `.AppImage` before launching.
- **Only Apple Silicon has been smoke-tested.** The x64 / Linux /
  Windows builds are produced by CI but have not been manually verified.
- **No iOS / Android app.** Mobile needs a frontend-only WASM build;
  plan lives in `docs/MOBILE.md`.
- **First launch is slow (~15–20 s)** on a cold filesystem cache while
  PyInstaller unpacks 486 MB of native libs. Subsequent launches are
  ~2–3 s thanks to the macOS dyld cache.
