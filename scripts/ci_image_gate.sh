#!/usr/bin/env bash
# Decide whether a development CI run should build + container-accept an image.
#
# Selective gating applies to pull_request only. Push to main and manual
# dispatch (workflow_dispatch) always exercise every image that the workflow
# is capable of building, because there is no "the PR changed these modules"
# signal to argue with - or rather, there is one, but the caller chose to
# run the full matrix anyway.
#
# Usage:
#   bash scripts/ci_image_gate.sh collector
#   bash scripts/ci_image_gate.sh shadow
#   bash scripts/ci_image_gate.sh dashboard
#
# Output (for GitHub Actions `>> "$GITHUB_OUTPUT"`):
#   build_image=true   build + container acceptance SHOULD run
#   build_image=false   skip - the PR did not touch any image input
#
# Exit codes:
#   0  gate produced a decision
#   2  unknown module argument (script misuse)
#   other  git diff failure - let the job fail loudly

set -euo pipefail

# Non-PR events (push to main, workflow_dispatch) always build the image.
# The full matrix is needed there, and there is no synthetic merge commit
# we could diff against.
if [[ "${GITHUB_EVENT_NAME:-}" != "pull_request" ]]; then
  echo build_image=true
  exit 0
fi

# Files that, when changed, should trigger every image build. Shared core
# and the gate script itself are deliberately here: a change to common code
# or to the gating logic affects every module.
inputs=(
  src pyproject.toml uv.lock README.md .dockerignore .gitattributes
  scripts/ci_image_gate.sh
)

# Module-specific inputs. Each module also pulls in its own Dockerfile and
# the corresponding Dockerfile-level ignore file if one exists, because a
# Dockerfile change rebuilds the image regardless of which lines changed.
case "$1" in
  collector)
    dockerfile=Dockerfile
    inputs+=(docker/healthcheck.py .github/workflows/ci.yml)
    ;;
  shadow)
    dockerfile=Dockerfile.shadow
    inputs+=(docker/healthcheck_shadow.py configs models .github/workflows/shadow-ci.yml)
    ;;
  dashboard)
    dockerfile=Dockerfile.dashboard
    inputs+=(
      docker/healthcheck_dashboard.py
      dashboard
      scripts/dashboard_docker_accept.sh
      .github/workflows/dashboard-ci.yml
    )
    ;;
  *)
    echo "usage: $0 {collector|shadow|dashboard}" >&2
    exit 2
    ;;
esac

inputs+=("$dockerfile" "$dockerfile.dockerignore")

# Actions checks out the synthetic PR merge commit. Its first parent
# (HEAD^1) is the exact base that this PR was merged into, which is
# exactly what we want to diff against - no matter how many commits the
# PR contains.
if git diff --quiet HEAD^1 HEAD -- "${inputs[@]}"; then
  echo build_image=false
else
  # `git diff --quiet` exits 1 on any difference; anything else (e.g. 2 on
  # pathspec error) must abort the job so we do not silently skip a build
  # that should have run.
  status=$?
  if [[ "$status" == 1 ]]; then
    echo build_image=true
  else
    exit "$status"
  fi
fi
