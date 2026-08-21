#!/bin/sh
# The local verification loop. Mirrors the platform pipeline's check and test stages exactly,
# so a violation surfaces here rather than on upload.
#
# Written for POSIX sh, not bash. The platform runner's shell is minimal and a bash-only
# feature here would die with "sh: bash: not found" at the platform build.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"

if [ -x .venv/bin/python ]; then
  PATH="$ROOT/.venv/bin:$PATH"
  export PATH
fi

echo "--- format ---"
ruff format --check src tests

echo "--- lint ---"
ruff check src tests

echo "--- types ---"
mypy

echo "--- tests with coverage ---"
# Cobertura XML at coverage.xml is the artefact the App Store quality gate reads. A bare
# pytest run writes nothing it can read, which scores as 0% coverage however good the suite is.
coverage erase
coverage run -m pytest
coverage report
coverage xml

echo "--- dependency vulnerabilities ---"
if pip-audit -r requirements.txt --strict; then
  echo "pip-audit: clean"
else
  status=$?
  # Fail closed on the authoritative networked runner. On an offline runner the scan cannot
  # be authoritative, so it must skip honestly rather than fail-open into a green pass.
  if [ "${GITLAB_CI:-}" = "true" ]; then
    echo "pip-audit: FAILED on the authoritative runner" >&2
    exit "$status"
  fi
  echo "pip-audit: could not complete locally, deferred to CI" >&2
  exit "$status"
fi

echo "--- verification loop green ---"
