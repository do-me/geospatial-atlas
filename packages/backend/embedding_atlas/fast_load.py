"""DuckDB-native fast-path loader for GIS parquet files.

For single-parquet inputs with either a lon/lat pair or a WKB geometry
column, this loader materialises the file into a DuckDB TABLE via a
single ``CREATE TABLE ... AS SELECT`` that pulls the rows in once and
projects the parquet reader's ``file_row_number`` virtual column under
``__row_index__`` — so the viewer never has to do its own
``ALTER TABLE ... ADD COLUMN`` + ``UPDATE`` rowid pass.

Why a TABLE and not a VIEW: the viewer's ``makeCategoryColumn`` (color
by) issues ``ALTER TABLE dataset ADD COLUMN ...`` + ``UPDATE`` on every
color-by click. DuckDB only allows ``ALTER VIEW`` on views, so a
view-backed dataset hard-fails the moment the user picks a color
column. A 322 M-row materialisation takes ~75 s on a 16-core box and
each subsequent ALTER+UPDATE is ~9 s — slower than a view at startup,
but functional through the full feature surface.

The previous CTAS path crashed on >~10 GB parquet because DuckDB's
defaults are ``memory_limit = 80% RAM`` (no headroom) and
``temp_directory = '.tmp'`` (a relative path that often can't be
created from the user's cwd). When the working set spilled past the
limit it had nowhere to go and the process OOM-killed mid-load. We now
set ``memory_limit`` to a generous-but-bounded fraction of system RAM
and pin ``temp_directory`` to the OS tmp dir so spilling always has a
home.

Used by:
  * ``apps/desktop`` sidecar — always.
  * ``packages/backend`` CLI (``geospatial-atlas``) — auto-selected when
    inputs are a single Parquet with no ``--query``/``--sample`` and
    no embedding generation is requested.
  * Frontend-only / static distros — unaffected; they use DuckDB-WASM
    directly in the browser.
"""

from __future__ import annotations

import json
import os
import pathlib
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Callable, Literal

import duckdb


def _safe_memory_temp_settings() -> tuple[str, str]:
    """Return ``(memory_limit, temp_directory)`` strings safe to apply to
    a fresh ``:memory:`` connection.

    The DuckDB defaults (``80% RAM`` limit, ``.tmp`` relative temp path)
    are the actual cause of large-file CREATE TABLE crashes: spilling
    has no usable scratch dir and no headroom over the OS.
    """
    try:
        total_ram = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError, AttributeError):
        total_ram = 16 * 1024**3  # conservative fallback (16 GiB)
    # 50 % of system RAM, capped at 64 GiB. Leaves headroom for the
    # parquet reader, FastAPI workers, the OS file cache, and the
    # ALTER+UPDATE pass the viewer triggers on every "color by" click.
    limit_bytes = min(int(total_ram * 0.5), 64 * 1024**3)
    limit_str = f"{limit_bytes // (1024**3)}GB"
    # OS tmp dir always exists and has plenty of room; per-process subdir
    # so we never collide with another sidecar / CLI invocation.
    temp_dir = tempfile.mkdtemp(prefix="duckdb_gsa_")
    return limit_str, temp_dir


ProgressCallback = Callable[[str, float, str], None]
"""Receives (stage, percent, detail). ``stage`` is a short slug; ``detail``
is a user-facing one-liner. ``percent`` is 0..100 or -1 if unknown."""


@dataclass
class FastLoadResult:
    connection: duckdb.DuckDBPyConnection
    table: str
    row_count: int
    x_column: str
    y_column: str
    # Stable per-row id, picked here so callers don't need a separate
    # ALTER TABLE + UPDATE pass — that pass would rewrite the whole
    # (multi-GB) view-backing parquet on every load.
    id_column: str
    columns: list[str]
    duration_seconds: float
    # Axis-aligned bounding box over (x_column, y_column). ``None`` if the
    # bounds query raised (e.g. spatial-extension edge cases on exotic
    # geometries). Consumers use these to quantize coordinates for the
    # wire — the frontend sends a MIN-MAX linear map to pack f32 → u16.
    x_bounds: tuple[float, float] | None = None
    y_bounds: tuple[float, float] | None = None


def _detect_columns(
    con: duckdb.DuckDBPyConnection, path: str
) -> tuple[Literal["xy", "geometry"], tuple[str, str] | str, list[str], dict[str, str]]:
    """Return (kind, info, all_columns, col_types).

    ``info`` is (x,y) column names or the geometry column name.
    ``col_types`` maps column name to its DuckDB type string.
    """
    # DESCRIBE doesn't require reading the full file, just the parquet footer.
    schema = con.sql(
        f"SELECT column_name, column_type FROM (DESCRIBE SELECT * FROM read_parquet({duckdb_literal(path)}) LIMIT 0)"
    ).fetchall()
    cols = [r[0] for r in schema]
    col_types = {r[0]: r[1] for r in schema}
    cols_lower = {c.lower(): c for c in cols}

    for xc, yc in [("longitude", "latitude"), ("lon", "lat"), ("lng", "lat"), ("x", "y")]:
        if xc in cols_lower and yc in cols_lower:
            return "xy", (cols_lower[xc], cols_lower[yc]), cols, col_types

    for cand in ["geometry", "geom", "wkb_geometry", "the_geom", "geo"]:
        if cand in cols_lower:
            return "geometry", cols_lower[cand], cols, col_types

    raise ValueError(
        "No GIS columns detected. Expected a lon/lat pair (e.g. "
        "longitude/latitude, lon/lat, x/y) or a WKB geometry / GEOMETRY "
        "column (geometry, geom, wkb_geometry, the_geom). Columns present: "
        + ", ".join(cols)
    )


