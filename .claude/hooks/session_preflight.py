#!/usr/bin/env python3
"""SessionStart hook. Prints a compact brief that Claude Code adds to the
session context: kit version, environment, repo state, disk headroom, gate
contract status, tools the gates need but cannot find, and the next action
from any handover note. Kept to a few lines because every line costs context."""
import importlib.util
import json
import shlex
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _common import (
    bypass_active,
    git,
    is_cloud,
    project_root,
    read_event,
)

MIN_FREE_GB = 2.0
RESOLVED_ELSEWHERE = {"npx", "npm", "cd", "echo", "true", "false", "test", "["}


def missing_tools(contract):
    missing = set()
    stages = [contract.get(s, []) for s in ("commit", "push", "tag")]
    stages += list(contract.get("fast", {}).values())
    for commands in stages:
        for cmd in commands:
            try:
                toks = shlex.split(cmd.replace("{file}", "x"))
            except ValueError:
                continue
            if not toks or toks[0] in RESOLVED_ELSEWHERE:
                continue
            if toks[0] in ("python", "python3") and len(toks) > 2 and toks[1] == "-m":
                if importlib.util.find_spec(toks[2]) is None:
                    missing.add(f"python module {toks[2]}")
            elif shutil.which(toks[0]) is None:
                missing.add(toks[0])
    return sorted(missing)


def handover_next_action(root):
    for candidate in ("docs/HANDOVER.md", "HANDOVER.md"):
        path = root / candidate
        if path.is_file():
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
            for i, line in enumerate(lines):
                if "next action" in line.lower():
                    following = [ln.strip() for ln in lines[i + 1:i + 4] if ln.strip()]
                    return candidate, (following[0] if following else line.strip())
            return candidate, "no 'Next action' line found"
    return None, None


def main():
    root = project_root(read_event())
    out = []
    version_file = root / ".claude" / "KIT_VERSION"
    kit = version_file.read_text().strip() if version_file.is_file() else "unknown"
    out.append(f"[preflight] Bluestaq kit {kit}; environment {'cloud' if is_cloud() else 'local'}")
    code, branch = git(root, "rev-parse", "--abbrev-ref", "HEAD")
    if code == 0:
        _, last_tag = git(root, "describe", "--tags", "--abbrev=0")
        _, dirty = git(root, "status", "--porcelain")
        changed = len([ln for ln in dirty.splitlines() if ln.strip()])
        out.append(f"[preflight] branch {branch}; last tag {last_tag or 'none'}; "
                   f"{changed} uncommitted file(s)")
    free_gb = shutil.disk_usage(root).free / 1e9
    warn = " LOW: clear build artefacts before heavy work" if free_gb < MIN_FREE_GB else ""
    out.append(f"[preflight] disk free {free_gb:.1f} GB{warn}")
    contract_path = root / ".claude" / "gate.json"
    if contract_path.is_file():
        try:
            missing = missing_tools(json.loads(contract_path.read_text(encoding="utf-8")))
            out.append("[preflight] gate contract present: commit, push and tag are gated")
            if missing:
                out.append("[preflight] gate tools NOT FOUND: " + ", ".join(missing) +
                           ". Gates needing them will block. Tell the user to add them to the "
                           "cloud environment setup script.")
        except (ValueError, OSError) as exc:
            out.append(f"[preflight] gate contract unreadable: {exc}")
    else:
        out.append("[preflight] NO .claude/gate.json: commit, push and tag are NOT gated. "
                   "Offer to draft one before release work.")
    if is_cloud():
        out.append("[preflight] cloud session: tag pushes are handed to the user; deliver "
                   "artefacts by sending the file, never by committing them")
    if bypass_active():
        out.append("[preflight] WARNING: CLAUDE_GATE_BYPASS is set; gates are off")
    name, action = handover_next_action(root)
    if name:
        out.append(f"[preflight] handover {name}: next action: {action}")
    print("\n".join(out))


if __name__ == "__main__":
    main()
