#!/usr/bin/env bash
# package-appstore.sh :: build the App Store upload zip for a quality-gated container.
# Shipped as a scaffold template by deploy-recipes and packaging. Copy to scripts/ and wire
# it as an "npm run package:appstore" step.
#
# The upload must be a self-sufficient, TESTABLE source tree (Dockerfile and lockfiles at
# root, source, build tooling, tests, docs), NOT a built artefact: the platform runs your
# tests with coverage inside the checkout. `git archive` ships exactly the tracked files and
# nothing gitignored, which is why node_modules, coverage, and .env never leak in. Keep
# tests and docs TRACKED, or the platform test stage cannot find them.
set -euo pipefail

OUT="dist/appstore.zip"
mkdir -p dist
rm -f "$OUT"

git archive --format=zip -o "$OUT" HEAD

# Verify the two contracts that most often fail on upload, before a human ever uploads.
# Read member NAMES only (unzip -Z1), so the pattern anchors on the real path and is not
# defeated by the size and date columns of a plain `unzip -l` listing.
names="$(unzip -Z1 "$OUT")"
printf '%s\n' "$names" | grep -q  "package.json" || { echo "package is missing package.json"; exit 1; }
printf '%s\n' "$names" | grep -qi "Dockerfile"   || { echo "package is missing the Dockerfile"; exit 1; }
# Anchor each banned component at the start of a name or just after a slash, so a root-level
# .env (and .env.local, .env.production), node_modules, or coverage is caught, not only a
# nested one. git archive already omits gitignored files; this net catches one tracked by mistake.
if printf '%s\n' "$names" | grep -qE '(^|/)(node_modules|coverage)/|(^|/)\.env($|\.)'; then
  echo "package contains a banned path (node_modules, coverage, or a .env file). Fix .gitignore and untrack it."; exit 1
fi

echo "wrote $OUT"