def duckdb_literal(s: str) -> str:
    """Quote a path as a DuckDB string literal (escapes single quotes)."""
    return "'" + s.replace("'", "''") + "'"


def fast_load_parquet(
    path: str,
    *,
    table: str = "dataset",
    threads: int | None = None,
    enable_spatial: bool | None = None,
    limit: int | None = None,
    progress: ProgressCallback | None = None,
) -> FastLoadResult:
    """Expose a parquet file as a DuckDB VIEW on a fresh ``:memory:`` connection.

    The view wraps ``read_parquet(path, file_row_number=true)`` so that
    DuckDB scans only the column chunks each query touches and never
    materialises the full table. This is what lets a 15.6 GB / 322 M-row
    file load instantly on a 16 GB machine — the previous
    ``CREATE TABLE AS SELECT *`` path needed 50–100 GB of RAM (or a
    tens-of-GB temp spill) for wide string-heavy schemas.

    The reader's virtual ``file_row_number`` column gives every row a
    stable id; we expose it under a unique alias so callers don't need a
    separate ``ALTER TABLE`` + ``UPDATE`` rewrite pass.

    If ``limit`` is set, the LIMIT lives inside the view definition.
    DuckDB pushes it down to the parquet reader, so glimpsing 1 000 rows
    of a 14 GB file takes milliseconds.

    Handles both legacy BLOB/WKB geometry columns and modern GeoParquet
    files where DuckDB surfaces the column as native ``GEOMETRY`` — the
    latter doesn't need an ``ST_GeomFromWKB`` wrapper.
    """
    t_start = time.perf_counter()
    fn = pathlib.Path(path)
    if not fn.is_file():
        raise FileNotFoundError(path)
    if limit is not None and limit <= 0:
        limit = None

    emit = progress or (lambda *_: None)

    con = duckdb.connect(":memory:")
    # Pin memory_limit + temp_directory before any heavy SQL runs so that
    # ``CREATE TABLE`` always has somewhere to spill on ≥10 GB inputs.
    mem_limit, temp_dir = _safe_memory_temp_settings()
    con.sql(f"SET memory_limit = '{mem_limit}'")
    con.sql(f"SET temp_directory = '{temp_dir}'")
    if threads:
        con.sql(f"PRAGMA threads={int(threads)}")
    # Required to make query_progress() return anything useful.
    con.sql("SET enable_progress_bar=true")
    con.sql("SET enable_progress_bar_print=false")

    # Load spatial first so that DESCRIBE surfaces GEOMETRY columns
    # correctly for GeoParquet 1.1 files. DuckDB 1.4+ surfaces the
    # column as ``GEOMETRY('OGC:CRS84')`` even without spatial loaded,
    # but ``ST_X`` / ``ST_Y`` obviously require the extension.
    spatial_loaded = False
    if enable_spatial is not False:
        try:
            emit("spatial", 5.0, "Loading DuckDB spatial extension")
            con.sql("INSTALL spatial")
            con.sql("LOAD spatial")
            spatial_loaded = True
        except Exception:
            if enable_spatial is True:
                raise

    emit("analyze", 8.0, f"Opening {fn.name}")
    kind, info, columns, col_types = _detect_columns(con, str(path))

    # Pick a stable id-column name that doesn't collide with the parquet
    # schema. We project the reader's ``file_row_number`` virtual column
    # under this alias.
    id_column = _unused_name(columns, "__row_index__")

    # If the parquet itself has a column literally named ``file_row_number``,
    # ``read_parquet(.., file_row_number=true)`` would collide on the
    # output schema. Fall back to a window-function row id in that case.
    has_frn_collision = any(c.lower() == "file_row_number" for c in columns)
    if has_frn_collision:
        read = f"read_parquet({duckdb_literal(str(path))})"
        id_expr = "ROW_NUMBER() OVER ()"
        passthrough = "*"
    else:
        read = f"read_parquet({duckdb_literal(str(path))}, file_row_number=true)"
        id_expr = "file_row_number"
        # Drop the virtual column from SELECT * so we don't surface it
        # twice (once raw, once aliased).
        passthrough = "* EXCLUDE (file_row_number)"

    limit_clause = f" LIMIT {int(limit)}" if limit is not None else ""

    if kind == "xy":
        x_col, y_col = info  # type: ignore[misc]
        x_out, y_out = x_col, y_col
        select = (
            f"SELECT {passthrough}, "
            f"{id_expr} AS {quote_ident(id_column)} "
            f"FROM {read}{limit_clause}"
        )
        detail = f"Using columns {x_col} / {y_col}"
    else:
        geom = str(info)  # type: ignore[arg-type]
        geom_type = col_types.get(geom, "").upper()
        is_native_geometry = geom_type.startswith("GEOMETRY")
        if is_native_geometry and not spatial_loaded:
            raise RuntimeError(
                "File uses native GEOMETRY column but DuckDB spatial extension "
                "could not be loaded (is this machine offline on first run?)"
            )
        # Reserve lon/lat names if they're not already taken.
        x_out = _unused_name(columns, "lon")
        y_out = _unused_name(columns + [x_out], "lat")
        geom_expr = (
            quote_ident(geom)
            if is_native_geometry
            else f"ST_GeomFromWKB({quote_ident(geom)})"
        )
        select = (
            f"SELECT {passthrough}, "
            f"ST_X({geom_expr}) AS {quote_ident(x_out)}, "
            f"ST_Y({geom_expr}) AS {quote_ident(y_out)}, "
            f"{id_expr} AS {quote_ident(id_column)} "
            f"FROM {read}{limit_clause}"
        )
        detail = (
            f"Extracting coordinates from {geom} "
            f"({'native GEOMETRY' if is_native_geometry else 'WKB'})"
        )

    # Materialise into a TABLE (not a VIEW). The viewer's color-by
    # path issues ``ALTER TABLE ... ADD COLUMN`` + ``UPDATE`` against
    # this name on every selection — DuckDB rejects both against a
    # view, so a view-backed dataset hard-fails the user's first click.
    sql = f"CREATE OR REPLACE TABLE {quote_ident(table)} AS {select}"
    emit("load", 10.0, detail)
    _run_with_progress(con, sql, emit)

    emit("count", 80.0, "Counting rows")
    row_count = con.sql(f"SELECT COUNT(*) FROM {quote_ident(table)}").fetchone()[0]

    emit("bounds", 90.0, "Computing bounding box")
    x_bounds: tuple[float, float] | None = None
    y_bounds: tuple[float, float] | None = None
    try:
        row = con.sql(
            f"SELECT MIN({quote_ident(x_out)}), MAX({quote_ident(x_out)}), "
            f"MIN({quote_ident(y_out)}), MAX({quote_ident(y_out)}) "
            f"FROM {quote_ident(table)}"
        ).fetchone()
        if row is not None and all(v is not None for v in row):
            x_min, x_max, y_min, y_max = (float(v) for v in row)
            if x_max > x_min:
                x_bounds = (x_min, x_max)
            if y_max > y_min:
                y_bounds = (y_min, y_max)
    except Exception:
        pass

    emit("ready", 100.0, f"Loaded {row_count:,} rows")
    return FastLoadResult(
        connection=con,
        table=table,
        row_count=row_count,
        x_column=x_out,
        y_column=y_out,
        id_column=id_column,
        columns=columns + [c for c in (x_out, y_out, id_column) if c not in columns],
        duration_seconds=time.perf_counter() - t_start,
        x_bounds=x_bounds,
        y_bounds=y_bounds,
    )


