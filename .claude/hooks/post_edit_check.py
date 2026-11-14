#!/usr/bin/env python3
"""PostToolUse hook (matcher: Edit|Write|MultiEdit).

Runs only the fast, single-file checks from .claude/gate.json "fast",
keyed by file extension. A failure returns exit 2 so Claude sees the
finding immediately and fixes it while the code is still in hand.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _common import (
    block,
    bypass_active,
    load_contract,
    project_root,
    read_event,
    run_commands,
)

SKIP_PARTS = {"node_modules", "dist", "build", ".venv", "venv", "__pycache__", ".git"}


def main():
    event = read_event()
    tool_input = event.get("tool_input") or {}
    file_path = tool_input.get("file_path") or tool_input.get("path")
    root = project_root(event)
    if not file_path or bypass_active():
        sys.exit(0)
    path = Path(file_path)
    if SKIP_PARTS.intersection(path.parts):
        sys.exit(0)
    contract = load_contract(root)
    if not contract:
        sys.exit(0)
    commands = contract.get("fast", {}).get(path.suffix.lower(), [])
    commands = [c.replace("{file}", f'"{file_path}"') for c in commands]
    failure = run_commands(root, commands, f"Fast check ({path.name})", 120)
    if failure:
        block(failure)
    sys.exit(0)


if __name__ == "__main__":
    main()
