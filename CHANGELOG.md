# Changelog

All notable changes to the Geospatial Atlas desktop app are documented here.

Desktop-app releases are tagged `app-vX.Y.Z`. Other streams (Python package,
static web viewer) have their own changelogs / tag prefixes — see
[`docs/RELEASING.md`](docs/RELEASING.md).

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
