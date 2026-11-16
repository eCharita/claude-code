#!/usr/bin/env python3
"""Runs the LangSmith tracing plugin's hooks with this session's GitHub ticket as trace metadata.

Why a wrapper
-------------
The plugin (langsmith-tracing@langsmith-claude-code-plugins) reads trace metadata on every hook from
the env var `CC_LANGSMITH_METADATA` (highest precedence) merged with `<cwd>/.claude/langsmith.json`,
`<cwd>/langsmith-plugins.json`, `~/.claude/langsmith.json` and `~/.langsmith-plugins.json`. Nothing in
it is per session: two sessions in this directory that write their ticket into the shared file
overwrite each other (the previous implementation of this hook did that).

So the plugin's own hooks are disabled for this project (`enabledPlugins` in .claude/settings.json)
and this hook runs them instead: for every event it
1. on UserPromptSubmit, detects a GitHub issue URL in a real user prompt, reads the ticket via `gh`
   and stores it in `<claude config>/ticket-langsmith/<session_id>.json` (one file per session,
   written atomically, only by that session's own hooks),
2. runs the plugin's hook command for the event (from the plugin's hooks/hooks.json) with
   `CC_LANGSMITH_METADATA` = existing env metadata + this session's ticket. The env var exists only
   in that child process, so concurrent sessions cannot see each other's ticket,
3. on SessionEnd, deletes the session file; files of crashed sessions expire after STALE_AFTER.

The wrapper runs the plugin only when it is installed and enabled in the user settings. It never
blocks Claude Code: its own errors are logged to stderr and the exit code is 0 (only the plugin's
own exit code 2 is passed through). Docs: .claude/docs/langsmith.md.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
from repo_scope import resolve_scope  # noqa: E402

PLUGIN_ID = "langsmith-tracing@langsmith-claude-code-plugins"
METADATA_ENV = "CC_LANGSMITH_METADATA"
STALE_AFTER_SECONDS = 7 * 24 * 3600
GH_TIMEOUT_SECONDS = 8
MAX_FETCH_ATTEMPTS = 3  # prompts on which a failed `gh` lookup is retried
ISSUE_PATTERN = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/issues/(\d+)")
# Prompts injected by the harness (subagent hand-backs, task notifications…) must not switch the ticket.
SYNTHETIC_PROMPT_PREFIXES = ("<agent-message", "<task-notification", "<system-reminder")
TICKET_KEYS = ("ticket_key", "ticket_number", "ticket_repository", "ticket_url", "ticket_title",
               "ticket_labels", "ticket_service_repositories")
MAX_TEXT = 300
MAX_ITEMS = 50


def log(message):
    print(f"[ticket-langsmith] {message}", file=sys.stderr)


# --------------------------------------------------------------------------- paths / io


def config_dir():
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def state_dir():
    return config_dir() / "ticket-langsmith"


def state_path(session_id):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", session_id or ""):
        raise ValueError(f"invalid session_id {session_id!r}")
    return state_dir() / f"{session_id}.json"


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as exc:  # truncated file of an interrupted write, unreadable file
        log(f"Ignoring unreadable {path}: {exc}")
        return default


def write_json_atomic(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# --------------------------------------------------------------------------- ticket


def clean_text(value):
    return re.sub(r"[\x00-\x1f\x7f]", " ", str(value))[:MAX_TEXT]


def sanitize(ticket):
    """Only the documented ticket keys, bounded plain values — nothing else reaches the trace."""
    if not isinstance(ticket, dict):
        return None
    clean = {}
    for key in TICKET_KEYS:
        value = ticket.get(key)
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, int):
            clean[key] = value
        elif isinstance(value, str):
            clean[key] = clean_text(value)
        elif isinstance(value, list):
            clean[key] = [clean_text(item) for item in value[:MAX_ITEMS] if isinstance(item, str)]
    if not ISSUE_PATTERN.fullmatch(clean.get("ticket_url", "")):
        return None
    return clean


def fetch_issue(repository, number):
    try:
        result = subprocess.run(
            ["gh", "issue", "view", str(number), "--repo", repository, "--json", "title,labels"],
            capture_output=True, text=True, timeout=GH_TIMEOUT_SECONDS, check=True)
        return json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        log(f"Could not read {repository}#{number} via gh: {exc}")
        return {}


def build_ticket(repository, number):
    ticket = {
        "ticket_key": f"{repository}#{number}",
        "ticket_number": number,
        "ticket_repository": repository,
        "ticket_url": f"https://github.com/{repository}/issues/{number}",
    }
    issue = fetch_issue(repository, number)
    if issue:
        labels = [label.get("name", "") for label in issue.get("labels") or []]
        scope = resolve_scope(repository, labels)["scope"]
        ticket["ticket_title"] = issue.get("title", "")
        ticket["ticket_labels"] = labels
        ticket["ticket_service_repositories"] = [entry["repo"] for entry in scope]
    return sanitize(ticket)


def session_ticket(data):
    """The session's ticket; on UserPromptSubmit a new issue URL in the prompt replaces it."""
    path = state_path(data.get("session_id"))
    current = sanitize(read_json(path, None))
    if data.get("hook_event_name") != "UserPromptSubmit":
        return current
    prompt = (data.get("prompt") or "").lstrip()
    match = None if prompt.startswith(SYNTHETIC_PROMPT_PREFIXES) else ISSUE_PATTERN.search(prompt)
    attempts = (read_json(path, {}) or {}).get("fetch_attempts", 0) if current else 0
    target = None
    if match and (not current or current["ticket_key"] != f"{match.group(1)}#{match.group(2)}"):
        target, attempts = (match.group(1), int(match.group(2))), 0
    elif current and "ticket_title" not in current and attempts < MAX_FETCH_ATTEMPTS:
        target = (current["ticket_repository"], current["ticket_number"])  # gh failed before: retry
    if target:
        current = build_ticket(*target)
        state = {**current, "fetch_attempts": attempts + 1}  # sanitize() drops it on read
        write_json_atomic(path, state)
        log(f"Ticket for this session: {current['ticket_key']}")
    if current and path.exists():
        os.utime(path)  # keeps an active session's file from expiring
    return current


