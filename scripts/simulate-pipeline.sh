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
# Use the PINNED interpreter, not whatever python3 happens to be. Validating against an
# interpreter the image will never run is the opposite of a high-fidelity simulation.
# uv is preferred where present because it resolves the pin itself; a bare python3.12 on PATH
# may be a distribution build with ensurepip split out, which cannot create a usable venv.
PYV=$(cat "$ROOT/.python-version")
if command -v uv >/dev/null 2>&1; then
  uv venv --python "$PYV" "$WORK/venv" >/dev/null
  uv pip install --python "$WORK/venv/bin/python" --quiet \
    --require-hashes -r "$WORK/checkout/requirements-dev.txt"
elif command -v "python$PYV" >/dev/null 2>&1; then
  "python$PYV" -m venv "$WORK/venv"
  "$WORK/venv/bin/pip" install --quiet --require-hashes \
    -r "$WORK/checkout/requirements-dev.txt"
else
  echo "simulate: python$PYV is required (pinned in .python-version)" >&2
  exit 1
fi
RESOLVED=$("$WORK/venv/bin/python" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
if [ "$RESOLVED" != "$PYV" ]; then
  echo "simulate: resolved python $RESOLVED does not match the pinned $PYV" >&2
  exit 1
fi
echo "install: python $RESOLVED from the hash-locked requirements"

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
