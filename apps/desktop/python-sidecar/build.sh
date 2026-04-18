#!/usr/bin/env bash
# Build the PyInstaller --onedir sidecar for the Tauri desktop app (macOS + Linux).
#
# Produces:
#   python-sidecar/dist/geospatial-atlas-sidecar/      (onedir PyInstaller output)
#   apps/desktop/resources/sidecar/                          (copy that gets bundled
#                                                        into the .app/.appimage/etc.
#                                                        via tauri.conf.json `resources`)
#
# The Rust shell resolves the binary at runtime via ``app.path().resource_dir()``,
# so there is no externalBin or shell-stub indirection any more.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "$HERE/.." && pwd)"
REPO="$(cd "$APP_DIR/../.." && pwd)"
BACKEND="$REPO/packages/backend"

echo "[build-sidecar] host: $(uname -s) $(uname -m)"
echo "[build-sidecar] backend: $BACKEND"

# 1. Viewer static assets (bundled into the sidecar package via fast_load's
#    resolution of embedding_atlas/static).
if [ ! -f "$BACKEND/embedding_atlas/static/index.html" ]; then
  echo "[build-sidecar] viewer static missing — running backend/build.sh"
  (cd "$BACKEND" && ./build.sh)
fi

# 2. Run PyInstaller in an ephemeral env so it doesn't touch the user's
#    pyproject.toml.
BUILD_DIR="$HERE/build"
DIST_DIR="$HERE/dist"
rm -rf "$BUILD_DIR" "$DIST_DIR"
mkdir -p "$BUILD_DIR" "$DIST_DIR"

echo "[build-sidecar] running pyinstaller…"
(
  cd "$HERE"
  uv run \
    --project "$BACKEND" \
    --with "pyinstaller>=6.10" \
    pyinstaller sidecar.spec \
      --noconfirm \
      --distpath "$DIST_DIR" \
      --workpath "$BUILD_DIR"
)

ONEDIR="$DIST_DIR/geospatial-atlas-sidecar"
INNER_BIN="$ONEDIR/geospatial-atlas-sidecar"

if [ ! -x "$INNER_BIN" ]; then
  echo "[build-sidecar] ERROR: expected binary at $INNER_BIN" >&2
  exit 1
fi

# 3. Mirror the onedir into resources/ so Tauri bundles it under
#    Contents/Resources/sidecar/ (macOS) or ${resource_dir}/sidecar/
#    (Linux, Windows).
RESOURCES="$APP_DIR/resources"
rm -rf "$RESOURCES/sidecar"
mkdir -p "$RESOURCES/sidecar"
cp -R "$ONEDIR/." "$RESOURCES/sidecar/"

echo "[build-sidecar] OK"
echo "  onedir: $ONEDIR"
echo "  bundled into: $RESOURCES/sidecar"
