#!/bin/sh
# Regenerate all three lock files, hash-locked, for one interpreter version.
#
# Never edit a .txt lock file by hand. The hashes are what make
# `pip install --require-hashes --no-deps` meaningful, and a hand-edited pin
# with a stale hash fails the container build rather than installing something
# unexpected. That is the correct behaviour and an expensive way to find a typo.
#
# --python-version is not optional. A lock file compiled for a different
# interpreter resolves a different set, and on the legacy analyser lineage a
# platform mismatch is a documented failure mode.
#
# Usage:  sh scripts/lock.sh [PYTHON_VERSION]
#         sh scripts/lock.sh 3.12

set -eu

PYVER="${1:-3.12}"

if ! command -v uv >/dev/null 2>&1; then
    echo "error: uv not found." >&2
    echo "       Install it, or substitute: pip-compile --generate-hashes" >&2
    exit 2
fi

lock() {
    src="$1"
    dst="$2"
    if [ ! -f "$src" ]; then
        echo "skip: $src not present"
        return 0
    fi
    echo "lock: $src -> $dst"
    # Redirected, never piped: a pipeline reports the last command's status.
    uv pip compile --python-version "$PYVER" --generate-hashes "$src" -o "$dst"
}

lock requirements-runtime.in requirements-runtime.txt
lock requirements.in         requirements.txt
lock requirements-dev.in     requirements-dev.txt

echo
echo "Locked for Python ${PYVER}."
echo "Next: install the new pins, then run the loop. Leg one catches a lock"
echo "file that was regenerated but not installed, which is the usual next"
echo "mistake. Commit all three .txt files together."
echo
echo "Optional, and worth it once a week rather than on every change:"
echo "  uv pip compile --exclude-newer \$(date -d '7 days ago' +%Y-%m-%d) ..."
echo "  Delayed ingestion lets the wider community be the canary. It is one"
echo "  layer, not a guarantee, and it delays security patches, so keep an"
echo "  expedite path for a real fix."
