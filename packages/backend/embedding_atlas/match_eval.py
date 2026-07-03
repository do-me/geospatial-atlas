# Copyright (c) 2025 Apple Inc. Licensed under MIT License.

"""Build the matcher-eval Expected Format (Points + Lines) from a matcher run.

This is the *local* build step described in
``docs/adr/0002-decoupled-expected-format-contract.md``: it reads the raw
``candidates/``, ``baseline/``, ``matches/`` and ``blocking/`` folders produced by a
matcher run and writes two pre-built datasets the viewer can consume directly:

- **points.parquet** — candidates ∪ baselines, one row per POI, tagged with
  ``point_class``. Memory-heavy / matcher-internal source columns (the VARCHAR[] arrays,
  the redundant ``*_clean`` text and the dedup ``signature`` — see ``DROP_COLUMNS``) are
  dropped so the build scales to tens of millions of POIs; the display/analysis scalars
  are kept. Two candidate-only flags
  are added (null on baseline rows): ``history_match`` (1 if the candidate's
  ``base_ids`` is non-null, else 0) and ``blocked`` (1 if the candidate never entered
  a ``blocking`` pair — blocked out before matching — else 0). A ``cluster_size`` flag
  (on both candidates and baselines, null when unmatched) gives the number of
  ``match`` rows sharing the record's ``base_id`` cluster.
- **lines.parquet** — one Match Line per ``match_type='match'`` row, with both
  type-aware endpoints, the pair identity, ``composite_score`` and ``match_pair_type``.

Endpoint resolution is type-aware (see ``CONTEXT.md``): ``id`` resolves against
``candidates`` when ``record_type='candidate'`` else ``baseline``; ``base_id`` resolves
against ``candidates`` when ``base_record_type='candidate'`` else ``baseline``.

The viewer never performs this join — it only loads the outputs.
"""

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path

import click
import duckdb
from platformdirs import user_cache_path

from .cache import sha256_hexdigest
from .utils import apply_logging_config

logger = logging.getLogger("embedding-atlas")

# Subfolders expected inside a matcher run directory.
CANDIDATES_DIR = "candidates"
BASELINE_DIR = "baseline"
MATCHES_DIR = "matches"
BLOCKING_DIR = "blocking"

# Version the build logic so a code change invalidates stale cached outputs.
BUILD_VERSION = 4

# Memory-heavy / matcher-internal source columns dropped from the Points output.
# At real scale (tens of millions of POIs) these dominate the in-memory build and
# the parquet size: four VARCHAR[] arrays plus the redundant cleaned-text columns
# and the dedup signature. The display/analysis scalars are kept. ``base_ids`` is
# dropped from the output but still read to derive ``history_match``.
DROP_COLUMNS = [
    "name_clean",
    "name_clean_tokens",
    "address_clean",
    "address_clean_tokens",
    "blocking_keys",
    "signature",
]
# Candidate-only column: present on candidates, absent on baselines.
DROP_COLUMNS_CANDIDATE = DROP_COLUMNS + ["base_ids"]


@dataclass
class MatchEvalBuild:
    """Result of a build: output paths and the stats worth logging."""

    points_path: str
    lines_path: str
    n_candidates: int
    n_baselines: int
    n_points: int
    n_points_dropped_no_coords: int
    n_match_rows: int
    n_lines: int
    n_lines_dropped: int
    pair_type_counts: dict[str, int]
    from_cache: bool


def _glob_for(path: Path) -> str:
    """Return a DuckDB read_parquet glob for a dataset that may be a single file
    or a directory of parts (flat or Hive-partitioned)."""
    if path.is_dir():
        return str(path / "**" / "*.parquet")
    return str(path)


def _list_input_files(*globs: str) -> list[tuple[str, int, int]]:
    """Resolve globs to a sorted list of (path, size, mtime_ns) for cache keying."""
    out: list[tuple[str, int, int]] = []
    for g in globs:
        for p in sorted(Path().glob(g) if not Path(g).is_absolute() else _abs_glob(g)):
            st = p.stat()
            out.append((str(p), st.st_size, st.st_mtime_ns))
    return sorted(out)


def _abs_glob(glob: str) -> list[Path]:
    # pathlib can't glob an absolute pattern directly; split at the first wildcard.
    parts = Path(glob).parts
    for i, part in enumerate(parts):
        if any(ch in part for ch in "*?["):
            root = Path(*parts[:i]) if i > 0 else Path("/")
            pattern = str(Path(*parts[i:]))
            return list(root.glob(pattern))
    p = Path(glob)
    return [p] if p.exists() else []


