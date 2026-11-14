#!/usr/bin/env python3
"""Shared helpers for Claude Code hooks. Python standard library only."""
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

DEFAULT_TIMEOUT = 1500
TAIL_LINES = 30  # keep feedback short: every line returned costs context tokens


def read_event():
    try:
        return json.load(sys.stdin)
    except ValueError:
        return {}


def project_root(event):
    return Path(os.environ.get("CLAUDE_PROJECT_DIR") or event.get("cwd") or os.getcwd())


def load_contract(root):
    path = root / ".claude" / "gate.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        block(f"Gate contract .claude/gate.json is not valid JSON: {exc}")


def bypass_active(root=None):
    """Emergency override. Set CLAUDE_GATE_BYPASS=1 as an environment variable in the
    cloud environment settings (user action). A variable typed into a Bash command does
    not reach the hook process, so Claude cannot set it for itself."""
    return os.environ.get("CLAUDE_GATE_BYPASS") == "1"


def is_cloud():
    return os.environ.get("CLAUDE_CODE_REMOTE") == "true"


def block(message):
    print(message, file=sys.stderr)
    sys.exit(2)


def tail(text, n=TAIL_LINES):
    return "\n".join(text.strip().splitlines()[-n:])


def run_commands(root, commands, label, timeout):
    """Run commands in order. Decide on exit code only, never on output text."""
    for cmd in commands:
        try:
            # Commands come only from the repo-owned, reviewed .claude/gate.json.
            r = subprocess.run(cmd, shell=True, check=False, cwd=root, capture_output=True,  # nosec B602
                               text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return f"{label} gate timed out after {timeout}s on: {cmd}"
        if r.returncode == 127:
            return (f"{label} gate cannot run: a tool in '{cmd}' is not installed in this "
                    f"environment. Add it to the cloud environment setup script. Do not "
                    f"remove the command from .claude/gate.json.")
        if r.returncode != 0:
            return (f"{label} gate FAILED on: {cmd} (exit {r.returncode})\n"
                    f"{tail((r.stdout or '') + (r.stderr or ''))}")
    return None


def git(root, *args, timeout=20):
    try:
        r = subprocess.run(["git", *args], check=False, cwd=root, capture_output=True,
                           text=True, timeout=timeout)
        return r.returncode, r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return 1, ""


def git_calls(command):
    """Return (subcommand, args) for every git invocation in a shell command."""
    calls = []
    for seg in re.split(r"&&|\|\||;|\||\n", command):
        try:
            toks = shlex.split(seg)
        except ValueError:
            toks = seg.split()
        for i, tok in enumerate(toks):
            if os.path.basename(tok) in ("git", "git.exe"):
                j = i + 1
                while j < len(toks) and toks[j].startswith("-"):
                    j += 2 if toks[j] in ("-C", "-c") else 1
                if j < len(toks):
                    calls.append((toks[j], toks[j + 1:]))
                break
    return calls
