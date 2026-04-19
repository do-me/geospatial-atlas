# Copyright (c) 2025 Apple Inc. Licensed under MIT License.

import asyncio
import concurrent.futures
import json
import os
import re
import uuid
from functools import lru_cache
from typing import Callable

import duckdb
from fastapi import FastAPI, HTTPException, Request, Response, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .data_source import DataSource
from .utils import arrow_to_bytes, to_parquet_bytes


def make_server(
    data_source: DataSource,
    *,
    static_path: str,
    mcp: bool = False,
    cors: bool | list[str] = False,
    duckdb_uri: str | None = None,
    duckdb_connection: "duckdb.DuckDBPyConnection | None" = None,
):
    """Creates a server for hosting Geospatial Atlas.

    If ``duckdb_connection`` is provided, it is used as-is for server-mode
    queries (skipping the pandas-DF-based ``make_duckdb_connection``). This
    lets callers build a DuckDB table directly from Parquet via
    ``read_parquet`` and skip the pandas materialization, which for large
    geospatial files (tens of millions of rows) turns a minutes-long load
    into a few seconds.
    """

    app = FastAPI()

    if cors is not None:
        if isinstance(cors, bool) and cors:
            app.add_middleware(
                CORSMiddleware,
                allow_origins=["*"],
                allow_methods=["*"],
                allow_headers=["*"],
                expose_headers=["*"],
            )
        elif isinstance(cors, list):
            app.add_middleware(
                CORSMiddleware,
                allow_origins=cors,
                allow_methods=["*"],
                allow_headers=["*"],
                expose_headers=["*"],
            )

    def _dataset_parquet_bytes() -> bytes:
        if data_source.dataset is not None:
            return to_parquet_bytes(data_source.dataset)
        # Fast-path: dataset was loaded directly into DuckDB; materialize
        # the full table on demand. This endpoint is only hit for
        # archive export / wasm fallback paths.
        if duckdb_connection is not None:
            return to_parquet_bytes(duckdb_connection.sql("SELECT * FROM dataset").df())
        raise RuntimeError("no dataset available to serialize")

    mount_bytes(
        app,
        "/data/dataset.parquet",
        "application/octet-stream",
        _dataset_parquet_bytes,
    )

    @app.get("/data/metadata.json")
    async def get_metadata():
        meta = {}
        # Database
        if duckdb_uri is None or duckdb_uri == "wasm":
            meta["database"] = {"type": "wasm", "load": True}
        elif duckdb_uri == "server":
            # Point to the server itself.
            meta["database"] = {"type": "rest"}
        else:
            # Point to the given uri.
            if duckdb_uri.startswith("http"):
                meta["database"] = {
                    "type": "rest",
                    "uri": duckdb_uri,
                    "load": True,
                }
            elif duckdb_uri.startswith("ws"):
                meta["database"] = {
                    "type": "socket",
                    "uri": duckdb_uri,
                    "load": True,
                }
            else:
                raise ValueError("invalid DuckDB uri")
        # MCP
        if mcp:
            meta["mcp"] = {"type": "websocket"}

        return data_source.metadata | meta

    @app.post("/data/cache/{name}")
    async def post_cache(request: Request, name: str):
        data_source.cache_set(name, await request.json())

    @app.get("/data/cache/{name}")
    async def get_cache(name: str):
        obj = data_source.cache_get(name)
        if obj is None:
            return Response(status_code=404)
        return obj

    @app.get("/data/archive.zip")
    async def make_archive():
        data = data_source.make_archive(static_path)
        return Response(content=data, media_type="application/zip")

    if duckdb_uri == "server":
        if duckdb_connection is None:
            duckdb_connection = make_duckdb_connection(data_source.dataset)
    else:
        duckdb_connection = None

    def handle_query(query: dict):
        assert duckdb_connection is not None
        sql = query["sql"]
        command = query["type"]
        with duckdb_connection.cursor() as cursor:
            try:
                result = cursor.execute(sql)
                if command == "exec":
                    return JSONResponse({})
                elif command == "arrow":
                    buf = arrow_to_bytes(result.arrow())
                    return Response(
                        buf, headers={"Content-Type": "application/octet-stream"}
                    )
                elif command == "json":
                    data = result.df().to_json(orient="records")
                    return Response(data, headers={"Content-Type": "application/json"})
                else:
                    raise ValueError(f"Unknown command {command}")
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)

    def handle_selection(query: dict):
        assert duckdb_connection is not None
        predicate = query.get("predicate", None)
        format = query["format"]
        formats = {
            "json": "(FORMAT JSON, ARRAY true)",
            "jsonl": "(FORMAT JSON)",
            "csv": "(FORMAT CSV)",
            "parquet": "(FORMAT parquet)",
        }
        with duckdb_connection.cursor() as cursor:
            filename = ".selection-" + str(uuid.uuid4()) + ".tmp"
            try:
                if predicate is not None:
                    cursor.execute(
                        f"COPY (SELECT * FROM dataset WHERE {predicate}) TO '{filename}' {formats[format]}"
                    )
                else:
                    cursor.execute(f"COPY dataset TO '{filename}' {formats[format]}")
                with open(filename, "rb") as f:
                    buffer = f.read()
                    return Response(
                        buffer, headers={"Content-Type": "application/octet-stream"}
                    )
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)
            finally:
                try:
                    os.unlink(filename)
                except Exception:
                    pass

    executor = concurrent.futures.ThreadPoolExecutor()

    @app.get("/data/query")
    async def get_query(req: Request):
        data = json.loads(req.query_params["query"])
        return await asyncio.get_running_loop().run_in_executor(
            executor, lambda: handle_query(data)
        )

    @app.post("/data/query")
    async def post_query(req: Request):
        body = await req.body()
        data = json.loads(body)
        return await asyncio.get_running_loop().run_in_executor(
            executor, lambda: handle_query(data)
        )

    @app.post("/data/selection")
    async def post_selection(req: Request):
        body = await req.body()
        data = json.loads(body)
        return await asyncio.get_running_loop().run_in_executor(
            executor, lambda: handle_selection(data)
        )

    if mcp:
        make_mcp_proxy(app)

    # Static files for the frontend
    app.mount("/", StaticFiles(directory=static_path, html=True))

    return app


