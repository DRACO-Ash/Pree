#!/bin/sh
# Simulates the platform pipeline against the ARTEFACT, not the repository, including the
# files and environment the platform adds to its own checkout.
#
# The platform copies the zip into a project it owns, commits its own generated
# .gitlab-ci.yml into that checkout, sets GITLAB_CI=true, and runs the stages strictly in
# order. Reproducing that here is the single highest-value control, because the platform
# otherwise reveals its requirements one gate at a time, at roughly one upload per stage.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

echo "=== stage: package ==="
sh scripts/package-appstore.sh "$WORK/upload.zip"

echo "=== stage: setup (platform checkout) ==="
unzip -q "$WORK/upload.zip" -d "$WORK/checkout"
# The platform commits its own generated pipeline file into the checkout. Any assertion about
# untracked files must be written for a checkout that contains it.
printf 'stages:\n  - generated-by-the-platform\n' > "$WORK/checkout/.gitlab-ci.yml"

echo "=== stage: install ==="
python3 -m venv "$WORK/venv"
"$WORK/venv/bin/pip" install --quiet --require-hashes -r "$WORK/checkout/requirements-dev.txt"

echo "=== stage: check and test (GITLAB_CI=true, at the archive root) ==="
cd "$WORK/checkout"
PATH="$WORK/venv/bin:$PATH"
export PATH
GITLAB_CI=true
export GITLAB_CI
ruff format --check src tests
ruff check src tests
mypy
coverage erase
coverage run -m pytest
coverage report
coverage xml
test -s coverage.xml || { echo "simulate: coverage.xml is empty or absent" >&2; exit 1; }
echo "coverage.xml present at the archive root"

echo "=== stage: containerize ==="
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  docker build -t pree:simulated .
  echo "container build: green"
else
  # An honest non-zero skip, never a green pass. Continuous integration is the binding source
  # of truth for this leg.
  echo "container build: NO DOCKER DAEMON AVAILABLE, deferred to CI" >&2
  exit 2
fi

echo "=== simulation green ==="
