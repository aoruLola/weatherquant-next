#!/usr/bin/env bash
# WQN-2 container-level acceptance: a blocked loop is *identified* by the
# independent heartbeat probe, and the container *recovers itself* via the
# watchdog exit + Docker restart policy - without duplicating writes.
#
# Two scenarios, two containers (both from the locally built image):
#
#   A. 识别   - watchdog disabled; block 150s > the probe's 120s staleness
#               threshold; the container must go UNHEALTHY on its own.
#   B. 恢复   - watchdog at its default 120s; block 130s; the process must
#               exit, Docker must restart it, and the restarted run must be
#               healthy with no duplicated data.
#
# Evidence is printed as it is collected; the script's exit code is 0 only if
# every assertion holds. Runtime is dominated by the real 120s thresholds:
# expect ~6 minutes.
set -euo pipefail

CONTAINER_A=
CONTAINER_B=
DATA_A=
DATA_B=

# Some hosts only expose the docker socket to root; use sudo when needed.
if ! docker version >/dev/null 2>&1; then
  docker() { sudo -n docker "$@"; }
fi

say() { printf '\n=== %s ===\n' "$*"; }

cleanup() {
  for container in "$CONTAINER_A" "$CONTAINER_B"; do
    if [[ -n "$container" ]]; then docker rm -f "$container" >/dev/null 2>&1 || true; fi
  done
  for volume in "$DATA_A" "$DATA_B"; do
    if [[ -n "$volume" ]]; then docker volume rm "$volume" >/dev/null 2>&1 || true; fi
  done
}
trap cleanup EXIT

health_of() { docker inspect --format '{{.State.Health.Status}}' "$1" 2>/dev/null || echo gone; }
restarts_of() { docker inspect --format '{{.RestartCount}}' "$1" 2>/dev/null || echo 0; }

wait_health() { # container target timeout_s label
  local container="$1" target="$2" timeout_s="$3" label="$4"
  local deadline=$((SECONDS + timeout_s))
  while (( SECONDS < deadline )); do
    if [ "$(health_of "$container")" = "$target" ]; then
      echo "$label: reached '$target' after ${SECONDS}s"
      return 0
    fi
    sleep 3
  done
  echo "$label: FAILED - still '$(health_of "$container")' after ${timeout_s}s" >&2
  return 1
}

if [[ -z "${IMAGE:-}" ]]; then
  IMAGE=wqn2-recovery:test
  say "build current checkout"
  docker build -f Dockerfile -t "$IMAGE" .
fi
docker image inspect --format 'Testing image {{.Id}}' "$IMAGE"

arm_block() {
  docker run --rm -v "$1":/data --entrypoint sh "$IMAGE" \
    -c 'printf "%s\n" "$1" > /data/.fault-block-main' sh "$2"
}

# ---------------------------------------------------------------------------
say "scenario A: the independent probe identifies a >120s block"
BLOCK_A=150
DATA_A=$(docker volume create)
arm_block "$DATA_A" "$BLOCK_A"
CONTAINER_A=$(docker run -d \
  --restart unless-stopped \
  -v "$DATA_A":/data \
  -e WQ_HTTP_TIMEOUT_SECONDS=6 -e WQ_HTTP_RETRIES=1 -e WQ_LOG_LEVEL=INFO \
  --health-interval 5s --health-timeout 3s --health-retries 2 --health-start-period 15s \
  "$IMAGE" --no-stream --loop-stuck-seconds 0 --discovery-seconds 5 --book-poll-seconds 2)
wait_health "$CONTAINER_A" healthy 60 "A: initial"
# The block is ${BLOCK_A}s; the probe calls a heartbeat stale at 120s. The
# budget tracks the block (not a fixed number) because the unhealthy window
# only exists between staleness and the block's end, and a loaded host can
# stretch container startup.
wait_health "$CONTAINER_A" unhealthy $((BLOCK_A + 120)) "A: probe flagged the blocked loop"
docker inspect --format '{{range .State.Health.Log}}{{println .Output}}{{end}}' "$CONTAINER_A" | grep -F 'heartbeat stale'
echo "A: RestartCount=$(restarts_of "$CONTAINER_A") (expected 0: watchdog inert)"
[ "$(restarts_of "$CONTAINER_A")" = "0" ]
docker logs "$CONTAINER_A" 2>&1 | grep -E "fault injection|loop watchdog" | tail -3 || true
docker rm -f "$CONTAINER_A" >/dev/null
CONTAINER_A=

