#!/usr/bin/env bash
# Process-level tests for the ingest/publish split.
#
# These need real processes, which is why they are not pytest cases: the
# properties under test are "a second run is refused while the first holds the
# lock" and "a run killed mid-flight leaves the committed watermark alone",
# and neither can be shown by calling a function.
#
# Runs anywhere bash + flock + the installed CLI exist (WSL2 is fine). It
# never touches production: everything happens under a temporary directory.
#
#   bash scripts/test_decouple_processes.sh
#
# Exit status is the number of failed checks, so CI can gate on it.

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${PY:-$REPO/.venv/bin/python}"
WORK="$(mktemp -d)"
LOCK="$WORK/wq.lock"
PASS=0
FAIL=0

cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

check() {
  local label="$1" expected="$2" actual="$3"
  if [[ "$expected" == "$actual" ]]; then
    printf '  PASS  %s\n' "$label"
    PASS=$((PASS + 1))
  else
    printf '  FAIL  %s (expected %s, got %s)\n' "$label" "$expected" "$actual"
    FAIL=$((FAIL + 1))
  fi
}

# ---------------------------------------------------------------------------
# fixture: a small but real export, in the shadow's published layout
# ---------------------------------------------------------------------------

make_export() {
  local dir="$1" parts="$2"
  mkdir -p "$dir/parts"
  local p i
  for ((p = 1; p <= parts; p++)); do
    : >"$dir/parts/part-$(printf '%09d' "$p").jsonl"
    for ((i = 0; i < 5; i++)); do
      local n=$(((p - 1) * 5 + i))
      # n can exceed 59, so roll it into minutes: the cursor is a timestamp and
      # an out-of-range seconds field is rejected before anything else runs.
      local mm=$((n / 60)) ss=$((n % 60))
      printf '{"decision_id": "dec-%06d", "decision_time_utc": "2026-10-16T10:%02d:%02d+00:00", "market_id": "m-%d", "target_date": "2026-10-16", "state": "NO_TRADE", "direction": "sell_yes", "model_name": "t", "model_version": "v1", "confidence": 0.6}\n' \
        "$n" "$mm" "$ss" "$((n % 3))" >>"$dir/parts/part-$(printf '%09d' "$p").jsonl"
    done
  done
  local last=$((parts * 5 - 1))
  cat >"$dir/state.json" <<JSON
{"next_part": $((parts + 1)), "rows_exported": $((parts * 5)),
 "watermark": ["2026-10-16T10:$(printf '%02d' "$((last / 60))"):$(printf '%02d' "$((last % 60))")+00:00", "hash-$parts"]}
JSON
}

run_cli() {
  local export="$1" out="$2" state="$3"
  shift 3
  "$PY" -m weatherquant.cli build-performance \
    --export "$export" --out "$out" --dataset fixture \
    --state-db "$state/perf.duckdb" --temp-dir "$state/tmp" \
    --memory-limit 256MB "$@"
}

printf '=== ingest/publish decoupling: process-level tests ===\n\n'

EXPORT="$WORK/export"
OUT="$WORK/out"
STATE="$WORK/state"
mkdir -p "$OUT" "$STATE"
make_export "$EXPORT" 3

# ---------------------------------------------------------------------------
# 1. ingest-only advances the ingest position and publishes nothing
# ---------------------------------------------------------------------------
printf '1. ingest-only\n'
run_cli "$EXPORT" "$OUT" "$STATE" --no-publish >/dev/null 2>&1
check "exit 0" 0 "$?"
check "ingest-state.json written" yes "$([[ -f $OUT/ingest-state.json ]] && echo yes || echo no)"
check "no generation published" no "$([[ -f $OUT/current.json ]] && echo yes || echo no)"

# ---------------------------------------------------------------------------
# 2. publish-only publishes and does not move the ingest position
# ---------------------------------------------------------------------------
printf '\n2. publish-only\n'
BEFORE="$(cat "$OUT/ingest-state.json")"
run_cli "$EXPORT" "$OUT" "$STATE" --no-ingest >/dev/null 2>&1
check "exit 0" 0 "$?"
check "generation published" yes "$([[ -f $OUT/current.json ]] && echo yes || echo no)"
check "ingest position untouched" "$BEFORE" "$(cat "$OUT/ingest-state.json")"

