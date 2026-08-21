#!/usr/bin/env bash
# SessionStart hook (Bluestaq Foundations): verify the baseline is installed AND enacted on setup.
#
# The baseline ships as flat files that rehydrate under .claude/. A project that uploads the bundle
# but does not install ALL of it ends up carrying standards Claude cannot see, because a skill,
# agent, or hook is only discovered from its installed location. Field retrospectives found this is
# the single most expensive failure class: work planned and scaffolded against a baseline that was
# never actually present, then torn up. So this hook treats the baseline as a fail-closed CONTROL,
# not a suggestion. It checks three limbs, every session start:
#   1. every skill named in the manifest is present under .claude/skills/<name>/SKILL.md
#   2. every agent named in the manifest is present under .claude/agents/<file>
#   3. the hooks manifest (hooks/hooks.json) is present
# If any limb is incomplete it prints a fail-closed directive (to stdout, so it enters the session
# context) naming what is missing and instructing a stop. It stays silent when the install is
# complete or when this is not a rehydrated Foundations project. It exits 0 either way: a SessionStart
# hook must not brick the session, so the enforcement is the in-context directive, which Claude is to
# treat as a blocking precondition on planning, scaffolding, packaging, and deploy.
set -uo pipefail
root="${CLAUDE_PROJECT_DIR:-$PWD}"
manifest="$root/.claude/.claude-plugin/plugin.json"

# Only a rehydrated Foundations project carries this manifest; otherwise there is nothing to check.
[ -f "$manifest" ] || exit 0

missing_skills=""
while IFS= read -r name; do
  [ -z "$name" ] && continue
  [ -f "$root/.claude/skills/$name/SKILL.md" ] || missing_skills="$missing_skills $name"
done < <(grep -oE '"skills/[a-z0-9-]+"' "$manifest" | sed -E 's#"skills/([a-z0-9-]+)"#\1#' | sort -u)

missing_agents=""
while IFS= read -r af; do
  [ -z "$af" ] && continue
  [ -f "$root/.claude/agents/$af" ] || missing_agents="$missing_agents $af"
done < <(grep -oE '"agents/[a-z0-9-]+\.md"' "$manifest" | sed -E 's#"agents/([a-z0-9.-]+)"#\1#' | sort -u)

# The hooks manifest itself: named in plugin.json ("hooks": "hooks/hooks.json"); confirm it is present.
# Constrain the href to a safe hooks/ path (defence in depth, matching the skills/agents limbs) and
# reject any traversal, so a hostile manifest cannot point the presence test at an arbitrary path.
missing_hooks=""
href=$(grep -oE '"hooks"[[:space:]]*:[[:space:]]*"hooks/[a-z0-9._/-]+"' "$manifest" | sed -E 's#.*"(hooks/[a-z0-9._/-]+)".*#\1#')
case "$href" in *..*) href="";; esac
[ -n "$href" ] && { [ -f "$root/.claude/$href" ] || missing_hooks=" $href"; }

if [ -n "$missing_skills" ] || [ -n "$missing_agents" ] || [ -n "$missing_hooks" ]; then
  echo "Bluestaq Foundations: the skill baseline is NOT fully enacted. Treat this as a FAILED CONTROL and stop: do not plan, scaffold, package, or deploy until it is resolved (the project's own fail-closed rule: a control that cannot be verified is treated as failed)."
  [ -n "$missing_skills" ] && echo "  Missing skills (under .claude/skills/):$missing_skills"
  [ -n "$missing_agents" ] && echo "  Missing agents (under .claude/agents/):$missing_agents"
  [ -n "$missing_hooks" ]  && echo "  Missing hooks manifest (under .claude/):$missing_hooks"
  echo "  Fix: rehydrate the flat files (see START-HERE.md) so every skill, agent, hook, and the output style lands under .claude/, then commit the tree. Install ALL of it; do not cherry-pick. A standard Claude cannot see is a standard it cannot apply."
fi
exit 0
