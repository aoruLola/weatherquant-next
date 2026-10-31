#!/usr/bin/env bash
# Only PR builds are selective. Main and manual checks exercise every image.
set -euo pipefail
if [[ "$GITHUB_EVENT_NAME" != pull_request ]]; then
  echo build_image=true
  exit 0
fi

inputs=(src pyproject.toml uv.lock README.md .dockerignore .gitattributes scripts/ci_image_gate.sh)
case "$1" in
  collector) dockerfile=Dockerfile; inputs+=(docker/healthcheck.py .github/workflows/ci.yml) ;;
  shadow) dockerfile=Dockerfile.shadow; inputs+=(docker/healthcheck_shadow.py configs models .github/workflows/shadow-ci.yml) ;;
  dashboard) dockerfile=Dockerfile.dashboard; inputs+=(docker/healthcheck_dashboard.py dashboard scripts/dashboard_docker_accept.sh .github/workflows/dashboard-ci.yml) ;;
  *) exit 2 ;;
esac
inputs+=("$dockerfile" "$dockerfile.dockerignore")

# Actions checks out the synthetic PR merge commit. Its first parent is the
# exact base used for this run, even for a PR containing multiple commits.
if git diff --quiet HEAD^1 HEAD -- "${inputs[@]}"; then
  echo build_image=false
else
  status=$?
  [[ "$status" == 1 ]] || exit "$status"
  echo build_image=true
fi