def _run_with_progress(
    con: duckdb.DuckDBPyConnection,
    sql: str,
    emit: ProgressCallback,
) -> None:
    """Run ``sql`` on ``con`` in a worker thread and forward DuckDB's own
    progress percentage to ``emit`` every ~300 ms.

    DuckDB's ``query_progress()`` is safe to call from another thread
    while the connection is executing a query (verified with DuckDB 1.5).
    """
    err: list[BaseException] = []

    def _worker() -> None:
        try:
            con.execute(sql)
        except BaseException as e:
            err.append(e)

    t = threading.Thread(target=_worker, name="fast-load-worker", daemon=True)
    t.start()
    last_pct = -1.0
    while t.is_alive():
        try:
            pct = con.query_progress()
        except Exception:
            pct = -1.0
        if pct is not None and pct >= 0 and abs(pct - last_pct) >= 0.5:
            emit("load", float(pct), "")
            last_pct = float(pct)
        time.sleep(0.25)
    t.join()
    if err:
        raise err[0]


def _unused_name(existing: list[str], candidate: str) -> str:
    lower = {c.lower() for c in existing}
    if candidate.lower() not in lower:
        return candidate
    i = 1
    while f"{candidate}_{i}".lower() in lower:
        i += 1
    return f"{candidate}_{i}"


def quote_ident(s: str) -> str:
    return '"' + s.replace('"', '""') + '"'


def progress_line(stage: str, percent: float, detail: str) -> str:
    """Serialize a progress event for pipe-based transport (Rust parses this)."""
    return "GSA_PROGRESS " + json.dumps({"stage": stage, "percent": percent, "detail": detail})