class WebSocketHandler:
    def __init__(self, websocket: WebSocket):
        self.websocket = websocket
        self.pending_requests: dict[str, asyncio.Future] = {}
        self.is_connected = True

    async def handle_connection(self):
        try:
            while self.is_connected:
                data = await self.websocket.receive_text()
                await self._handle_message(data)
        except Exception as _:
            pass
        finally:
            await self._cleanup()

    async def _handle_message(self, data: str):
        try:
            response = json.loads(data)
            request_id = response.get("id")
            if request_id and request_id in self.pending_requests:
                future = self.pending_requests.pop(request_id)
                if not future.done():
                    future.set_result(response.get("response"))
        except json.JSONDecodeError:
            pass
        except Exception as e:
            print(f"Error processing WebSocket message: {e}")

    async def _cleanup(self):
        self.is_connected = False
        for future in self.pending_requests.values():
            if not future.done():
                future.cancel()
        self.pending_requests.clear()

    async def send_request(self, request: dict, timeout: float = 300.0) -> dict:
        """Send a request to the WebSocket and wait for response.

        Timeout defaults to 5 min so MCP tools that run heavy DuckDB scans
        over 75 M+ row datasets don't get guillotined at 30 s.
        """
        if not self.is_connected:
            raise HTTPException(status_code=503, detail="WebSocket disconnected")

        request_id = str(uuid.uuid4())
        payload = {"id": request_id, "request": request}

        future = asyncio.Future()
        self.pending_requests[request_id] = future

        try:
            await self.websocket.send_text(json.dumps(payload))
            response = await asyncio.wait_for(future, timeout=timeout)
            return response

        except asyncio.TimeoutError:
            self.pending_requests.pop(request_id, None)
            raise HTTPException(status_code=408, detail="Request timeout")
        except Exception as e:
            self.pending_requests.pop(request_id, None)
            if not self.is_connected:
                raise HTTPException(status_code=503, detail="WebSocket disconnected")
            else:
                raise HTTPException(
                    status_code=500, detail=f"Internal server error: {str(e)}"
                )

    async def send_close(self):
        try:
            await self.websocket.send_text(json.dumps({"control": "close"}))
        except Exception:
            pass


