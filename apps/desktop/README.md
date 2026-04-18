# Geospatial Atlas — native macOS app

A Tauri 2 wrapper that ships the FastAPI backend and the Svelte viewer as a
single native macOS application.

## Architecture

```
┌──────────────────────── .app ─────────────────────────┐
│ Contents/MacOS/                                        │
│   geospatial-atlas-mac         ← Rust / Tauri shell    │
│   geospatial-atlas-sidecar     ← shell stub            │
│ Contents/Resources/                                    │
│   sidecar/                     ← PyInstaller --onedir  │
│     geospatial-atlas-sidecar   ← real Python binary    │
│     _internal/                 ← duckdb, pyarrow, etc. │
│       embedding_atlas/static/  ← prebuilt Svelte viewer│
└────────────────────────────────────────────────────────┘
```

Lifecycle:

1. Tauri launches the Rust shell → opens a WKWebView with the Svelte
   bootstrap UI (`src/App.svelte`).
2. User picks a dataset (or passes one via argv / `GEOSPATIAL_ATLAS_INITIAL_DATASET`).
3. Rust picks a free TCP port via `portpicker`, spawns the sidecar with
   `GEOSPATIAL_ATLAS_HOST`, `GEOSPATIAL_ATLAS_PORT`, and
   `GEOSPATIAL_ATLAS_PARENT_PID` in the environment.
4. The sidecar (PyInstaller `--onedir`) loads the dataset, auto-detects
   GIS columns, starts FastAPI + uvicorn.
5. Rust polls `/data/metadata.json` and emits a `sidecar-ready` event; the
   Svelte UI redirects the webview to `http://127.0.0.1:<PORT>`.
6. On Cmd+Q (`RunEvent::ExitRequested`) the Rust shell kills the sidecar.
   As belt-and-suspenders the sidecar also runs three parallel watchdogs:
   stdin-EOF, `kqueue NOTE_EXIT`, and a getppid poll.

## Prerequisites

- macOS 11 or later
- Rust / `cargo` (2024 edition)
- Node 20+
- `uv` (for the backend venv)
- `cargo-tauri` CLI: `cargo install tauri-cli --version "^2.0"`

## Build from source

```bash
cd apps/desktop

# 1. Install JS deps
npm install

# 2. Build the Svelte viewer for the backend (required — provides
#    embedding_atlas/static bundled into the sidecar)
cd ../packages/backend && ./build.sh && cd ../../apps/desktop

# 3. Build the PyInstaller sidecar for the host arch
npm run build:sidecar

# 4. Build the .app + .dmg
npm run build
```

Artifacts land in:

- `src-tauri/target/release/bundle/macos/Geospatial Atlas.app`
- `src-tauri/target/release/bundle/dmg/Geospatial Atlas_<version>_aarch64.dmg`

## Running

**From the Finder / Dock:** double-click the `.app`. The file picker opens;
choose any Parquet/CSV/GeoParquet file that has `lon`/`lat` columns or a
WKB `geometry` column.

**From the command line with a dataset pre-loaded:**

```bash
"src-tauri/target/release/bundle/macos/Geospatial Atlas.app/Contents/MacOS/geospatial-atlas-mac" \
  /path/to/your.parquet
```

Or equivalently with `open`:

```bash
open -a "Geospatial Atlas" --args /path/to/your.parquet
```

A sample test dataset is bundled in the repo at
`e2e/.data/dataset_test.parquet` (333 843 European school records).

## Dev loop (hot reload)

```bash
# Make sure the sidecar binary exists at least once
npm run build:sidecar

# Then live-reload the Svelte UI and auto-rebuild Rust
npm run dev
```

## Cross-compiling for x86_64

```bash
# In a Rosetta shell (arch -x86_64 zsh)
TARGET_TRIPLE=x86_64-apple-darwin npm run build:sidecar:x86_64
# then from normal shell
cargo tauri build --target x86_64-apple-darwin
```

Ship two DMGs rather than a universal2 bundle — the DuckDB / PyArrow
wheel machinery makes true universal2 a multi-day engineering task with
no user-visible win.

## Signing / notarization

For distribution beyond a developer machine, set:

```bash
export APPLE_SIGNING_IDENTITY="Developer ID Application: Your Name (TEAMID)"
export APPLE_ID="you@example.com"
export APPLE_APP_SPECIFIC_PASSWORD="xxxx-xxxx-xxxx-xxxx"
```

and run `npm run build`. Tauri signs the entire bundle (including every
`.dylib` under `Contents/Resources/sidecar/`). The `entitlements.plist`
already contains the three flags required for a bundled Python
interpreter under the hardened runtime:

- `com.apple.security.cs.allow-jit`
- `com.apple.security.cs.allow-unsigned-executable-memory`
- `com.apple.security.cs.disable-library-validation`

## Known limitations

- First launch takes ~8–12 s while PyInstaller unpacks native libs and
  Python cold-imports pyarrow + DuckDB. Subsequent launches are ~2–3 s
  because of macOS's dyld cache.
- Embedding projection (UMAP) is *not* bundled — this build is a GIS
  viewer. Install the Python CLI (`uv tool install embedding-atlas`) if
  you need that pipeline.
- Unsigned builds require right-click → Open the first time (Gatekeeper
  quarantine). Signed + notarized builds open normally.
