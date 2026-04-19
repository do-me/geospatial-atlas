#!/usr/bin/env bash
# Thin MCP Streamable HTTP client for ad-hoc exploration.
#
# Usage:
#   mcp.sh list                          # tools/list
#   mcp.sh call <tool>                   # tools/call with {} args
#   mcp.sh call <tool> '<json_args>'     # tools/call with supplied args
#   mcp.sh sql "<sql>"                   # shorthand for run_sql_query
#
# Env:
#   MCP_URL   Default: http://localhost:5055/mcp/
#   OUT_DIR   Where to write screenshots (default: /tmp/gsa-shots)

set -euo pipefail

MCP_URL="${MCP_URL:-http://localhost:5055/mcp/}"
OUT_DIR="${OUT_DIR:-/tmp/gsa-shots}"
mkdir -p "$OUT_DIR"

rpc() {
  # $1 = method, $2 = params JSON
  local method="$1"
  local params="$2"
  local body
  body=$(printf '{"jsonrpc":"2.0","id":1,"method":%s,"params":%s}' \
    "$(printf '%s' "$method" | jq -Rs .)" "$params")
  # Streamable HTTP returns SSE by default; grep the data line, strip "data: ".
  curl -sS -X POST "$MCP_URL" \
    -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' \
    --data "$body" \
  | awk '/^data: /{ sub(/^data: /, ""); print; exit }'
}

case "${1:-}" in
  list)
    rpc 'tools/list' '{}' | jq '.result.tools | map({name, description: (.description // "" | .[0:60])})'
    ;;
  call)
    tool="${2:?tool required}"
    args="${3:-}"
    [ -z "$args" ] && args='{}'
    params=$(jq -nc --arg n "$tool" --argjson a "$args" '{name:$n, arguments:$a}')
    resp=$(rpc 'tools/call' "$params")
    # Extract text or save image to disk
    echo "$resp" | jq -r '
      if .error then "ERROR: \(.error.message)"
      else
        (.result.content // []) | map(
          if .type == "text" then .text
          elif .type == "image" then "IMAGE(\(.mimeType)) b64len=\(.data|length)"
          else tostring end
        ) | .[]
      end'
    # Also dump any image blocks to files
    echo "$resp" | jq -r '(.result.content // [])[] | select(.type == "image") | .data' \
      | while read -r data; do
          [ -z "$data" ] && continue
          ts=$(date +%s%N)
          out="$OUT_DIR/shot_${ts}.png"
          echo "$data" | base64 -d > "$out"
          echo "  → saved $out"
        done
    ;;
  sql)
    query="${2:?sql required}"
    args=$(jq -nc --arg q "$query" '{query:$q}')
    params=$(jq -nc --argjson a "$args" '{name:"run_sql_query", arguments:$a}')
    rpc 'tools/call' "$params" | jq -r '.result.content[0].text' | jq .
    ;;
  raw)
    # raw <method> '<params json>' — for debugging
    method="${2:?method}"
    params="${3:-{}}"
    rpc "$method" "$params"
    ;;
  *)
    echo "usage: $0 {list|call <tool> [args]|sql <sql>|raw <method> [params]}" >&2
    exit 1
    ;;
esac