def metadata_env(env, ticket):
    """CC_LANGSMITH_METADATA for the plugin: existing env metadata + the ticket (ticket wins)."""
    if not ticket:
        return env
    metadata = {}
    if env.get(METADATA_ENV):
        try:
            parsed = json.loads(env[METADATA_ENV])
            metadata = parsed if isinstance(parsed, dict) else {}
        except ValueError:
            log(f"Ignoring invalid {METADATA_ENV}")
    return {**env, METADATA_ENV: json.dumps({**metadata, **ticket}, ensure_ascii=False)}


def prune_stale_sessions():
    cutoff = time.time() - STALE_AFTER_SECONDS
    for path in state_dir().glob("*.json"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass


# --------------------------------------------------------------------------- plugin


def plugin_root(project_dir):
    """Install path of the plugin when it is installed and enabled in the user settings."""
    settings = read_json(config_dir() / "settings.json", {})
    if (settings.get("enabledPlugins") or {}).get(PLUGIN_ID) is not True:
        return None
    installs = (read_json(config_dir() / "plugins" / "installed_plugins.json", {}).get("plugins") or {})
    candidates = sorted(installs.get(PLUGIN_ID) or [],
                        key=lambda i: i.get("projectPath") != str(project_dir))  # project install first
    for install in candidates:
        if install.get("scope") != "user" and install.get("projectPath") != str(project_dir):
            continue
        root = Path(install.get("installPath", ""))
        if (root / "hooks" / "hooks.json").is_file():
            return root
    return None


def run_plugin(event, raw, env, project_dir):
    root = plugin_root(project_dir)
    if root is None:
        return 0
    groups = (read_json(root / "hooks" / "hooks.json", {}).get("hooks") or {}).get(event) or []
    code = 0
    for hook in (h for group in groups for h in group.get("hooks", []) if h.get("type") == "command"):
        try:
            result = subprocess.run(["/bin/sh", "-c", hook["command"]], input=raw, capture_output=True,
                                    env={**env, "CLAUDE_PLUGIN_ROOT": str(root)},
                                    timeout=hook.get("timeout", 60))
        except (OSError, subprocess.SubprocessError) as exc:
            log(f"Plugin hook {event} failed: {exc}")
            continue
        sys.stdout.buffer.write(result.stdout)
        sys.stderr.buffer.write(result.stderr)
        if result.returncode == 2:
            code = 2
        elif result.returncode != 0:
            log(f"Plugin hook {event} exited {result.returncode}")
    return code


# --------------------------------------------------------------------------- main


def main():
    raw = sys.stdin.buffer.read()
    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        data = {}
    event = data.get("hook_event_name") or (sys.argv[1] if len(sys.argv) > 1 else "")
    project_dir = Path(os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or os.getcwd())
    env = dict(os.environ)
    try:
        if data.get("session_id"):
            env = metadata_env(env, session_ticket(data))
    except Exception as exc:  # noqa: BLE001 — tracing must never break Claude Code
        log(f"Ticket metadata unavailable: {exc}")

    code = run_plugin(event, raw, env, project_dir)

    if event in ("SessionEnd", "UserPromptSubmit"):
        try:
            if event == "SessionEnd" and data.get("session_id"):
                state_path(data["session_id"]).unlink(missing_ok=True)
            prune_stale_sessions()
        except Exception as exc:  # noqa: BLE001
            log(f"Cleanup failed: {exc}")
    return code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        log(f"Failed: {exc}")
        sys.exit(0)
