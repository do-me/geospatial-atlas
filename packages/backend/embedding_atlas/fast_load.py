"""DuckDB-native fast-path loader for GIS parquet files.

For single-parquet inputs with either a lon/lat pair or a WKB geometry
column, this loader replaces the pandas + Python ``apply`` pipeline with
a single DuckDB ``CREATE TABLE ... AS SELECT`` that reads the parquet
and (when needed) extracts coordinates via ``ST_X``/``ST_Y``. On a
75 M-row / 4 GB file this cuts load time from ~15+ minutes to ~5 seconds.

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
import pathlib
import threading
import time
from dataclasses import dataclass
from typing import Callable, Literal

import duckdb


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
    columns: list[str]
    duration_seconds: float


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
    """Load a parquet file into a fresh DuckDB :memory: connection.

    If ``limit`` is set, only the first ``limit`` rows are materialised.
    DuckDB pushes the LIMIT down to the parquet reader, so glimpsing
    1 000 rows of a 14 GB file takes milliseconds.

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

    limit_clause = f" LIMIT {int(limit)}" if limit is not None else ""
    read = f"read_parquet({duckdb_literal(str(path))})"

    if kind == "xy":
        x_col, y_col = info  # type: ignore[misc]
        x_out, y_out = x_col, y_col
        select = f"SELECT * FROM {read}{limit_clause}"
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
        y_out = _unused_name(columns, "lat")
        geom_expr = (
            quote_ident(geom)
            if is_native_geometry
            else f"ST_GeomFromWKB({quote_ident(geom)})"
        )
        select = (
            f"SELECT *, "
            f"ST_X({geom_expr}) AS {quote_ident(x_out)}, "
            f"ST_Y({geom_expr}) AS {quote_ident(y_out)} "
            f"FROM {read}{limit_clause}"
        )
        detail = (
            f"Extracting coordinates from {geom} "
            f"({'native GEOMETRY' if is_native_geometry else 'WKB'})"
        )

    sql = f"CREATE OR REPLACE TABLE {quote_ident(table)} AS {select}"
    emit("load", 10.0, detail)
    _run_with_progress(con, sql, emit)

    row_count = con.sql(f"SELECT COUNT(*) FROM {quote_ident(table)}").fetchone()[0]
    emit("bounds", 98.0, "Computing bounding box")
    # Optional but cheap sanity bbox — warms the column stats.
    try:
        con.sql(
            f"SELECT MIN({quote_ident(x_out)}), MAX({quote_ident(x_out)}), "
            f"MIN({quote_ident(y_out)}), MAX({quote_ident(y_out)}) "
            f"FROM {quote_ident(table)}"
        ).fetchone()
    except Exception:
        pass

    emit("ready", 100.0, f"Loaded {row_count:,} rows")
    return FastLoadResult(
        connection=con,
        table=table,
        row_count=row_count,
        x_column=x_out,
        y_column=y_out,
        columns=columns + [c for c in (x_out, y_out) if c not in columns],
        duration_seconds=time.perf_counter() - t_start,
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
