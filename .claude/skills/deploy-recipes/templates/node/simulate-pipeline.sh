#!/usr/bin/env bash
# simulate-pipeline.sh :: run what the App Store pipeline does, against the ACTUAL artefact.
# Shipped as a scaffold template by appstore-gate-compliance. Run it before EVERY upload.
# A green repository loop is not a green upload: the platform checks out your zip, adds its
# own generated .gitlab-ci.yml, and runs your tests with coverage in its own environment.
# This reproduces that checkout state and command, not just the command.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SIM="${TMPDIR:-/tmp}/appstore-sim"

# 1. Produce the real upload artefact (adjust if your package script lives elsewhere).
bash "$HERE/package-appstore.sh"

# 2. Check it out the way the platform does.
rm -rf "$SIM"
mkdir -p "$SIM"
unzip -q dist/appstore.zip -d "$SIM"
printf 'stages: [test]\n' > "$SIM/.gitlab-ci.yml"   # the platform commits its own CI file

# 3. Run the platform's install, command, and environment.
cd "$SIM"
npm ci
GITLAB_CI=true npm test -- --coverage

# 4. Confirm the artefact the SonarQube gate reads actually exists.
test -s coverage/lcov.info || {
  echo "coverage/lcov.info is missing or empty; the Code Quality gate would score 0%"; exit 1
}

echo "pipeline simulation green: the upload should clear the test and coverage stages"