def make_mcp_proxy(app: FastAPI):
    """Expose the viewer's MCP tools via the standard Streamable HTTP transport.

    Adds three routes:
      * ``GET/POST/DELETE /mcp`` — Streamable HTTP endpoint, the format
        Claude Desktop and every other MCP client speak natively. No
        stdio bridge / Node shim required.
      * ``WS /data/mcp_websocket`` — the viewer JS connects here on load
        and registers as the tool executor.
      * ``POST /mcp_legacy`` — the old home-grown "plain POST forward"
        endpoint, kept for backwards compatibility until downstreams
        migrate. New integrations should use ``/mcp``.
    """
    from .mcp_bridge import MCPBridge

    last_handler: dict[str, WebSocketHandler | None] = {"handler": None}

    def get_handler() -> "WebSocketHandler | None":
        return last_handler["handler"]

    @app.websocket("/data/mcp_websocket")
    async def websocket_mcp_ws(websocket: WebSocket):
        await websocket.accept()
        handler = WebSocketHandler(websocket)
        if last_handler["handler"] is not None:
            await last_handler["handler"].send_close()
        last_handler["handler"] = handler
        await handler.handle_connection()
        if last_handler["handler"] == handler:
            last_handler["handler"] = None

    # Standards-compliant MCP endpoint (Streamable HTTP).
    bridge = MCPBridge(get_handler)
    bridge.mount(app, path="/mcp")

    # Legacy plain-POST endpoint — will be removed after users migrate.
    @app.post("/mcp_legacy")
    async def post_mcp_legacy(request: Request):
        handler = last_handler["handler"]
        if handler is None or not handler.is_connected:
            raise HTTPException(status_code=503, detail="No MCP WebSocket connected")
        return await handler.send_request(await request.json())


def make_duckdb_connection(df):
    con = duckdb.connect(":memory:")
    _ = df  # used in the query
    con.sql("CREATE TABLE dataset AS (SELECT * FROM df)")
    con.sql("SET enable_external_access = false")
    con.sql("SET lock_configuration = true")
    return con


def parse_range_header(request: Request, content_length: int):
    value = request.headers.get("Range")
    if value is not None:
        m = re.match(r"^ *bytes *= *([0-9]+) *- *([0-9]+) *$", value)
        if m is not None:
            r0 = int(m.group(1))
            r1 = int(m.group(2)) + 1
            if r0 < r1 and r0 <= content_length and r1 <= content_length:
                return (r0, r1)
    return None


def mount_bytes(
    app: FastAPI, url: str, media_type: str, make_content: Callable[[], bytes]
):
    @lru_cache(maxsize=1)
    def get_content() -> bytes:
        return make_content()

    @app.head(url)
    async def head(request: Request):
        content = get_content()
        bytes_range = parse_range_header(request, len(content))
        if bytes_range is None:
            length = len(content)
        else:
            length = bytes_range[1] - bytes_range[0]
        return Response(
            headers={
                "Content-Length": str(length),
                "Content-Type": media_type,
            }
        )

    @app.get(url)
    async def get(request: Request):
        content = get_content()
        bytes_range = parse_range_header(request, len(content))
        if bytes_range is None:
            return Response(content=content)
        else:
            r0, r1 = bytes_range
            result = content[r0:r1]
            return Response(
                content=result,
                headers={
                    "Content-Length": str(r1 - r0),
                    "Content-Range": f"bytes {r0}-{r1 - 1}/{len(content)}",
                    "Content-Type": media_type,
                },
                media_type=media_type,
                status_code=206,
            )
