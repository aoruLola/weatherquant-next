#!/usr/bin/env bash
# The production scheduler for the streaming research pipeline (WQN-28).
#
# Two roles share one state database, at two cadences:
#
#   ingest   every 5 min (cron): consume new decision parts, publish nothing
#   publish  every 20 min (cron): rebuild and publish the six snapshots
#
# The cadence belongs to cron (Unraid User Scripts or the NAS crontab), not to
# this script: a resident daemon is one more thing to monitor and one more
# thing that dies silently. Cron invokes one role per tick; this script does
# exactly one guarded invocation and appends one structured log line.
#
# Serialization: both roles open the same state DB, and DuckDB allows a single
# writer. `flock -n` on ONE lock file shared by both roles makes the second
# arrival skip instead of queue - the next cron tick catches up, and a skipped
# publish costs 20 minutes, not correctness. The skip is logged, never silent.
#
# Crash recovery needs no code here: every invocation is a fresh process, and
# the state store commits consumed rows and the watermark in one transaction -
# a SIGKILL mid-run leaves the previous watermark intact and the next tick
# redoes the work (proven by scripts/test_decouple_processes.sh and by the
# soak in scripts/streaming_soak.py).
#
# Usage (crontab):
#   */5  * * * *  WQ_SCHEDULE_ROLE=ingest  /path/to/streaming_schedule.sh
#   */20 * * * *  WQ_SCHEDULE_ROLE=publish /path/to/streaming_schedule.sh
# or:
#   streaming_schedule.sh ingest
#   streaming_schedule.sh publish
#
# Environment:
#   WQ_EXPORT_DIR          decisions-export directory            (required)
#   WQ_PERFORMANCE_OUT     published generations directory       (required)
#   WQ_STATE_DB            performance state store path          (required)
#   WQ_DATASET             live | fixture                        (default live)
#   WQ_SETTLEMENTS_JSON    settlement index for publish scoring  (optional)
#   WQ_SCHEDULE_LOG        JSONL log (default <state db dir>/schedule.jsonl)
#   WQ_MEMORY_LIMIT        DuckDB budget for the state store     (default 1GB)
#   WQ_RUN_TIMEOUT_SECONDS wall-clock cap per invocation (default 900 = 15
#                          min). A hung run is killed (TERM, then KILL) and
#                          logged as timeout - without it, one stuck ingest
#                          holds the lock and every later tick reads
#                          skipped-locked forever.
#   WQ_LOCK_WAIT_SECONDS   how long a second arrival waits for the lock
#                          (default 0 = skip immediately)
#
# The log line is the acceptance record: role, exit, duration, peak RSS (when
# /usr/bin/time exists), the watermark this run reached and - on skip - that
# it was skipped because another role held the lock.
#
# Everything here touches the research side only: never the shadow's database,
# never a container, never anything outside the three configured paths.

set -uo pipefail

ROLE="${1:-${WQ_SCHEDULE_ROLE:-}}"
if [[ "$ROLE" != "ingest" && "$ROLE" != "publish" ]]; then
  echo "usage: streaming_schedule.sh ingest|publish (or WQ_SCHEDULE_ROLE=...)" >&2
  exit 2
fi

: "${WQ_EXPORT_DIR:?set WQ_EXPORT_DIR to the decisions-export directory}"
: "${WQ_PERFORMANCE_OUT:?set WQ_PERFORMANCE_OUT to the published generations directory}"
: "${WQ_STATE_DB:?set WQ_STATE_DB to the performance state store path}"

DATASET="${WQ_DATASET:-live}"
MEMORY_LIMIT="${WQ_MEMORY_LIMIT:-1GB}"
STATE_DIR="$(dirname "$WQ_STATE_DB")"
LOG="${WQ_SCHEDULE_LOG:-$STATE_DIR/schedule.jsonl}"
LOCK="$STATE_DIR/.pipeline.lock"
TIME_BIN=""
if command -v /usr/bin/time >/dev/null 2>&1; then
  TIME_BIN="/usr/bin/time -v"
fi

mkdir -p "$STATE_DIR" "$WQ_PERFORMANCE_OUT"

PY="${PY:-python}"
ARGS=(
  build-performance
  --export "$WQ_EXPORT_DIR"
  --state-db "$WQ_STATE_DB"
  --out "$WQ_PERFORMANCE_OUT"
  --dataset "$DATASET"
  --memory-limit "$MEMORY_LIMIT"
)
if [[ "$ROLE" == "ingest" ]]; then
  # Ingest only: the next publish will rebuild from what this consumed.
  ARGS+=(--no-publish)
else
  # Publish only: rebuild from the state store, do not touch the export.
  ARGS+=(--no-ingest)
  if [[ -n "${WQ_SETTLEMENTS_JSON:-}" ]]; then
    ARGS+=(--settlements "$WQ_SETTLEMENTS_JSON")
  fi
fi

