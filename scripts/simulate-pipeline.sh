#!/bin/sh
# Simulates the platform pipeline against the ARTEFACT, not the repository, including the
# files and environment the platform adds to its own checkout.
#
# EXIT: 0 every stage green, image build included, and the only state that clears an
# upload. 2 every stage green except containerize, deferred to CI for want of a Docker
# daemon; exit 2 is NOT a pass. Anything else is a real failure in the last stage printed.
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
mypy src/pree tests
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

  # The container hard rules are asserted against the BUILT IMAGE here, not against the
  # Dockerfile's text. Four consecutive security reviews defeated the text guards, each in a
  # new place: the sweep moved to another stage, its predicate was narrowed, PATH was changed
  # so `find` resolved elsewhere, and a no-op binary was copied over /usr/bin/find. Every one
  # of those left the whole suite green, because text cannot verify what an image contains.
  # These three checks can. They run only when a daemon exists, which is why the no-daemon
  # branch below exits 2 rather than reporting a pass.
  echo "--- image: no setuid or setgid bits ---"
  BITS=$(docker run --rm --entrypoint /usr/bin/find pree:simulated \
    / -xdev -perm /6000 \( -type f -o -type d \) -print 2>/dev/null || true)
  if [ -n "$BITS" ]; then
    echo "image: setuid or setgid bits present in the shipped filesystem:" >&2
    echo "$BITS" >&2
    exit 1
  fi
  echo "image: no setuid or setgid bits"

  echo "--- image: runs as the non-root numeric user ---"
  IDENTITY=$(docker run --rm --entrypoint /opt/venv/bin/python pree:simulated \
    -c 'import os;print(f"{os.getuid()}:{os.getgid()}")')
  if [ "$IDENTITY" != "10001:10001" ]; then
    echo "image: runtime identity is $IDENTITY, not 10001:10001" >&2
    exit 1
  fi
  echo "image: runs as $IDENTITY"

  echo "--- image: the package manager does not ship ---"
  PIPS=$(docker run --rm --entrypoint /usr/bin/find pree:simulated \
    /opt/venv/bin /usr/local/bin -maxdepth 1 -name 'pip*' -print 2>/dev/null || true)
  if [ -n "$PIPS" ]; then
    echo "image: pip is present in the shipped filesystem:" >&2
    echo "$PIPS" >&2
    exit 1
  fi
  echo "image: no pip in the shipped filesystem"
else
  # An honest non-zero skip, never a green pass. Continuous integration is the binding source
  # of truth for this leg, and it is the ONLY place the three image assertions above can run,
  # which means the container hard rules are unverified until CI builds the image.
  echo "container build: NO DOCKER DAEMON AVAILABLE, deferred to CI" >&2
  echo "         The image assertions (no setuid bits, non-root numeric user, no pip) run" >&2
  echo "         only with a daemon, so those three rules are UNVERIFIED here." >&2
  exit 2
fi

echo "=== simulation green ==="
