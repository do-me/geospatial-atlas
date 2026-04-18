# Connecting an LLM agent to Geospatial Atlas via MCP

Geospatial Atlas ships a **Model Context Protocol** server so that any MCP-capable LLM
(Claude Desktop, Claude Code, Cursor, Continue, your own SDK client, …) can drive the
viewer in real time: run SQL against your data, list/add/update/delete charts, change
column styles, capture screenshots.

The transport is **Streamable HTTP** — no bridge scripts, no Node.js, no stdio shim.

## Setup (3 steps)

### 1. Start the server with `--mcp`

```bash
# Python CLI (available today)
uv run geospatial-atlas /path/to/your.parquet --mcp

# Or the native app (coming in a follow-up release)
```

The URL banner printed on launch is where the viewer *and* MCP clients connect:

```
  ➜ URL: http://localhost:5055
  ➜ MCP server: http://localhost:5055/mcp
```

### 2. Open the viewer in a browser

```
http://localhost:5055
```

The viewer is where the tool handlers actually execute (chart state, Mosaic coordinator,
canvases all live in the webview). Keep the tab open while you're chatting with the LLM.

### 3. Point your LLM at `/mcp`

**Claude Desktop** — edit `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "geospatial-atlas": {
      "url": "http://localhost:5055/mcp"
    }
  }
}
```

Fully quit Claude Desktop (⌘Q) and reopen. You should see 19 tools show up in the
tool-picker.

**Claude Code / Cursor / Continue / any other client**: same — they all accept a URL
entry for MCP servers. Check their docs for the exact config path.

**Programmatic / raw** — any HTTP client:

```bash
curl -s -X POST http://localhost:5055/mcp/ \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  --data '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' \
  | grep '^data: ' | head -1 | cut -c7-
```

That returns the live tools list as JSON.

## The 19 tools

### Data
- `get_data_schema` — table name + column list (names, types)
- `run_sql_query` — readonly SQL against the loaded DuckDB

### Charts
- `list_charts`, `add_chart`, `delete_chart`
- `get_chart_spec`, `set_chart_spec`
- `get_chart_state`, `set_chart_state`, `clear_chart_state`
- `get_chart_screenshot` — PNG of one chart

### Layout
- `get_layout_type`, `set_layout_type` (`"list"` / `"dashboard"`)
- `get_layout_state`, `set_layout_state`
- `get_full_screenshot` — PNG of the entire viewer

### Rendering
- `list_renderers` — column renderer types (text, number, timestamp, bar, …)
- `get_column_styles`, `set_column_style`

## Architecture

```
Claude Desktop / Cursor / etc.   ─── Streamable HTTP ──▶  /mcp
                                                            │
                                                            │ JSON-RPC forward
                                                            ▼
                                                 WebSocketHandler
                                                            │
                                                            │ ws://…/data/mcp_websocket
                                                            ▼
                                                 Viewer JavaScript
                                                            │
                                                            │ executes
                                                            ▼
                                                 DuckDB / charts / canvas
```

The Python side (`packages/backend/embedding_atlas/mcp_bridge.py`) is a pure
protocol adapter. The tool implementations themselves live in the viewer
(`packages/viewer/src/model_context/model_context.ts`) — a tool added there
becomes available to MCP clients automatically with zero Python changes.

## Caveats

- **Viewer browser tab must be open.** The tools run in the webview — close the tab and
  `tools/call` returns an error. On `tools/list`, a warning logs and the cached
  tool list (if any) is returned.
- **One active viewer per server.** Opening a second tab disconnects the first. The
  last-connected viewer owns the MCP session.
- **Read-only SQL enforced server-side.** The backend DuckDB connection has
  `enable_external_access = false` and `lock_configuration = true`. Attempts to
  mutate data will error out, not succeed silently.
- **MCP needs `--mcp` to be enabled.** The CLI ships it as an opt-in flag; by default
  MCP endpoints are not mounted. The native app ships with it `mcp=False` today —
  a future "Enable Claude Desktop integration" toggle will wire it up in the UI.

## Example prompt

> I just loaded an Overture Places parquet file. Use the geospatial-atlas tools to:
> 1. Describe the schema.
> 2. Count rows per `primary_category` and show me the top 10.
> 3. Add a density plot coloured by category.
> 4. Switch the layout to dashboard and take a screenshot.

Claude will chain `get_data_schema` → `run_sql_query` → `add_chart` (density spec)
→ `set_column_style` → `set_layout_type` → `get_full_screenshot`. You'll see each
step happen live in the browser.

## Legacy endpoint

The pre-standard `POST /mcp` endpoint (simple JSON-RPC forwarder, no session, no SSE)
is still available at `/mcp_legacy` for backwards compatibility. New integrations
should use the standard `/mcp` endpoint.