# ---------------------------------------------------------------------------
say "scenario B: watchdog exit + restart policy recover the container"
DATA_B=$(docker volume create)
arm_block "$DATA_B" 130
CONTAINER_B=$(docker run -d \
  --restart unless-stopped \
  -v "$DATA_B":/data \
  -e WQ_HTTP_TIMEOUT_SECONDS=6 -e WQ_HTTP_RETRIES=1 -e WQ_LOG_LEVEL=INFO \
  --health-interval 5s --health-timeout 3s --health-retries 2 --health-start-period 15s \
  "$IMAGE" --no-stream --discovery-seconds 5 --book-poll-seconds 2)
wait_health "$CONTAINER_B" healthy 90 "B: initial"
policy=$(docker inspect --format '{{.HostConfig.RestartPolicy.Name}}' "$CONTAINER_B")
echo "B: restart policy = $policy"
[ "$policy" = unless-stopped ]

# Block is 130s; the watchdog exits the process just past 120s, and the
# restart policy brings the container back. Marker was consumed pre-block,
# so the restart runs clean.
deadline=$((SECONDS + 300))
until [ "$(restarts_of "$CONTAINER_B")" -ge 1 ]; do
  if (( SECONDS > deadline )); then echo "B: FAILED - never restarted" >&2; exit 1; fi
  sleep 3
done
echo "B: automatic restart observed (RestartCount=$(restarts_of "$CONTAINER_B"))"
wait_health "$CONTAINER_B" healthy 120 "B: recovered"
docker logs "$CONTAINER_B" 2>&1 | grep -F "collection loop stuck"
deadline=$((SECONDS + 120))
until docker exec "$CONTAINER_B" python -c '
import json
from pathlib import Path
stats = json.loads(Path("/data/health/heartbeat.json").read_text())["stats"]
raise SystemExit(not (stats["books_rows_inserted"] and stats["price_points_inserted"]))'; do
  if (( SECONDS >= deadline )); then echo "B: no recovered book and price writes" >&2; exit 1; fi
  sleep 3
done
[ "$(restarts_of "$CONTAINER_B")" = 1 ]

say "B: no duplicated writes after the restart"
# Stop the collector *gracefully* before opening the database: DuckDB allows
# one process at a time on the file, so a second reader cannot attach while
# the (recovered, running) collector holds the write lock. Graceful also
# means the recovered run closes out as 'stopped' - leaving exactly the
# watchdog-killed run in a non-stopped state, which the check asserts.
docker stop -t 60 "$CONTAINER_B" >/dev/null
docker rm "$CONTAINER_B" >/dev/null
CONTAINER_B=
# -i carries the heredoc into the container; without it python reads an empty
# stdin, runs nothing, and exits 0 - a vacuous pass.
docker run --rm -i --entrypoint python -v "$DATA_B":/data:ro "$IMAGE" - <<'PY'
import duckdb
con = duckdb.connect("/data/weatherquant.duckdb", read_only=True)
runs = con.execute("SELECT run_id, status, stop_reason FROM collector_runs ORDER BY started_at_utc").fetchall()
print(f"runs recorded: {len(runs)}")
for run_id, status, reason in runs:
    print(f"  {run_id[:12]}  status={status}  reason={str(reason)[:60]!r}")
assert [r[1] for r in runs] == ["failed", "stopped"], runs
assert "did not exit cleanly" in (runs[0][2] or ""), runs[0]
open_gaps = con.execute("SELECT COUNT(*) FROM collection_gaps WHERE ended_at_utc IS NULL").fetchone()[0]
assert open_gaps == 0, f"{open_gaps} gap(s) left open"
books = con.execute("SELECT COUNT(*) FROM order_books").fetchone()[0]
distinct = con.execute("SELECT COUNT(DISTINCT book_id) FROM order_books").fetchone()[0]
assert books == distinct and books > 0, f"invalid book rows: {books} vs {distinct}"
dupes = con.execute(
    "SELECT COUNT(*) FROM (SELECT market_id, token_id, ts, source FROM price_history "
    "GROUP BY 1,2,3,4 HAVING COUNT(*) > 1)"
).fetchone()[0]
assert dupes == 0, f"{dupes} duplicated price rows"
prices = con.execute("SELECT COUNT(*) FROM price_history").fetchone()[0]
assert prices > 0, "no price observations"
print(f"order_books={books} (all distinct)  price_history={prices} (no dup natural keys)")
print("no-duplicate verification passed")
PY

printf '\nALL SCENARIOS PASSED\n'
