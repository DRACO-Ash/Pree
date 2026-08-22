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

  # The container hard rules are asserted against the BUILT IMAGE, and asserted from OUTSIDE
  # it. The first version of these checks ran `docker run --entrypoint /usr/bin/find` and
  # treated empty output as a pass, which was wrong twice over: a failed run produces empty
  # output, and the mutation these checks exist to catch is a no-op binary copied over
  # /usr/bin/find, so the attack made the check print "no setuid or setgid bits" and pass.
  #
  # `docker export` streams the flattened filesystem to the host, so nothing inside the image
  # is trusted to report on it. Every check names the tool that must produce output, so silence
  # is a failure rather than a pass.
  CONTAINER=$(docker create pree:simulated) || { echo "image: docker create failed" >&2; exit 1; }
  LISTING=$(mktemp)
  if ! docker export "$CONTAINER" | tar -tv > "$LISTING"; then
    echo "image: could not export the built filesystem for inspection" >&2
    docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
    exit 1
  fi
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true

  # Positive control FIRST: if the listing is short, the export failed quietly and every
  # assertion below would pass by saying nothing.
  ENTRIES=$(wc -l < "$LISTING")
  if [ "$ENTRIES" -lt 1000 ]; then
    echo "image: the exported listing has only $ENTRIES entries, so it did not export" >&2
    exit 1
  fi
  echo "image: exported $ENTRIES filesystem entries for inspection"

  echo "--- image: no setuid or setgid bits ---"
  # tar -tv renders the mode as e.g. -rwsr-xr-x. s or S in the user or group execute position
  # is the /6000 mask. Read from the host listing, not from a binary inside the image.
  if awk '{ m = substr($1, 1, 10) } substr(m,4,1) ~ /[sS]/ || substr(m,7,1) ~ /[sS]/ { print }' \
       "$LISTING" | grep . >&2; then
    echo "image: setuid or setgid bits present in the shipped filesystem (listed above)" >&2
    exit 1
  fi
  echo "image: no setuid or setgid bits"

  echo "--- image: the package manager does not ship ---"
  if grep -E '(^|/)(opt/venv/bin|usr/local/bin)/pip' "$LISTING" >&2; then
    echo "image: pip is present in the shipped filesystem (listed above)" >&2
    exit 1
  fi
  echo "image: no pip in the shipped filesystem"

  echo "--- image: runs as the non-root numeric user ---"
  # The one check that must run INSIDE the image, because an identity is a runtime property.
  # It fails closed: an unset or unexpected value is a failure, and the command is the venv
  # python the CMD itself uses, so a broken interpreter fails here rather than at deploy.
  if ! IDENTITY=$(docker run --rm --entrypoint /opt/venv/bin/python pree:simulated \
       -c 'import os;print(f"{os.getuid()}:{os.getgid()}")'); then
    echo "image: could not read the runtime identity" >&2
    exit 1
  fi
  if [ "$IDENTITY" != "10001:10001" ]; then
    echo "image: runtime identity is '$IDENTITY', not 10001:10001" >&2
    exit 1
  fi
  echo "image: runs as $IDENTITY"
  rm -f "$LISTING"
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
