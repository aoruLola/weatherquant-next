#!/usr/bin/env bash
# Shared Docker acceptance for the dashboard image.
#
#   scripts/dashboard_docker_accept.sh <image>
#
# Called by BOTH dashboard-ci.yml and dashboard-release.yml so the two can
# never drift apart (the failure mode that bit shadow-release once already).
# Tests for this script's invariants live in tests/test_dashboard_ci.py.
#
# What it proves, in order:
#   1. the container starts against read-only fixture mounts and goes healthy
#   2. it runs as a non-root user
#   3. the API answers with the versioned envelope (and the fixture stamp)
#   4. the read-only mounts really are read-only
#   5. no .duckdb file is even visible to the container - no lock can exist
#   6. the published port serves the built SPA
#   7. a restart recovers without touching the sources
#   8. SIGTERM stops it gracefully

set -euo pipefail

IMAGE="${1:?usage: dashboard_docker_accept.sh <image>}"
CONTAINER="wq-dashboard-accept"
WORK="$(mktemp -d)"

cleanup() {
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT

echo "== generating fixture sources (read-only inputs) =="
# helpers.py is stdlib-only, so any python3 will do - no venv required.
python3 dashboard/fixtures/generate.py --root "$WORK/sources" >/dev/null
# The image runs as uid 1000; a directory owned by the runner (1001) would
# not be readable after the bind mount masks the image's own ownership.
sudo chown -R 1000:1000 "$WORK/sources"

echo "== start =="
docker run -d --name "$CONTAINER" \
  -p 8787:8787 \
  -v "$WORK/sources/collector:/sources/collector:ro" \
  -v "$WORK/sources/shadow:/sources/shadow:ro" \
  -e WQ_COLLECTOR_DATA_DIR=/sources/collector \
  -e WQ_SHADOW_DATA_DIR=/sources/shadow \
  -e WQ_DATASET=fixture \
  "$IMAGE"

echo "== health =="
healthy=""
for _ in $(seq 1 40); do
  if docker exec "$CONTAINER" python /usr/local/bin/healthcheck.py >/dev/null 2>&1; then
    healthy=1
    break
  fi
  sleep 1
done
test -n "$healthy" || { echo "container never became healthy" >&2; docker logs "$CONTAINER" | tail -50 >&2; exit 1; }
docker exec "$CONTAINER" python /usr/local/bin/healthcheck.py

echo "== non-root =="
uid="$(docker exec "$CONTAINER" id -u)"
echo "uid=$uid"
test "$uid" != "0"

echo "== API envelope and fixture stamp =="
docker exec "$CONTAINER" python - <<'PY'
import json, urllib.request

def get(path):
    with urllib.request.urlopen(f"http://127.0.0.1:8787{path}", timeout=10) as r:
        assert r.status == 200, f"{path} -> {r.status}"
        return json.loads(r.read())

health = get("/healthz")
assert health["schema_version"] == 1, health
assert health["dataset"] == "fixture", "fixture sources must be stamped as fixture"
assert set(health["data"]["sources"]) == {
    "collector_heartbeat", "shadow_heartbeat", "shadow_report"
}, health

overview = get("/api/v1/overview")
assert overview["api"] == "overview"
assert overview["data"]["collector"]["present"] is True
assert overview["data"]["shadow"]["mode"] in ("LIVE", "REPLAY", "UNKNOWN")

markets = get("/api/v1/markets?limit=5")
assert markets["data"]["total"] >= 1, "fixture archive produced no markets"
assert markets["data"]["items"][0]["market_id"]

decisions = get("/api/v1/decisions")
assert decisions["data"]["totals_status"] in ("fresh", "stale"), decisions["data"]
assert decisions["data"]["disclaimer"], "the ACCEPT disclaimer must be in the payload"

# a corrupt-input request must be a 422, not a 500
import urllib.error
try:
    get("/api/v1/markets?limit=0")
    raise SystemExit("limit=0 should be rejected")
except urllib.error.HTTPError as e:
    assert e.code == 422, e.code
print("API OK")
PY

echo "== read-only mounts cannot be written =="
if docker exec "$CONTAINER" sh -c 'touch /sources/collector/health/.write-probe 2>/dev/null'; then
  echo "wrote to a read-only mount" >&2
  exit 1
fi
if docker exec "$CONTAINER" sh -c 'touch /sources/shadow/reports/.write-probe 2>/dev/null'; then
  echo "wrote to a read-only mount" >&2
  exit 1
fi
echo "mounts are read-only"

echo "== no database is even visible =="
docker exec "$CONTAINER" python - <<'PY'
import pathlib
hits = [str(p) for root in ("/sources", "/app") for p in pathlib.Path(root).rglob("*.duckdb")]
assert not hits, f"container can see database files: {hits}"
print("no .duckdb anywhere in the container's view")
PY

echo "== published port serves the SPA =="
python3 - <<'PY'
import time, urllib.request
for _ in range(20):
    try:
        with urllib.request.urlopen("http://127.0.0.1:8787/", timeout=5) as r:
            body = r.read().decode()
            assert r.status == 200 and "<div id=\"root\">" in body, body[:200]
        break
    except Exception:
        time.sleep(1)
else:
    raise SystemExit("SPA not served on 8787")
print("SPA OK")
PY

echo "== sources unchanged by the run =="
before="$(find "$WORK/sources" -type f -exec sha256sum {} \; | sort)"

echo "== restart recovers =="
docker restart "$CONTAINER" >/dev/null
healthy=""
for _ in $(seq 1 40); do
  if docker exec "$CONTAINER" python /usr/local/bin/healthcheck.py >/dev/null 2>&1; then
    healthy=1
    break
  fi
  sleep 1
done
test -n "$healthy" || { echo "container unhealthy after restart" >&2; docker logs "$CONTAINER" | tail -50 >&2; exit 1; }

after="$(find "$WORK/sources" -type f -exec sha256sum {} \; | sort)"
test "$before" = "$after" || { echo "the dashboard modified its sources" >&2; exit 1; }
echo "sources untouched"

echo "== graceful stop =="
start="$(date +%s)"
docker stop -t 20 "$CONTAINER" >/dev/null
elapsed=$(( $(date +%s) - start ))
echo "stopped in ${elapsed}s"
test "$elapsed" -lt 20 || { echo "SIGTERM was not handled inside the grace period" >&2; exit 1; }

echo "dashboard docker acceptance: PASS"
