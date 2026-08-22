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
  # Inside $WORK, so the EXIT trap removes it on every failure path. Outside it, a full
  # filesystem listing of the image leaked into the system temp directory on each failure.
  LISTING="$WORK/image-listing.txt"
  if ! docker export "$CONTAINER" | tar -tv > "$LISTING"; then
    echo "image: could not export the built filesystem for inspection" >&2
    docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
    exit 1
  fi
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true

  # Positive controls FIRST, and there are three, because the previous version proved only that
  # the listing was long. A check whose pattern cannot match anything prints its success message
  # unconditionally, which is exactly how the pip assertion below was dead: `docker export`
  # writes RELATIVE member names, so a pattern anchored on `(^|/)opt/` could never fire.
  ENTRIES=$(wc -l < "$LISTING")
  if [ "$ENTRIES" -lt 1000 ]; then
    echo "image: the exported listing has only $ENTRIES entries, so it did not export" >&2
    exit 1
  fi
  # The mode column must parse, or the setuid scan reads silence from an unparseable listing.
  MODED=$(awk 'length($1) == 10 && $1 ~ /^[-dlbcps]/ { n++ } END { print n + 0 }' "$LISTING")
  if [ "$MODED" -lt 1000 ]; then
    echo "image: only $MODED of $ENTRIES lines carry a parseable mode column" >&2
    exit 1
  fi
  # And the pip pattern must be able to match SOMETHING: the venv the build creates is in the
  # listing, so if this cannot be found the pattern is wrong and its silence means nothing.
  if ! awk '{ print $NF }' "$LISTING" | grep -q '^opt/venv/'; then
    echo "image: the pip pattern's own path prefix is absent, so the scan below is dead" >&2
    exit 1
  fi
  echo "image: exported $ENTRIES entries, $MODED with a parseable mode, venv path present"

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
  # The LAST field, unanchored. tar -tv puts the mode first and docker export writes member
  # names relative, so `opt/venv/bin/pip3` is preceded by a space and never by `/` or
  # start-of-line: the anchored pattern this replaces could not match a single line, and the
  # check printed its success message on every run. The library directory is covered too, not
  # only the entry points, because site-packages/pip is what the policy scan reads.
  if awk '{ print $NF }' "$LISTING" \
       | grep -E '(^|/)pip[0-9.]*$|/site-packages/(pip|setuptools|pkg_resources)/' >&2; then
    echo "image: pip or setuptools is present in the shipped filesystem (listed above)" >&2
    exit 1
  fi
  echo "image: no pip or setuptools in the shipped filesystem"

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