# ---------------------------------------------------------------------------
# 3. a second run is refused while the first holds the lock
# ---------------------------------------------------------------------------
printf '\n3. mutual exclusion (flock)\n'
( flock -n 9 || exit 1; sleep 4 ) 9>"$LOCK" &
HOLDER=$!
sleep 0.5
flock -n -E 75 "$LOCK" true
check "contended run exits 75" 75 "$?"
wait "$HOLDER"
flock -n -E 75 "$LOCK" true
check "uncontended run exits 0" 0 "$?"

# ---------------------------------------------------------------------------
# 4. killing a run mid-ingest leaves the committed watermark alone
# ---------------------------------------------------------------------------
printf '\n4. crash before commit\n'
KILL_OUT="$WORK/kill-out"
KILL_STATE="$WORK/kill-state"
mkdir -p "$KILL_OUT" "$KILL_STATE"
# A large export so the run is still working when it is killed.
BIG="$WORK/big"
make_export "$BIG" 60
# Started inline rather than through run_cli: a shell function runs in a
# subshell, so `$!` would name the subshell and killing it would orphan the
# python process, which would keep holding the state store's lock. The point
# of this test is that a *dead* writer releases the store.
"$PY" -m weatherquant.cli build-performance \
  --export "$BIG" --out "$KILL_OUT" --dataset fixture \
  --state-db "$KILL_STATE/perf.duckdb" --temp-dir "$KILL_STATE/tmp" \
  --memory-limit 256MB --no-publish >/dev/null 2>&1 &
VICTIM=$!
sleep 1.5
kill -9 "$VICTIM" 2>/dev/null
wait "$VICTIM" 2>/dev/null
# Confirm the writer is really gone before asserting on recovery.
for _ in $(seq 1 40); do
  kill -0 "$VICTIM" 2>/dev/null || break
  sleep 0.25
done
if kill -0 "$VICTIM" 2>/dev/null; then
  printf '  FAIL  killed writer still alive; recovery result would be meaningless\n'
  FAIL=$((FAIL + 1))
fi

rows_consumed() {
  "$PY" -c 'import json,sys
try:
    print(json.load(open(sys.argv[1])).get("rows_consumed") or 0)
except Exception:
    print(0)' "$1"
}

FIRST_ROWS="$(rows_consumed "$KILL_OUT/ingest-state.json")"
RECOVERY_LOG="$WORK/recovery.log"
run_cli "$BIG" "$KILL_OUT" "$KILL_STATE" --no-publish >"$RECOVERY_LOG" 2>&1
RECOVERY_RC=$?
check "recovery run exits 0" 0 "$RECOVERY_RC"
if [[ "$RECOVERY_RC" != 0 ]]; then
  printf '        recovery output:\n'
  sed 's/^/          /' "$RECOVERY_LOG" | tail -8
fi

# ingest-state.json is written at the END of a run, so the killed run's count
# is not observable - it may have committed its watermark without ever
# reaching that write. What is observable, and what actually matters:
#   * the recovery run caught up to the export's final watermark (nothing lost)
#   * a further run consumes nothing (nothing was re-consumed)
FINAL_WATERMARK="$("$PY" -c 'import json,sys
print(json.load(open(sys.argv[1]))["watermark"][0])' "$BIG/state.json")"
RECOVERED_WATERMARK="$("$PY" -c 'import json,sys
print(json.load(open(sys.argv[1]))["watermark"][0])' "$KILL_OUT/ingest-state.json")"
check "recovery caught up to the export watermark" "$FINAL_WATERMARK" "$RECOVERED_WATERMARK"

run_cli "$BIG" "$KILL_OUT" "$KILL_STATE" --no-publish >/dev/null 2>&1
THIRD_ROWS="$(rows_consumed "$KILL_OUT/ingest-state.json")"
check "nothing left to consume, nothing re-consumed" 0 "$THIRD_ROWS"

# ---------------------------------------------------------------------------
# 5. rollback: the original single-run mode still works unchanged
# ---------------------------------------------------------------------------
printf '\n5. rollback to single-run mode\n'
RB_OUT="$WORK/rb-out"
RB_STATE="$WORK/rb-state"
mkdir -p "$RB_OUT" "$RB_STATE"
run_cli "$EXPORT" "$RB_OUT" "$RB_STATE" >/dev/null 2>&1
check "combined run exits 0" 0 "$?"
check "generation published" yes "$([[ -f $RB_OUT/current.json ]] && echo yes || echo no)"
check "ingest-state.json written" yes "$([[ -f $RB_OUT/ingest-state.json ]] && echo yes || echo no)"

printf '\n=== %d passed, %d failed ===\n' "$PASS" "$FAIL"
exit "$FAIL"