def _resolve_run_dir(run_dir: Path) -> tuple[Path, Path, Path, Path]:
    """Locate the candidates/baseline/matches/blocking inputs in a run directory."""
    candidates = run_dir / CANDIDATES_DIR
    baseline = run_dir / BASELINE_DIR
    matches = run_dir / MATCHES_DIR
    blocking = run_dir / BLOCKING_DIR
    missing = [
        d.name for d in (candidates, baseline, matches, blocking) if not d.exists()
    ]
    if missing:
        raise FileNotFoundError(
            f"Run directory {run_dir} is missing expected subfolder(s): "
            f"{', '.join(missing)}. Expected {CANDIDATES_DIR}/, {BASELINE_DIR}/, "
            f"{MATCHES_DIR}/, {BLOCKING_DIR}/."
        )
    return candidates, baseline, matches, blocking


def _default_out_dir(
    cand_glob: str, base_glob: str, match_glob: str, block_glob: str
) -> Path:
    """A cache directory keyed by the resolved input files (path/size/mtime)."""
    files = _list_input_files(cand_glob, base_glob, match_glob, block_glob)
    key = sha256_hexdigest([BUILD_VERSION, files], scope="match_eval")
    return user_cache_path("embedding_atlas") / "match_eval" / key


def build_match_eval(
    run_dir: str | Path,
    out_dir: str | Path | None = None,
    *,
    force: bool = False,
) -> MatchEvalBuild:
    """Build Points + Lines parquet from a local matcher run directory.

    Args:
        run_dir: directory containing ``candidates/``, ``baseline/``, ``matches/``.
        out_dir: where to write ``points.parquet`` / ``lines.parquet``. If None, a
            content-addressed cache directory is used and reused across runs.
        force: rebuild even if a valid cached build already exists.

    Returns:
        A :class:`MatchEvalBuild` with output paths and stats.
    """
    run_dir = Path(run_dir).expanduser().resolve()
    candidates, baseline, matches, blocking = _resolve_run_dir(run_dir)
    cand_glob = _glob_for(candidates)
    base_glob = _glob_for(baseline)
    match_glob = _glob_for(matches)
    block_glob = _glob_for(blocking)

    if out_dir is None:
        out_dir = _default_out_dir(cand_glob, base_glob, match_glob, block_glob)
    out_dir = Path(out_dir).expanduser().resolve()
    points_path = out_dir / "points.parquet"
    lines_path = out_dir / "lines.parquet"
    manifest_path = out_dir / "manifest.json"

    cached = _load_cached(manifest_path, points_path, lines_path)
    if cached is not None and not force:
        logger.info("match-eval: using cached build at %s", out_dir)
        cached.from_cache = True
        return cached

    out_dir.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    try:
        result = _run_build(
            con, cand_glob, base_glob, match_glob, block_glob, points_path, lines_path
        )
    finally:
        con.close()

    manifest_path.write_text(json.dumps(asdict(result), indent=2))
    _log_summary(result)
    return result


