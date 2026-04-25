#!/bin/bash
# Time the wire round-trip for the queries the viewer actually issues at
# 300M scale: u16-packed scatter (no color), u16-packed scatter + category
# index (color-by), and a few histograms. Saves bodies under /tmp so we can
# inspect the real Arrow payload shape.

set -u
URL=${URL:-http://127.0.0.1:5055/data/query}
OUT=${OUT:-/tmp/gsa_bench/wire}
mkdir -p "$OUT"

bench() {
  local name=$1
  local accept=$2
  local sql=$3
  local body
  body=$(printf '{"sql":%s,"type":"%s"}' "$(jq -Rsa <<< "$sql")" "$accept")
  printf "  %-32s " "$name"
  /usr/bin/time -l -p curl -sS -X POST "$URL" \
    -H "Content-Type: application/json" \
    --data "$body" \
    -o "$OUT/$name" \
    -w "  status=%{http_code} size=%{size_download} dur=%{time_total}s xfer=%{speed_download}\n" 2>&1 \
    | grep -E "status=|maximum resident set size" \
    | awk '{ if ($0 ~ /resident/) printf("  rss=%s\n", $1); else printf("%s", $0); }'
}

# Scatter, no color: x,y as u16 over [-10,35]x[35,70]
SCATTER_SQL='SELECT
  GREATEST(0, LEAST(65535, ROUND((lon - (-10.0))/45.0 * 65535)))::USMALLINT AS x,
  GREATEST(0, LEAST(65535, ROUND((lat - 35.0)/35.0 * 65535)))::USMALLINT AS y
FROM dataset'

# Scatter + category index: x,y,c
SCATTER_CAT_SQL='SELECT
  GREATEST(0, LEAST(65535, ROUND((lon - (-10.0))/45.0 * 65535)))::USMALLINT AS x,
  GREATEST(0, LEAST(65535, ROUND((lat - 35.0)/35.0 * 65535)))::USMALLINT AS y,
  CASE category WHEN '"'"'A'"'"' THEN 0::TINYINT WHEN '"'"'B'"'"' THEN 1::TINYINT
                WHEN '"'"'C'"'"' THEN 2::TINYINT WHEN '"'"'D'"'"' THEN 3::TINYINT
                WHEN '"'"'E'"'"' THEN 4::TINYINT WHEN '"'"'F'"'"' THEN 5::TINYINT
                WHEN '"'"'G'"'"' THEN 6::TINYINT WHEN '"'"'H'"'"' THEN 7::TINYINT
                ELSE 0::TINYINT END AS c
FROM dataset'

# Histogram of value (50 bins)
HIST_VALUE='SELECT FLOOR(value / 2.0)::INT AS bin, COUNT(*)::BIGINT AS n FROM dataset GROUP BY 1 ORDER BY 1'

# Category counts
CAT_COUNT='SELECT category, COUNT(*)::BIGINT AS n FROM dataset GROUP BY 1 ORDER BY 1'

echo "=== type=arrow (binary IPC, what the viewer uses) ==="
bench "scatter_arrow"      arrow "$SCATTER_SQL"
bench "scatter_cat_arrow"  arrow "$SCATTER_CAT_SQL"
bench "hist_value_arrow"   arrow "$HIST_VALUE"
bench "cat_count_arrow"    arrow "$CAT_COUNT"

echo
echo "=== file sizes & encoding sanity ==="
ls -la "$OUT" | tail -n +2 | awk '{ printf("  %-32s %14s bytes\n", $NF, $5) }' | grep -v ' total'
