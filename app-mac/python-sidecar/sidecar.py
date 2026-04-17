"""Desktop sidecar entry point for Geospatial Atlas.

Launched by the Tauri Rust shell with:
  argv[1]                        = path to dataset file
  env GEOSPATIAL_ATLAS_HOST      = bind host (default 127.0.0.1)
  env GEOSPATIAL_ATLAS_PORT      = bind port
  env GEOSPATIAL_ATLAS_PARENT_PID = Tauri process pid (watchdog target)

Responsibilities:
  * Load the dataset (parquet / csv / arrow / geoparquet).
  * Auto-detect GIS columns (lat/lon or WKB geometry).
  * Mount the existing FastAPI server from `embedding_atlas.server`.
  * Serve uvicorn on the chosen port.
  * Exit as soon as the Tauri parent process disappears (belt + suspenders
    against zombie sidecars if Tauri is SIGKILLed).
"""

from __future__ import annotations

import logging
import os
import pathlib
import signal
import sys
import threading
import time
from typing import Optional


def _log(msg: str) -> None:
    print(f"[sidecar] {msg}", flush=True)


def _resolve_static_dir() -> str:
    """Locate the prebuilt Svelte viewer bundled inside the app.

    When running under PyInstaller we unpack ``embedding_atlas`` into
    ``sys._MEIPASS`` (for --onedir this is the binary's folder), which
    contains ``embedding_atlas/static``.
    """
    candidates = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(pathlib.Path(meipass) / "embedding_atlas" / "static")
    here = pathlib.Path(__file__).resolve().parent
    candidates.append(here / "embedding_atlas" / "static")
    try:
        import embedding_atlas

        pkg_dir = pathlib.Path(embedding_atlas.__file__).resolve().parent
        candidates.append(pkg_dir / "static")
    except Exception:
        pass
    for c in candidates:
        if c.is_dir() and (c / "index.html").is_file():
            return str(c)
    raise RuntimeError(
        "Could not locate bundled static viewer; tried: "
        + ", ".join(str(c) for c in candidates)
    )


def _start_parent_watchdog(parent_pid_env: Optional[str]) -> None:
    """Kill this process as soon as the Tauri parent goes away.

    Uses three independent mechanisms so a starved thread or closed stdin
    alone cannot strand the process:

      1. macOS kqueue ``EVFILT_PROC NOTE_EXIT`` on the parent PID — a
         native, non-polling notification that never misses and is not
         starved by asyncio-heavy threads.
      2. stdin EOF watcher — Tauri pipes stdin, so closure is another
         reliable parent-died signal.
      3. PID / getppid poll at 200 ms — belt and braces.
    """

    # Only activate stdin/kqueue watchdogs when we know we're being run
    # from a Tauri shell (which always sets PARENT_PID). Standalone CLI
    # invocations (including tests via bash backgrounding) have stdin
    # redirected to /dev/null, which would false-trigger the EOF watcher.
    if not parent_pid_env:
        return

    def _watch_stdin() -> None:
        try:
            while True:
                chunk = sys.stdin.buffer.read(4096)
                if not chunk:
                    break
        except Exception:
            pass
        _log("stdin closed, shutting down")
        os._exit(0)

    threading.Thread(target=_watch_stdin, name="stdin-watchdog", daemon=True).start()
    try:
        parent_pid = int(parent_pid_env)
    except ValueError:
        return

    # kqueue NOTE_EXIT watcher — fires immediately when parent dies.
    try:
        import select

        if hasattr(select, "kqueue"):
            def _watch_kq() -> None:
                try:
                    kq = select.kqueue()
                    ev = select.kevent(
                        parent_pid,
                        filter=select.KQ_FILTER_PROC,
                        flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE | select.KQ_EV_ONESHOT,
                        fflags=select.KQ_NOTE_EXIT,
                    )
                    kq.control([ev], 0, None)
                    events = kq.control(None, 1, None)
                    if events:
                        _log(f"kqueue NOTE_EXIT for parent {parent_pid}, shutting down")
                        os._exit(0)
                except Exception as e:
                    _log(f"kqueue watcher failed: {e}")

            threading.Thread(target=_watch_kq, name="kq-watchdog", daemon=True).start()
    except Exception:
        pass

    def _watch_pid() -> None:
        while True:
            try:
                os.kill(parent_pid, 0)
            except OSError:
                _log(f"parent pid {parent_pid} gone, shutting down")
                os._exit(0)
            if os.getppid() == 1:
                _log("reparented to launchd, shutting down")
                os._exit(0)
            time.sleep(0.2)

    threading.Thread(target=_watch_pid, name="pid-watchdog", daemon=True).start()


def _install_signal_handlers() -> None:
    def _graceful(signum, _frame):
        _log(f"signal {signum} received, exiting")
        os._exit(0)

    for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(s, _graceful)
        except (OSError, ValueError):
            pass