def _run_build(
    con: duckdb.DuckDBPyConnection,
    cand_glob: str,
    base_glob: str,
    match_glob: str,
    block_glob: str,
    points_path: Path,
    lines_path: Path,
) -> MatchEvalBuild:
    p = {
        "cand": cand_glob,
        "base": base_glob,
        "match": match_glob,
        "block": block_glob,
    }

    n_candidates = con.execute(
        "SELECT count(*) FROM read_parquet($cand)", {"cand": cand_glob}
    ).fetchone()[0]
    n_baselines = con.execute(
        "SELECT count(*) FROM read_parquet($base)", {"base": base_glob}
    ).fetchone()[0]
    n_match_rows = con.execute(
        "SELECT count(*) FROM read_parquet($match) WHERE match_type='match'",
        {"match": match_glob},
    ).fetchone()[0]

    # ---- Points -------------------------------------------------------------
    # Matched if the id participates in any match row on either side, with the
    # side's record-type matching this point's origin folder.
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE _points AS
        WITH m AS (
            SELECT * FROM read_parquet($match) WHERE match_type='match'
        ),
        matched_cand AS (
            SELECT id AS pid FROM m WHERE record_type='candidate'
            UNION
            SELECT base_id AS pid FROM m WHERE base_record_type='candidate'
        ),
        matched_base AS (
            SELECT id AS pid FROM m WHERE record_type='baseline'
            UNION
            SELECT base_id AS pid FROM m WHERE base_record_type='baseline'
        ),
        -- Candidate ids that took part in any blocking pair (either side).
        blocked_cand AS (
            SELECT id AS pid FROM read_parquet($block) WHERE record_type='candidate'
            UNION
            SELECT base_id AS pid FROM read_parquet($block)
                WHERE base_record_type='candidate'
        ),
        -- Cluster size = number of match rows sharing a base_id; assigned to every
        -- member of that cluster (both the base_id record and each id record),
        -- type-aware via record_type / base_record_type. Unmatched points get null.
        csize AS (SELECT base_id, count(*) AS n FROM m GROUP BY base_id),
        members AS (
            SELECT id AS pid, record_type AS rt, base_id FROM m
            UNION ALL
            SELECT base_id AS pid, base_record_type AS rt, base_id FROM m
        ),
        pid_size AS (
            SELECT pid, rt, max(n) AS cluster_size
            FROM members JOIN csize USING (base_id)
            GROUP BY pid, rt
        ),
        cand AS (
            SELECT c.* EXCLUDE ({cand_excl}), 'candidate' AS origin,
                CASE WHEN c.id IN (SELECT pid FROM matched_cand)
                     THEN 'matched_candidate' ELSE 'unmatched_candidate' END
                AS point_class,
                -- 1 if the candidate carries a historical base_ids link, else 0.
                -- (base_ids itself is dropped from the output; only the flag is kept.)
                CASE WHEN c.base_ids IS NOT NULL THEN 1 ELSE 0 END
                AS history_match,
                -- 1 if the candidate never entered a blocking pair (blocked out
                -- before matching), else 0.
                CASE WHEN c.id IN (SELECT pid FROM blocked_cand) THEN 0 ELSE 1 END
                AS blocked,
                cs.cluster_size AS cluster_size
            FROM read_parquet($cand) c
            LEFT JOIN pid_size cs ON cs.pid = c.id AND cs.rt = 'candidate'
        ),
        base AS (
            SELECT b.* EXCLUDE ({base_excl}), 'baseline' AS origin,
                CASE WHEN b.id IN (SELECT pid FROM matched_base)
                     THEN 'matched_baseline' ELSE 'unmatched_baseline' END
                AS point_class,
                -- history_match / blocked are candidate-only; null on baseline rows.
                CAST(NULL AS INTEGER) AS history_match,
                CAST(NULL AS INTEGER) AS blocked,
                cs.cluster_size AS cluster_size
            FROM read_parquet($base) b
            LEFT JOIN pid_size cs ON cs.pid = b.id AND cs.rt = 'baseline'
        )
        -- One row per POI: kept scalar columns + origin + point_class + flags.
        -- Memory-heavy / matcher-internal columns (arrays, *_clean, signature) are
        -- excluded; no per-match enrichment — a POI may have several matches, so all
        -- match detail lives in the Lines dataset (joinable by id / base_id).
        SELECT * FROM cand
        UNION ALL BY NAME
        SELECT * FROM base
        """.format(
            cand_excl=", ".join(DROP_COLUMNS_CANDIDATE),
            base_excl=", ".join(DROP_COLUMNS),
        ),
        p,
    )

    n_points_all = con.execute("SELECT count(*) FROM _points").fetchone()[0]
    con.execute(
        """
        COPY (
            SELECT * FROM _points
            WHERE latitude IS NOT NULL AND longitude IS NOT NULL
        ) TO $out (FORMAT parquet)
        """,
        {"out": str(points_path)},
    )
    n_points = con.execute(
        "SELECT count(*) FROM read_parquet($out)", {"out": str(points_path)}
    ).fetchone()[0]
    n_points_dropped_no_coords = n_points_all - n_points

    # ---- Lines --------------------------------------------------------------
    # Type-aware endpoint resolution via record_type / base_record_type.
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE _lines AS
        WITH m AS (
            SELECT * FROM read_parquet($match) WHERE match_type='match'
        ),
        cand AS (SELECT id, longitude, latitude FROM read_parquet($cand)),
        base AS (SELECT id, longitude, latitude FROM read_parquet($base))
        SELECT
            CASE WHEN m.record_type='candidate' THEN c1.longitude
                 ELSE b1.longitude END AS lon1,
            CASE WHEN m.record_type='candidate' THEN c1.latitude
                 ELSE b1.latitude  END AS lat1,
            CASE WHEN m.base_record_type='candidate' THEN c2.longitude
                 ELSE b2.longitude END AS lon2,
            CASE WHEN m.base_record_type='candidate' THEN c2.latitude
                 ELSE b2.latitude  END AS lat2,
            m.id              AS id,
            m.base_id         AS base_id,
            m.composite_score AS composite_score,
            m.record_type || '->' || m.base_record_type AS match_pair_type
        FROM m
        LEFT JOIN cand c1 ON m.record_type='candidate'      AND m.id      = c1.id
        LEFT JOIN base b1 ON m.record_type='baseline'       AND m.id      = b1.id
        LEFT JOIN cand c2 ON m.base_record_type='candidate' AND m.base_id = c2.id
        LEFT JOIN base b2 ON m.base_record_type='baseline'  AND m.base_id = b2.id
        """,
        {"cand": cand_glob, "base": base_glob, "match": match_glob},
    )

    con.execute(
        """
        COPY (
            SELECT lon1, lat1, lon2, lat2, id, base_id,
                   composite_score, match_pair_type
            FROM _lines
            WHERE lon1 IS NOT NULL AND lat1 IS NOT NULL
              AND lon2 IS NOT NULL AND lat2 IS NOT NULL
        ) TO $out (FORMAT parquet)
        """,
        {"out": str(lines_path)},
    )
    n_lines = con.execute(
        "SELECT count(*) FROM read_parquet($out)", {"out": str(lines_path)}
    ).fetchone()[0]
    n_lines_dropped = n_match_rows - n_lines

    pair_type_counts = dict(
        con.execute(
            "SELECT match_pair_type, count(*) FROM read_parquet($out) "
            "GROUP BY 1 ORDER BY 2 DESC",
            {"out": str(lines_path)},
        ).fetchall()
    )

    return MatchEvalBuild(
        points_path=str(points_path),
        lines_path=str(lines_path),
        n_candidates=n_candidates,
        n_baselines=n_baselines,
        n_points=n_points,
        n_points_dropped_no_coords=n_points_dropped_no_coords,
        n_match_rows=n_match_rows,
        n_lines=n_lines,
        n_lines_dropped=n_lines_dropped,
        pair_type_counts=pair_type_counts,
        from_cache=False,
    )


