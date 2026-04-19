# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the Geospatial Atlas Tauri sidecar.
#
# Build with:
#   uv run --with pyinstaller pyinstaller sidecar.spec --noconfirm --distpath ./build
#
# Produces a --onedir bundle at ./build/geospatial-atlas-sidecar/.
# The Tauri build script then renames the inner binary to the per-target
# triple that Tauri's externalBin expects.

from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata

HERE = Path(SPECPATH).resolve()
REPO = HERE.parent.parent
BACKEND = REPO / "packages" / "backend"

datas = []
binaries = []
hiddenimports = []

# DuckDB: ship native .dylib + extensions (if preinstalled into the dir)
_duckdb_data, _duckdb_bin, _duckdb_hidden = collect_all("duckdb")
datas += _duckdb_data
binaries += _duckdb_bin
hiddenimports += _duckdb_hidden

# pyarrow ships large C++ libraries we must include
_pa_data, _pa_bin, _pa_hidden = collect_all("pyarrow")
datas += _pa_data
binaries += _pa_bin
hiddenimports += _pa_hidden

# Pandas + io engines
_pd_data, _pd_bin, _pd_hidden = collect_all("pandas")
datas += _pd_data
binaries += _pd_bin
hiddenimports += _pd_hidden

# fastparquet fallback engine
_fp_data, _fp_bin, _fp_hidden = collect_all("fastparquet")
datas += _fp_data
binaries += _fp_bin
hiddenimports += _fp_hidden

# FastAPI / Starlette / pydantic and the MCP stack.
# mcp pulls in jsonschema → rfc3987_syntax, which ships a .lark grammar
# data file next to its .py modules; without collect_all PyInstaller
# skips that file and `from mcp import types` crashes at import time
# (FileNotFoundError on syntax_rfc3987.lark). jsonschema_specifications
# and referencing similarly ship .json schema resources.
for mod in (
    "fastapi",
    "starlette",
    "pydantic",
    "pydantic_core",
    "anyio",
    "sniffio",
    "mcp",
    "jsonschema",
    "jsonschema_specifications",
    "referencing",
    "rfc3987_syntax",
    "sse_starlette",
):
    try:
        d, b, h = collect_all(mod)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass

# Some packages call importlib.metadata.version(pkg) at import time and crash
# without their dist-info. Copy the metadata for packages known to need it.
for _mdpkg in (
    "readchar",
    "inquirer",
    "blessed",
    "editor",
    "runs",
    "xmod",
    "uvicorn",
    "fastapi",
    "pydantic",
    "starlette",
    "duckdb",
    "pyarrow",
    "numpy",
    "pandas",
    "click",
    "h11",
    "anyio",
    "sniffio",
    "typing_extensions",
    "websockets",
    "cryptography",
    "narwhals",
    "platformdirs",
):
    try:
        datas += copy_metadata(_mdpkg)
    except Exception:
        pass

# uvicorn implicit imports
hiddenimports += [
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.lifespan.on",
    "uvicorn.logging",
    "h11",
    "websockets",
    "websockets.legacy",
]
hiddenimports += collect_submodules("uvicorn")

# Our app package — ship the bundled static viewer with it
_ea_data, _ea_bin, _ea_hidden = collect_all("embedding_atlas")
datas += _ea_data
binaries += _ea_bin
hiddenimports += _ea_hidden

# Explicit exclusions to keep the bundle lean.
# The desktop GIS viewer does not generate embeddings — users who want that
# should use the Python CLI. This strips ~2 GB from the bundle.
excludes = [
    "torch",
    "transformers",
    "sentence_transformers",
    "accelerate",
    "huggingface_hub",
    "datasets",
    "tokenizers",
    "safetensors",
    "onnxruntime",
    "sklearn",
    "scipy",
    "numba",
    "llvmlite",
    "umap",
    "tensorflow",
    "litellm",
    "tensorboard",
    "matplotlib",
    "streamlit",
    "anywidget",
    "jupyter",
    "jupyterlab",
    "notebook",
    "IPython",
    "ipykernel",
    "ipywidgets",
    "pytest",
    "pyright",
    "PIL",
]

block_cipher = None

a = Analysis(
    [str(HERE / "sidecar.py")],
    pathex=[str(BACKEND)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="geospatial-atlas-sidecar",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="geospatial-atlas-sidecar",
)
