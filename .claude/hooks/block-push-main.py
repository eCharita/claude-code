#!/usr/bin/env python3
"""PreToolUse hook (Bash): blocks `git push` to main/master (STRICT RULE 2 in CLAUDE.md).

Blocks when the push targets main/master explicitly (e.g. `git push origin main`,
`git push origin HEAD:master`), when it pushes the current branch and that branch
is main/master (e.g. plain `git push` on main), or with --all / --mirror.
Exit code 2 = block, the stderr message is shown to Claude.
"""
import json
import os
import re
import shlex
import subprocess
import sys

PROTECTED = {"main", "master"}


def current_branch(directory):
    try:
        out = subprocess.run(
            ["git", "-C", directory, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip() if out.returncode == 0 else None
    except Exception:
        return None


def is_protected(ref, branch):
    ref = ref.lstrip("+")
    if ref.startswith("refs/heads/"):
        ref = ref[len("refs/heads/"):]
    if ref == "HEAD":
        return branch in PROTECTED
    return ref in PROTECTED


def check_push(args, directory):
    """args = tokens after `push`. Returns a reason string if the push must be blocked."""
    if any(a in ("--all", "--mirror") for a in args):
        return "git push --all/--mirror would also push main/master"
    positional = [a for a in args if not a.startswith("-")]
    branch = current_branch(directory)
    refspecs = positional[1:]  # first positional is the remote
    if not refspecs:
        if branch in PROTECTED:
            return f"current branch in {directory} is '{branch}'"
        return None
    for spec in refspecs:
        dest = spec.split(":", 1)[1] if ":" in spec else spec
        if is_protected(dest, branch):
            return f"push target '{spec}' is main/master"
    return None


def main():
    data = json.load(sys.stdin)
    command = (data.get("tool_input") or {}).get("command", "")
    if not re.search(r"\bgit\b.*\bpush\b", command, re.S):
        return 0

    directory = data.get("cwd") or os.getcwd()
    for segment in re.split(r"&&|\|\||;|\||\n", command):
        try:
            tokens = shlex.split(segment)
        except ValueError:
            tokens = segment.split()
        if not tokens:
            continue
        if tokens[0] == "cd" and len(tokens) > 1:
            directory = os.path.normpath(os.path.join(directory, os.path.expanduser(tokens[1])))
            continue
        if "git" not in tokens:
            continue
        i = tokens.index("git") + 1
        git_dir = directory
        # skip git global options (-C <dir>, -c <k=v>, --no-pager, ...)
        while i < len(tokens) and tokens[i].startswith("-"):
            if tokens[i] in ("-C", "-c") and i + 1 < len(tokens):
                if tokens[i] == "-C":
                    git_dir = os.path.normpath(os.path.join(directory, os.path.expanduser(tokens[i + 1])))
                i += 2
            else:
                i += 1
        if i < len(tokens) and tokens[i] == "push":
            reason = check_push(tokens[i + 1:], git_dir)
            if reason:
                print(
                    f"BLOCKED by .claude/hooks/block-push-main.py: {reason}. "
                    "Pushing to main/master is forbidden (STRICT RULE 2). "
                    "Push to the ticket branch instead.",
                    file=sys.stderr,
                )
                return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