def _load_cached(
    manifest_path: Path, points_path: Path, lines_path: Path
) -> MatchEvalBuild | None:
    if not (manifest_path.exists() and points_path.exists() and lines_path.exists()):
        return None
    try:
        data = json.loads(manifest_path.read_text())
        return MatchEvalBuild(**data)
    except Exception:
        logger.debug("match-eval: cache manifest unreadable", exc_info=True)
        return None


def _log_summary(r: MatchEvalBuild) -> None:
    logger.info(
        "match-eval build: %d candidates + %d baselines -> %d points "
        "(%d dropped: no coords)",
        r.n_candidates,
        r.n_baselines,
        r.n_points,
        r.n_points_dropped_no_coords,
    )
    logger.info(
        "match-eval build: %d match rows -> %d lines (%d dropped: unresolved / "
        "no coords). Pair types: %s",
        r.n_match_rows,
        r.n_lines,
        r.n_lines_dropped,
        r.pair_type_counts,
    )


def _launch_viewer(
    points_input: str,
    lines_glob: str,
    *,
    host: str,
    port: int,
    lines_min_zoom: float,
) -> None:
    """Load the Points dataset via the GIS fast path and launch the viewer with
    the Lines dataset as a secondary table.

    ``points_input`` may be a single ``.parquet`` file, a directory of parquet
    parts, or a glob; ``lines_glob`` is a read_parquet target (file/dir-glob).
    """
    from .cli import _run_fast_path, _try_fast_load

    fast_connection = _try_fast_load(
        inputs=[points_input],
        splits=[],
        query=None,
        sample=None,
        text=None,
        image=None,
        audio=None,
        vector=None,
        x_column=None,
        y_column=None,
        duckdb_uri="server",
    )
    if fast_connection is None:
        raise click.ClickException(
            f"Could not load the points dataset via the GIS fast path: {points_input}"
        )
    # Expose the POI ``name`` column as the viewer's tooltip/label text. This is
    # what powers the "Point Labels" toggle (a name above each dot) and the
    # tooltip name; it is pure metadata and does not trigger any embedding.
    cols_lower = {c.lower(): c for c in fast_connection.columns}
    text_column = cols_lower.get("name")
    _run_fast_path(
        fast_connection=fast_connection,
        static=None,
        host=host,
        port=port,
        enable_auto_port=True,
        enable_mcp=False,
        cors=None,
        duckdb_uri="server",
        lines_glob=lines_glob,
        lines_min_zoom=lines_min_zoom,
        text_column=text_column,
    )


