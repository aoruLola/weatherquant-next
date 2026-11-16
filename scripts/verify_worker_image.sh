#!/usr/bin/env bash
# WQN-4. With no image argument, build a candidate from the checkout.
# With an existing image, verify it against the specified Git commit (default HEAD).
set -euo pipefail

if ! docker version >/dev/null 2>&1; then
  docker() { sudo -n docker "$@"; }
fi

target="${1:-}"
sha="$(git rev-parse --verify "${2:-HEAD}^{commit}")"
version="$(git show "$sha:pyproject.toml" | sed -n 's/^version = "\([^"]*\)"$/\1/p')"
if [[ -z "$target" ]]; then
  target="wqn4-verify:${sha:0:12}"
  docker build --build-arg "GIT_SHA=$sha" -t "$target" .
fi

image_id="$(docker image inspect --format '{{.Id}}' "$target")"
revision="$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$image_id")"
echo "image=$target image_id=$image_id expected_sha=$sha revision=$revision"
[[ "$revision" == "$sha" ]] || { echo "image revision mismatch" >&2; exit 1; }

site="$(docker run --rm --network none --entrypoint python "$image_id" -c \
  'import pathlib,weatherquant; print(pathlib.Path(weatherquant.__file__).parent)')"
for module in worker writer archive watchdog; do
  path="data/$module.py"
  expected="$(git show "$sha:src/weatherquant/$path" | sha256sum)"
  actual="$(docker run --rm --network none --entrypoint cat "$image_id" "$site/$path" | sha256sum)"
  echo "$path sha256=${actual%% *}"
  [[ "$actual" == "$expected" ]] || { echo "$path differs from $sha" >&2; exit 1; }
done

# Use the image's own provenance defaults and an isolated, writable data volume.
volume="$(docker volume create)"
trap 'docker volume rm "$volume" >/dev/null' EXIT
docker run --rm --network none -v "$volume:/data" \
  -e WQ_HTTP_TIMEOUT_SECONDS=1 -e WQ_HTTP_RETRIES=0 "$image_id" --once --no-stream
docker run --rm -i --network none --entrypoint python -v "$volume:/data:ro" "$image_id" - "$sha" "$version" <<'PY'
import json
import sys

import duckdb

sha, version = sys.argv[1:]
expected = (sha, "sha-" + sha, version)
with duckdb.connect("/data/weatherquant.duckdb", read_only=True) as db:
    runs = db.execute("SELECT git_sha, image_tag, app_version FROM collector_runs").fetchall()
assert runs == [expected], (runs, expected)
with open("/data/health/heartbeat.json") as handle:
    beat = json.load(handle)
stamp = (beat.get("git_sha"), beat.get("image_tag"), beat["app_version"])
assert stamp == expected, (stamp, expected)
print("verified collector_runs and heartbeat:", *stamp)
PY