def _emit_progress(stage: str, percent: float, detail: str) -> None:
    """Forward loader progress to the Tauri shell as a GSA_PROGRESS line.
    The Rust sidecar handler parses these and emits 'sidecar-progress'."""
    from embedding_atlas.fast_load import progress_line

    print(progress_line(stage, percent, detail), flush=True)


def _fast_load(dataset_path: str, limit: Optional[int] = None):
    """DuckDB-native fast path. Returns (connection, DataSource, props, is_gis)."""
    from embedding_atlas.cache import sha256_hexdigest
    from embedding_atlas.data_source import DataSource
    from embedding_atlas.fast_load import fast_load_parquet
    from embedding_atlas.options import make_embedding_atlas_props
    from embedding_atlas.version import __version__

    result = fast_load_parquet(dataset_path, progress=_emit_progress, limit=limit)

    # Add an __row_index__ column to the DuckDB table (used by the viewer).
    con = result.connection
    id_col = "__row_index__"
    existing_cols = [r[0] for r in con.sql(f'PRAGMA table_info("{result.table}")').fetchall()]
    i = 1
    while id_col in existing_cols:
        id_col = f"__row_index___{i}"
        i += 1
    con.sql(
        f'ALTER TABLE "{result.table}" ADD COLUMN "{id_col}" BIGINT DEFAULT 0'
    )
    con.sql(
        f'UPDATE "{result.table}" SET "{id_col}" = rowid'
    )
    con.sql("SET enable_external_access = false")
    con.sql("SET lock_configuration = true")

    props = make_embedding_atlas_props(
        row_id=id_col,
        x=result.x_column,
        y=result.y_column,
        neighbors=None,
        text=None,
        point_size=None,
        stop_words=None,
        labels=None,
        is_gis=True,
    )
    metadata = {"props": props}
    identifier = sha256_hexdigest(
        [__version__, [dataset_path], metadata], scope="DataSource"
    )
    data_source = DataSource(identifier, None, metadata)
    _log(
        f"fast load done: {result.row_count:,} rows in {result.duration_seconds:.2f}s "
        f"(x={result.x_column}, y={result.y_column})"
    )
    return con, data_source, metadata, True


def _add_shutdown_endpoint(app) -> None:
    from fastapi import Response

    @app.post("/_internal/shutdown")
    def shutdown() -> Response:
        def _exit() -> None:
            time.sleep(0.1)
            os._exit(0)

        threading.Thread(target=_exit, daemon=True).start()
        return Response(status_code=204)


def main() -> int:
    _install_signal_handlers()
    _start_parent_watchdog(os.environ.get("GEOSPATIAL_ATLAS_PARENT_PID"))
    # Log received env + argv to aid debugging when the app UI is opaque.
    env_dump = {k: v for k, v in os.environ.items() if k.startswith("GEOSPATIAL_ATLAS_")}
    _log(f"argv={sys.argv!r}")
    _log(f"env={env_dump!r}")

    if len(sys.argv) < 2:
        print("usage: sidecar <dataset-path>", file=sys.stderr)
        return 2

    dataset_path = sys.argv[1]
    host = os.environ.get("GEOSPATIAL_ATLAS_HOST", "127.0.0.1")
    port_str = os.environ.get("GEOSPATIAL_ATLAS_PORT")
    if not port_str:
        print("GEOSPATIAL_ATLAS_PORT env var required", file=sys.stderr)
        return 2
    port = int(port_str)

    from embedding_atlas.server import make_server

    # argv[2] is the optional row limit (0 or absent means "no limit").
    limit: Optional[int] = None
    if len(sys.argv) >= 3:
        try:
            n = int(sys.argv[2])
            if n > 0:
                limit = n
        except ValueError:
            _log(f"invalid limit argv[2]={sys.argv[2]!r}, ignoring")
    _log(f"using row limit: {limit if limit else 'none'}")
    connection, data_source, _meta, _is_gis = _fast_load(dataset_path, limit=limit)
    static_path = _resolve_static_dir()
    _log(f"static dir: {static_path}")

    app = make_server(
        data_source,
        static_path=static_path,
        duckdb_uri="server",
        mcp=False,
        cors=False,
        duckdb_connection=connection,
    )
    _add_shutdown_endpoint(app)

    _log(f"listening on http://{host}:{port}")

    import uvicorn

    uvicorn.run(app, host=host, port=port, access_log=False, log_level=logging.WARNING)
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except BaseException:
        import traceback

        traceback.print_exc()
        # Bypass Python's orderly shutdown — our daemon watchdog threads
        # may be blocked on stdin.read(), which makes interpreter
        # finalisation hang / emit an ugly
        # "could not acquire lock for <stdin> at interpreter shutdown"
        # warning. os._exit skips that.
        os._exit(1)
    else:
        os._exit(code or 0)
