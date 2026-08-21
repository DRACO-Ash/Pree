#!/usr/bin/env bash
# format-gate.sh :: PostToolUse gate after Write|Edit|MultiEdit.
# Runs a fast syntax check so a broken edit is caught immediately. Deterministic.
# A non-zero exit surfaces the failure to the model via stderr so it fixes it before continuing.
# Adapts to both archetypes: prefers the project validate script; otherwise node --check on changed JS.
set -uo pipefail
DIR="${CLAUDE_PROJECT_DIR:-${CLAUDE_PLUGIN_ROOT:-.}}"

# Static archetype: the inline-JS syntax validator.
if [ -f "$DIR/scripts/validate.mjs" ]; then
  out="$(cd "$DIR" && node scripts/validate.mjs 2>&1)"
  if printf '%s' "$out" | grep -q "JS syntax: OK"; then exit 0; fi
  echo "format-gate: inline JavaScript failed validation after this edit." >&2
  echo "$out" >&2
  exit 2
fi

# Server archetype: node --check on JavaScript sources if a package.json is present.
if [ -f "$DIR/package.json" ]; then
  bad=0
  while IFS= read -r f; do
    [ -f "$f" ] || continue
    if ! node --check "$f" 2>/tmp/fg.$$; then
      echo "format-gate: $f failed node --check:" >&2
      cat /tmp/fg.$$ >&2
      bad=1
    fi
  done < <(cd "$DIR" && git diff --name-only --diff-filter=ACMR 2>/dev/null | grep -E '\.(m?js|cjs)$' || true)
  rm -f /tmp/fg.$$ 2>/dev/null || true
  [ "$bad" -eq 0 ] && exit 0
  exit 2
fi

exit 0
