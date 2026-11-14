#!/usr/bin/env python3
"""PreToolUse hook (matcher: Bash).

Enforces the project gate contract in .claude/gate.json at the three
irreversible moments: git commit, git push and git tag. Blocks with exit 2
and returns a short failure tail to Claude. Projects without a contract
are not blocked; the SessionStart hook warns instead.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _common import (
    DEFAULT_TIMEOUT,
    block,
    bypass_active,
    git,
    git_calls,
    is_cloud,
    load_contract,
    project_root,
    read_event,
    run_commands,
)

LIST_FLAGS = {"-l", "--list", "-d", "--delete", "-v", "--verify", "--contains", "-n"}
VALUE_FLAGS = {"-m", "-F", "--message", "--file", "-u", "--local-user"}


def tag_name(args):
    if any(a in LIST_FLAGS for a in args):
        return None
    skip = False
    for a in args:
        if skip:
            skip = False
            continue
        if a in VALUE_FLAGS:
            skip = True
            continue
        if a.startswith("-"):
            continue
        return a
    return None


def pushes_tags(root, args):
    if any(a in ("--tags", "--follow-tags") or "refs/tags/" in a for a in args):
        return True
    for a in args:
        if not a.startswith("-"):
            code, out = git(root, "tag", "-l", a)
            if code == 0 and out == a:
                return True
    return False


def main():
    event = read_event()
    command = (event.get("tool_input") or {}).get("command", "") or ""
    root = project_root(event)

    if "CLAUDE_GATE_BYPASS" in command:
        block("CLAUDE_GATE_BYPASS belongs to the user. Do not set, unset or reference it.")

    calls = git_calls(command)
    if not calls or bypass_active():
        sys.exit(0)

    contract = load_contract(root)
    if contract is None:
        sys.exit(0)
    timeout = int(contract.get("timeout_seconds", DEFAULT_TIMEOUT))

    for sub, args in calls:
        if sub == "commit":
            failure = run_commands(root, contract.get("commit", []), "Commit", timeout)
            if failure:
                block(failure + "\nFix the cause, then commit. Do not weaken the gate.")

        elif sub == "push":
            handoff = contract.get("tag_push", "handoff" if is_cloud() else "allow") == "handoff"
            if pushes_tags(root, args) and handoff:
                block("Tag pushes from this environment are handed off, not retried (the Claude Code proxy returns 403). "
                      "Report the exact tag, commit SHA and push command for the user or CI.")
            failure = run_commands(root, contract.get("push", []), "Push", timeout)
            if failure:
                block(failure + "\nNothing is pushed until the full local gate is green.")

        elif sub == "tag":
            name = tag_name(args)
            if not name:
                continue
            code, out = git(root, "tag", "-l", name)
            if code == 0 and out == name:
                block(f"Tag {name} already exists locally. Propose the next unused version.")
            code, out = git(root, "ls-remote", "--tags", "origin", f"refs/tags/{name}")
            if code == 0 and out:
                block(f"Tag {name} already exists on origin. Propose the next unused version.")
            failure = run_commands(root, contract.get("tag", []), "Release", timeout)
            if failure:
                block(failure + "\nA release cannot be tagged until every scanner passes.")
    sys.exit(0)


if __name__ == "__main__":
    main()