@click.command(name="match-eval")
@click.argument("run_dir", type=click.Path(exists=True, file_okay=False), required=False)
@click.option(
    "--out",
    "out_dir",
    type=click.Path(file_okay=False),
    default=None,
    help="Output directory for points.parquet / lines.parquet. "
    "Defaults to a content-addressed cache directory.",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Rebuild even if a valid cached build already exists.",
)
@click.option(
    "--build-only",
    is_flag=True,
    default=False,
    help="Build the Expected Format and exit without launching the viewer.",
)
@click.option(
    "--points",
    "points_path",
    type=click.Path(exists=True),
    default=None,
    help="Prebuilt Points dataset (a .parquet file or a directory of parts, e.g. a "
    "Databricks/Spark build downloaded locally). Skips the build entirely. Requires "
    "--lines. Mutually exclusive with RUN_DIR.",
)
@click.option(
    "--lines",
    "lines_path",
    type=click.Path(exists=True),
    default=None,
    help="Prebuilt Lines dataset (a .parquet file or a directory of parts). Used with "
    "--points.",
)
@click.option("--host", default="localhost", help="Host for the web server.")
@click.option("--port", default=5055, help="Port for the web server.")
@click.option(
    "--lines-min-zoom",
    type=float,
    default=12.0,
    help="Zoom threshold below which Match Lines are hidden (they are sub-pixel "
    "at lower zoom). Default 12.",
)
def match_eval_cli(
    run_dir: str | None,
    out_dir: str | None,
    force: bool,
    build_only: bool,
    points_path: str | None,
    lines_path: str | None,
    host: str,
    port: int,
    lines_min_zoom: float,
):
    """Launch the matcher-eval viewer over the Expected Format (Points + Lines).

    Two modes:

    \b
    * Build mode (default): pass RUN_DIR containing candidates/, baseline/,
      matches/ and blocking/ subfolders; the Expected Format is built locally
      (DuckDB) and cached, then the viewer launches.
    * Prebuilt mode: pass --points and --lines pointing at an already-built
      Expected Format (e.g. produced on Databricks and downloaded). The build
      is skipped entirely; the files are loaded and the viewer launches.
    """
    apply_logging_config()

    prebuilt = points_path is not None or lines_path is not None
    if prebuilt:
        if run_dir is not None:
            raise click.UsageError(
                "Pass either RUN_DIR (build mode) or --points/--lines "
                "(prebuilt mode), not both."
            )
        if points_path is None or lines_path is None:
            raise click.UsageError(
                "Prebuilt mode requires both --points and --lines."
            )
        points_input = str(Path(points_path).expanduser().resolve())
        lines_glob = _glob_for(Path(lines_path).expanduser().resolve())
        click.echo()
        click.echo(click.style("  Points (prebuilt): ", bold=True) + points_input)
        click.echo(click.style("  Lines  (prebuilt): ", bold=True) + lines_glob)
        if build_only:
            return
        _launch_viewer(
            points_input, lines_glob, host=host, port=port,
            lines_min_zoom=lines_min_zoom,
        )
        return

    if run_dir is None:
        raise click.UsageError(
            "Pass RUN_DIR (build mode) or --points/--lines (prebuilt mode)."
        )
    result = build_match_eval(run_dir, out_dir, force=force)
    click.echo()
    click.echo(click.style("  Points: ", bold=True) + result.points_path)
    click.echo(click.style("  Lines:  ", bold=True) + result.lines_path)

    if build_only:
        return

    _launch_viewer(
        result.points_path, result.lines_path, host=host, port=port,
        lines_min_zoom=lines_min_zoom,
    )


if __name__ == "__main__":
    match_eval_cli()
