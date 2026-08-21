#!/bin/sh
# Builds the App Store upload zip. Flat by construction: the Dockerfile, the lockfiles and
# the source sit at the archive root, never nested, because the platform detects the template
# from a root-level Dockerfile and builds with the root as its context.
#
# This is an allowlist, not an exclusion list. The platform runs the test suite against the
# zip root before it builds any image, so the package must be a self-sufficient, testable
# source tree: tests, runner config and docs included, built output and secrets excluded.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"

OUT="${1:-appstore-package/pree-upload.zip}"
mkdir -p "$(dirname "$OUT")"
rm -f "$OUT"

INCLUDE="Dockerfile .dockerignore .gitignore requirements.txt requirements.in \
requirements-dev.txt requirements-dev.in pyproject.toml sonar-project.properties \
.python-version .env.example README.md src tests docs scripts"

for entry in $INCLUDE; do
  if [ ! -e "$entry" ]; then
    echo "package: missing required entry $entry" >&2
    exit 1
  fi
done

# Banned from the archive: virtual environments, caches, built output, version-control
# metadata, local data and any environment file.
zip -qr "$OUT" $INCLUDE \
  -x '*/__pycache__/*' '*.pyc' '*/.venv/*' '*/.mypy_cache/*' '*/.ruff_cache/*' \
     '*/.pytest_cache/*' '*/data/*' '*.env' '*.zip'

echo "package: wrote $OUT"
echo "--- archive root ---"
unzip -l "$OUT" | awk 'NR>3 && $4 !~ /\// {print $4}' | head -20

for banned in .env .git .venv node_modules coverage; do
  if unzip -Z1 "$OUT" | grep -qE "^(.*/)?${banned}(/|$)"; then
    echo "package: banned entry $banned present in the archive" >&2
    exit 1
  fi
done

echo "package: allowlist clean"
