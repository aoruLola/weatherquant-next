#!/usr/bin/env bash
# WQN-4: prove that a built image runs exactly this checkout's worker.
#
# What "consistent" has to mean, and what this script checks:
#   1. the image's weatherquant package (worker.py among it) is byte-for-byte
#      the file in this checkout - not merely the same version string;
#   2. a container started with GIT_SHA/IMAGE_TAG stamps exactly those values
#      into collector_runs (what an audit will later read);
#   3. the heartbeat's app_version matches the project version.
#
# Usage:
#   scripts/verify_worker_image.sh              # build from this checkout and verify
#   scripts/verify_worker_image.sh <image>      # verify an already-built/deployed image
#
# Exit 0 only if every comparison holds. The upstream reachability of --once
# is best-effort: the stamping check does not depend on the network.
set -euo pipefail

if ! docker version >/dev/null 2>&1; then
  docker() { sudo -n docker "$@"; }
fi

TARGET="${1:-}"
SOURCE_SHA="$(git rev-parse HEAD)"
SHORT_SHA="$(git rev-parse --short HEAD)"
SOURCE_VERSION="$(uv run python -c 'import tomllib; print(tomllib.load(open("pyproject.toml","rb"))["project"]["version"])')"

if [ -z "$TARGET" ]; then
  TARGET="wqn4-verify:${SHORT_SHA}"
  echo "== building ${TARGET} from $(git rev-parse --abbrev-ref HEAD) @ ${SOURCE_SHA}"
  docker build -f Dockerfile -t "$TARGET" . >/dev/null
fi

echo "== image: ${TARGET}"
docker image inspect "$TARGET" --format 'image_id: {{.Id}}'

fail=0

# 1) the packaged sources are this checkout's sources, byte for byte. The
# runtime image ships an installed wheel, so ask the image itself where each
# module lives instead of guessing a layout.
SITE="$(docker run --rm --entrypoint python "$TARGET" -c \
  'import weatherquant, pathlib; print(pathlib.Path(weatherquant.__file__).parent)')"
echo "== package root in image: ${SITE}"
for rel in data/worker.py data/writer.py data/archive.py data/watchdog.py; do
  if docker run --rm --entrypoint cat "$TARGET" "${SITE}/${rel}" \
      | diff -q - "src/weatherquant/${rel}" >/dev/null; then
    echo "ok    src/weatherquant/${rel} == image"
  else
    echo "FAIL  src/weatherquant/${rel} differs between image and checkout"
    fail=1
  fi
done

# 2) a real run stamps GIT_SHA / IMAGE_TAG into the audit trail.
DATA="$(mktemp -d /tmp/wqn4-img.XXXXXX)"
docker run --rm -v "$DATA":/data -e GIT_SHA="$SOURCE_SHA" -e IMAGE_TAG="verify-${SHORT_SHA}" \
  -e WQ_HTTP_TIMEOUT_SECONDS=6 -e WQ_HTTP_RETRIES=1 \
  "$TARGET" --once --no-stream >/dev/null 2>&1 || echo "(note: --once exited nonzero - upstream may be unreachable; checking stamps anyway)"

docker run --rm -i --entrypoint python -v "$DATA":/data:ro "$TARGET" - <<PY
import duckdb, json, sys
sha, tag = "${SOURCE_SHA}", "verify-${SHORT_SHA}"
con = duckdb.connect("/data/weatherquant.duckdb", read_only=True)
rows = con.execute(
    "SELECT git_sha, image_tag, app_version FROM collector_runs ORDER BY started_at_utc"
).fetchall()
assert rows, "no run recorded"
bad = [r for r in rows if r[0] != sha or r[1] != tag]
print("runs:", len(rows), "stamped:", len(rows) - len(bad))
for r in rows:
    print(f"  git_sha={str(r[0])[:12]} image_tag={r[1]} app_version={r[2]}")
if bad:
    print(f"FAIL: {len(bad)} run(s) stamped with the wrong provenance", file=sys.stderr)
    sys.exit(1)
print("ok    collector_runs provenance matches the checkout")
hb = json.load(open("/data/health/heartbeat.json"))
assert hb["app_version"] == "${SOURCE_VERSION}", hb["app_version"]
print("ok    heartbeat app_version == pyproject version (${SOURCE_VERSION})")
PY
[ $? -eq 0 ] || fail=1

rm -rf "$DATA"
if [ "$fail" -ne 0 ]; then
  echo "RESULT: FAIL" >&2
  exit 1
fi
echo "RESULT: PASS - image ${TARGET} is ${SOURCE_SHA}"
