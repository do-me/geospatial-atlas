#!/bin/bash
# End-to-end perf integration run for the synthetic 300M-row probe.
#
# Steps:
#   0. Existing fast-load pytest suite passes (ensures Phase 0 cleanup is
#      stable).
#   1. Validate europe_300m.parquet exists; regenerate if --regen.
#   2. Re-time the loader (fast_load_parquet) end-to-end on the file.
#   3. Restart the server with the file; wait until ready.
#   4. Re-run the wire bench (scatter + scatter+cat + histograms).
#   5. Run the Playwright frontend-fluency probe.
#   6. Print a one-line summary per phase.
#
# Usage:
#   bash scripts/bench/run_all.sh            # reuse existing /tmp/europe_300m.parquet
#   bash scripts/bench/run_all.sh --regen    # regenerate
set -euo pipefail
cd "$(dirname "$0")/../.."

PARQUET=${PARQUET:-/tmp/gsa_bench/europe_300m.parquet}
PORT=${PORT:-5055}
LOG=/tmp/gsa_300m.log
PIDFILE=/tmp/gsa_run.pid

heading() { printf "\n========== %s ==========\n" "$1"; }

heading "0/6  fast-load pytest suite"
( cd packages/backend && uv run pytest tests/test_fast_load.py -q 2>&1 | tail -n 3 )

heading "1/6  dataset"
SCRIPT_DIR="$PWD/scripts/bench"
if [[ "${1:-}" == "--regen" || ! -f "$PARQUET" ]]; then
  rm -f "$PARQUET"
  uv --directory packages/backend run python "$SCRIPT_DIR/gen_points.py" \
    --rows 300000000 --backend duckdb --out "$PARQUET" 2>&1 | tail -n 1
fi
uv --directory packages/backend run python "$SCRIPT_DIR/validate_points.py" \
  "$PARQUET" --rows 300000000

heading "2/6  loader (fast_load_parquet on 300M)"
uv --directory packages/backend run python -c "
from embedding_atlas.fast_load import fast_load_parquet
import time
t0 = time.perf_counter()
res = fast_load_parquet('$PARQUET')
print(f'  fast_load_parquet  {time.perf_counter()-t0:5.2f}s  rows={res.row_count:,}')
"

heading "3/6  server start"
PID=$(cat $PIDFILE 2>/dev/null || true)
if [[ -n "${PID:-}" ]]; then kill "$PID" 2>/dev/null || true; fi
lsof -i ":$PORT" -t 2>/dev/null | xargs -r kill -9 2>/dev/null || true
sleep 2
nohup uv --directory packages/backend run geospatial-atlas "$PARQUET" \
  --host 127.0.0.1 --port "$PORT" > "$LOG" 2>&1 &
echo $! > "$PIDFILE"
SERVER_T0=$(date +%s)
until curl -sS "http://127.0.0.1:$PORT/" -o /dev/null -w "%{http_code}\n" 2>/dev/null | grep -q "^200$"; do
  sleep 2
done
SERVER_DT=$(( $(date +%s) - SERVER_T0 ))
echo "  server up in ${SERVER_DT}s"

heading "4/6  wire bench (HTTP + Arrow IPC)"
URL="http://127.0.0.1:$PORT/data/query" bash scripts/bench/bench_wire.sh 2>&1 | grep -E "scatter|hist|cat_count" | head -n 6

heading "5/6  frontend fluency probe (Playwright + WebGPU)"
PORT=$PORT npx playwright test e2e/europe-300m.spec.ts --project=perf-chrome \
  --workers=1 --reporter=list 2>&1 \
  | grep -E "passed|failed|cssPanApplied|renderCalls|canvases at|====" | head -n 12

heading "6/6  summary"
echo "  parquet:    $PARQUET ($(du -h "$PARQUET" | cut -f1))"
echo "  bundle:     $(grep -oE 'index-[A-Za-z0-9_]+\.js' packages/backend/embedding_atlas/static/index.html | head -n 1)"
echo "  server PID: $(cat $PIDFILE) (port $PORT)"
echo "  pid file:   $PIDFILE  | log: $LOG"
echo "  shut down:  kill \$(cat $PIDFILE)"
