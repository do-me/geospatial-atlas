"""Canonical MCP tool descriptors for Geospatial Atlas.

These mirror the tool definitions in
``packages/viewer/src/model_context/model_context.ts`` so the FastAPI
MCP bridge can advertise them to clients (Claude Desktop, Cursor, …)
even before the viewer browser tab has connected.

If the TS file grows a new tool, add it here. If it renames one,
rename here too. Keeping both lists in sync is a small cost for a
big UX win — clients see a fully-populated tool list the moment the
MCP server comes up.

At call time, tools are still forwarded to the viewer WebSocket for
execution (see ``mcp_bridge.MCPBridge._call_tool``). If the viewer
isn't connected, the call returns a clear ``TextContent`` error
telling the user to open the viewer.
"""

from __future__ import annotations

from typing import Any

# Each entry: (name, description, input_schema)
CANONICAL_TOOLS: list[tuple[str, str, dict[str, Any]]] = [
    (
        "get_data_schema",
        "Get the table name and columns.",
        {"type": "object", "additionalProperties": False},
    ),
    (
        "run_sql_query",
        "Run a readonly SQL query in DuckDB.",
        {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The SQL query to run, must be readonly.",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    ),
    (
        "list_renderers",
        "List the available column renderers (text, number, timestamp, image, bar, …).",
        {"type": "object", "additionalProperties": False},
    ),
    (
        "get_column_styles",
        "Get column styles for all columns.",
        {"type": "object", "additionalProperties": False},
    ),
    (
        "set_column_style",
        "Set column style for a given column.",
        {
            "type": "object",
            "properties": {
                "column": {"type": "string"},
                "style": {
                    "type": "object",
                    "description": "The column style. Use list_renderers for valid renderer names.",
                },
            },
            "required": ["column", "style"],
            "additionalProperties": False,
        },
    ),
    (
        "list_charts",
        "List all charts in the current viewer.",
        {"type": "object", "additionalProperties": False},
    ),
    (
        "add_chart",
        "Create a new chart with the given specification; returns the new chart's id.",
        {
            "type": "object",
            "properties": {
                "spec": {"type": "object", "description": "The chart specification."},
            },
            "required": ["spec"],
            "additionalProperties": False,
        },
    ),
    (
        "get_chart_spec",
        "Get the specification of a chart.",
        {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
            "additionalProperties": False,
        },
    ),
    (
        "set_chart_spec",
        "Update the specification of a chart.",
        {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "spec": {"type": "object"},
            },
            "required": ["id", "spec"],
            "additionalProperties": False,
        },
    ),
    (
        "get_chart_state",
        "Get the state (filters, selection, zoom, …) of a chart.",
        {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
            "additionalProperties": False,
        },
    ),
    (
        "set_chart_state",
        "Replace the state of a chart.",
        {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "state": {"type": "object"},
            },
            "required": ["id", "state"],
            "additionalProperties": False,
        },
    ),
    (
        "clear_chart_state",
        "Clear the state of a chart.",
        {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
            "additionalProperties": False,
        },
    ),
    (
        "delete_chart",
        "Delete a chart.",
        {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
            "additionalProperties": False,
        },
    ),
    (
        "get_chart_screenshot",
        "Get a PNG screenshot of a specific chart.",
        {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
            "additionalProperties": False,
        },
    ),
    (
        "get_layout_type",
        "Get the type of the current layout ('list' or 'dashboard').",
        {"type": "object", "additionalProperties": False},
    ),
    (
        "set_layout_type",
        "Set the type of the current layout ('list' or 'dashboard').",
        {
            "type": "object",
            "properties": {"type": {"type": "string", "enum": ["list", "dashboard"]}},
            "required": ["type"],
            "additionalProperties": False,
        },
    ),
    (
        "get_layout_state",
        "Get the state of the current layout (positions, sizes).",
        {"type": "object", "additionalProperties": False},
    ),
    (
        "set_layout_state",
        "Set the state of the current layout.",
        {
            "type": "object",
            "properties": {"state": {"type": "object"}},
            "required": ["state"],
            "additionalProperties": False,
        },
    ),
    (
        "get_full_screenshot",
        "Get a PNG screenshot of the entire viewer.",
        {"type": "object", "additionalProperties": False},
    ),
    # ------------------------------------------------------------------
    # Geospatial tools (only useful when the loaded dataset is GIS —
    # i.e. the embedding chart has data.isGis=true).
    # ------------------------------------------------------------------
    (
        "get_map_viewport",
        "Get the current map viewport: center lat/lon, MapLibre zoom, bbox, canvas size.",
        {"type": "object", "additionalProperties": False},
    ),
    (
        "fly_to_point",
        "Pan/zoom the map to (lon, lat) at the given MapLibre zoom (default 10).",
        {
            "type": "object",
            "properties": {
                "lon": {"type": "number"},
                "lat": {"type": "number"},
                "zoom": {"type": "number"},
            },
            "required": ["lon", "lat"],
            "additionalProperties": False,
        },
    ),
    (
        "fly_to_bbox",
        "Fit the map to a geographic bounding box (west, south, east, north, optional padding).",
        {
            "type": "object",
            "properties": {
                "west": {"type": "number"},
                "south": {"type": "number"},
                "east": {"type": "number"},
                "north": {"type": "number"},
                "padding": {"type": "number"},
            },
            "required": ["west", "south", "east", "north"],
            "additionalProperties": False,
        },
    ),
    (
        "get_map_screenshot",
        "PNG of the map canvas only (no surrounding UI chrome).",
        {"type": "object", "additionalProperties": False},
    ),
    (
        "get_map_screenshot_at",
        "Fly to a point or bbox, then take a map-only screenshot in one call.",
        {
            "type": "object",
            "properties": {
                "lon": {"type": "number"},
                "lat": {"type": "number"},
                "zoom": {"type": "number"},
                "west": {"type": "number"},
                "south": {"type": "number"},
                "east": {"type": "number"},
                "north": {"type": "number"},
                "padding": {"type": "number"},
                "settle_ms": {"type": "number"},
            },
            "additionalProperties": False,
        },
    ),
    (
        "select_bbox",
        "Cross-filter the viewer to rows inside the given geographic bbox.",
        {
            "type": "object",
            "properties": {
                "west": {"type": "number"},
                "south": {"type": "number"},
                "east": {"type": "number"},
                "north": {"type": "number"},
            },
            "additionalProperties": False,
        },
    ),
    (
        "clear_selection",
        "Clear the GIS cross-filter brush.",
        {"type": "object", "additionalProperties": False},
    ),
    (
        "count_in_bbox",
        "Read-only count of rows in a geographic bbox (does not touch the brush).",
        {
            "type": "object",
            "properties": {
                "west": {"type": "number"},
                "south": {"type": "number"},
                "east": {"type": "number"},
                "north": {"type": "number"},
            },
            "required": ["west", "south", "east", "north"],
            "additionalProperties": False,
        },
    ),
    (
        "find_nearby",
        "Return places nearest to (lon, lat) within radius_km, sorted by distance.",
        {
            "type": "object",
            "properties": {
                "lon": {"type": "number"},
                "lat": {"type": "number"},
                "radius_km": {"type": "number"},
                "limit": {"type": "number"},
                "columns": {"type": "array", "items": {"type": "string"}},
                "where": {"type": "string"},
            },
            "required": ["lon", "lat"],
            "additionalProperties": False,
        },
    ),
    (
        "density_grid",
        "Bin rows inside a bbox into an nx × ny grid and return per-cell counts.",
        {
            "type": "object",
            "properties": {
                "west": {"type": "number"},
                "south": {"type": "number"},
                "east": {"type": "number"},
                "north": {"type": "number"},
                "nx": {"type": "number"},
                "ny": {"type": "number"},
                "top_k": {"type": "number"},
            },
            "required": ["west", "south", "east", "north"],
            "additionalProperties": False,
        },
    ),
    (
        "highlight_points",
        "Draw temporary markers at given coordinates so they stand out in screenshots.",
        {
            "type": "object",
            "properties": {
                "points": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "lon": {"type": "number"},
                            "lat": {"type": "number"},
                            "label": {"type": "string"},
                            "color": {"type": "string"},
                            "radius": {"type": "number"},
                        },
                        "required": ["lon", "lat"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["points"],
            "additionalProperties": False,
        },
    ),
    (
        "set_basemap_style",
        "Switch the MapLibre basemap (style URL or a built-in name like openfreemap-positron, osm-raster, or none).",
        {
            "type": "object",
            "properties": {"style": {"type": "string"}},
            "required": ["style"],
            "additionalProperties": False,
        },
    ),
]
