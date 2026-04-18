# PowerShell build script for the Geospatial Atlas sidecar on Windows.
# Mirrors python-sidecar/build.sh but targets Windows conventions.

$ErrorActionPreference = "Stop"

$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$AppDir = Split-Path -Parent $Here
$Repo = Split-Path -Parent (Split-Path -Parent $AppDir)
$Backend = Join-Path $Repo "packages\backend"

Write-Host "[build-sidecar] host: Windows $env:PROCESSOR_ARCHITECTURE"
Write-Host "[build-sidecar] backend: $Backend"

# 1. Viewer static assets
if (-not (Test-Path (Join-Path $Backend "embedding_atlas\static\index.html"))) {
    Write-Host "[build-sidecar] viewer static missing — running backend build"
    Push-Location $Backend
    # build.sh requires bash; fall back to manual steps.
    uv sync --frozen
    Push-Location (Join-Path $Repo "packages\viewer")
    npm install
    npm run build
    Pop-Location
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue `
        (Join-Path $Backend "embedding_atlas\static"), `
        (Join-Path $Backend "embedding_atlas\widget_static")
    Copy-Item -Recurse `
        (Join-Path $Repo "packages\viewer\dist") `
        (Join-Path $Backend "embedding_atlas\static")
    uv build --wheel
    Pop-Location
}

$BuildDir = Join-Path $Here "build"
$DistDir  = Join-Path $Here "dist"
Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $BuildDir, $DistDir
New-Item -ItemType Directory -Force $BuildDir, $DistDir | Out-Null

Write-Host "[build-sidecar] running pyinstaller…"
Push-Location $Here
uv run `
    --project $Backend `
    --with "pyinstaller>=6.10" `
    pyinstaller sidecar.spec `
        --noconfirm `
        --distpath $DistDir `
        --workpath $BuildDir
Pop-Location

$OneDir  = Join-Path $DistDir "geospatial-atlas-sidecar"
$InnerBin = Join-Path $OneDir "geospatial-atlas-sidecar.exe"

if (-not (Test-Path $InnerBin)) {
    Write-Error "expected binary at $InnerBin"
    exit 1
}

$Resources = Join-Path $AppDir "resources"
Remove-Item -Recurse -Force -ErrorAction SilentlyContinue (Join-Path $Resources "sidecar")
New-Item -ItemType Directory -Force (Join-Path $Resources "sidecar") | Out-Null
Copy-Item -Recurse -Force (Join-Path $OneDir "*") (Join-Path $Resources "sidecar")

Write-Host "[build-sidecar] OK"
Write-Host "  onedir: $OneDir"
Write-Host "  bundled into: $(Join-Path $Resources 'sidecar')"