STARTED="$(date -u +%FT%TZ)"
T0="$(date +%s)"
STATUS="ok"
EXIT=0
RSS_KB=""
OUTPUT=""

# The JSON helpers must NOT depend on $PY: the acceptance's timeout test
# hung exactly there, because a hung/fake PY kept the watermark fallback
# running after timeout had already killed the CLI run. These helpers need
# any python3 - stdlib only - and never the run's own interpreter.
JSON_PY="$(command -v python3 || command -v python || true)"

json_escape() { "$JSON_PY" -c "import json,sys; print(json.dumps(sys.argv[1]))" "$1"; }

log_line() {
  # One JSON line per invocation - the acceptance record. Appended, then the
  # file's data is synced so a NAS power cut cannot reorder it away (local
  # acceptance caught the comment promising fsync without the deed).
  printf '%s\n' "$1" >>"$LOG"
  sync -d "$LOG" 2>/dev/null || true
}

# One lock for both roles. Two fixes from local acceptance:
#  - the crontab now staggers the publish minute (see the Runbook), and this
#    script additionally supports `flock -w` (WQ_LOCK_WAIT_SECONDS, default
#    0 = the old immediate skip) so an operator who prefers waiting over
#    skipping can bound the wait instead of losing the tick;
#  - a skip is still a logged normal event, never silent.
exec 9>"$LOCK" || { echo "cannot open lock file $LOCK" >&2; exit 2; }
LOCK_WAIT="${WQ_LOCK_WAIT_SECONDS:-0}"
if ! flock -w "$LOCK_WAIT" 9; then
  log_line "{\"ts\":\"$STARTED\",\"role\":\"$ROLE\",\"status\":\"skipped-locked\",\"exit\":0,\"duration_s\":0,\"rss_kb\":null,\"watermark\":null}"
  exit 0
fi

# A publish that fires before the first ingest ever ran (cold start: the
# state store does not exist yet) is a scheduling artifact, not a pipeline
# failure. Record it as its own status and let the next tick do the work.
if [[ "$ROLE" == "publish" && ! -f "$WQ_STATE_DB" ]]; then
  log_line "{\"ts\":\"$STARTED\",\"role\":\"publish\",\"status\":\"skipped-empty-state\",\"exit\":0,\"duration_s\":0,\"rss_kb\":null,\"watermark\":null}"
  exit 0
fi

RUN_TIMEOUT="${WQ_RUN_TIMEOUT_SECONDS:-900}"
RUN_CMD=(timeout --signal=TERM --kill-after=60 "$RUN_TIMEOUT")
if [[ -n "$TIME_BIN" ]]; then
  OUTPUT="$("${RUN_CMD[@]}" $TIME_BIN "$PY" -m weatherquant.cli "${ARGS[@]}" 2>&1)"
  EXIT=$?
  # GNU time prints "Maximum resident set size (kbytes): N" in its report.
  RSS_KB="$(printf '%s\n' "$OUTPUT" | sed -n 's/.*Maximum resident set size (kbytes): //p' | tail -1)"
else
  OUTPUT="$("${RUN_CMD[@]}" "$PY" -m weatherquant.cli "${ARGS[@]}" 2>&1)"
  EXIT=$?
fi
if [[ $EXIT -eq 124 || $EXIT -eq 137 ]]; then
  # timeout's own codes: the run was killed for exceeding the wall clock.
  # Recorded as its own status - a hung run is not the same fact as a failed
  # one, and "every later tick reads skipped-locked forever" is the failure
  # mode this cap exists to break.
  STATUS="timeout"
elif [[ $EXIT -ne 0 ]]; then
  STATUS="failed"
fi

DURATION="$(($(date +%s) - T0))"

# The watermark this run reached: the CLI prints it; the export's own
# state.json cursor is the fallback when parsing fails. Recorded, never
# invented.
WATERMARK="$(printf '%s\n' "$OUTPUT" | sed -n 's/^watermark: //p' | tail -1)"
if [[ -z "$WATERMARK" && -f "$WQ_EXPORT_DIR/state.json" ]]; then
  WATERMARK="$("$JSON_PY" -c "
import json
try:
    state = json.load(open('$WQ_EXPORT_DIR/state.json'))
    cursor = state.get('cursor') or {}
    print(str(cursor.get('last_time_utc') or '') + ', ' + str(cursor.get('last_id') or ''))
except Exception:
    print('')
")"
fi

WATERMARK_JSON="$(json_escape "${WATERMARK:-null}")"
log_line "{\"ts\":\"$STARTED\",\"role\":\"$ROLE\",\"status\":\"$STATUS\",\"exit\":$EXIT,\"duration_s\":$DURATION,\"rss_kb\":${RSS_KB:-null},\"watermark\":$WATERMARK_JSON}"

if [[ $EXIT -ne 0 ]]; then
  # The operator's headline evidence: the last 15 lines of a failed run.
  printf '%s\n' "$OUTPUT" | tail -15 >&2
fi
exit $EXIT
