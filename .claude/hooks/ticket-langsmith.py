"""Attach GitHub ticket + repository data to LangSmith traces of a Claude Code session.

How it works
------------
The LangSmith tracing plugin (langsmith-tracing@langsmith-claude-code-plugins)
re-reads `<cwd>/.claude/langsmith.json` on every hook and copies its `metadata`
object onto every run it uploads. Runs that are already finished can NOT be
changed afterwards (LangSmith answers PATCH with 409 "Duplicate run update"),
so the ticket metadata has to be in that file *before* the plugin closes the
turn. This hook therefore:

* UserPromptSubmit – detects a GitHub issue URL in the user's prompt, reads the
  ticket via `gh` (title, labels -> affected repositories) and remembers it for
  the session in ~/.claude/ticket-langsmith/<session_id>.json.
* UserPromptSubmit / Stop / SubagentStop – writes the session's ticket metadata
  into `.claude/langsmith.json` (or removes stale ticket keys when the session
  has no ticket), so the plugin uploads the turn with it.
* SessionEnd – removes the ticket keys again if this session put them there.

Limitation: the config file is shared by all sessions in this directory. Two
sessions running at the same time on different tickets can mix up metadata.

The hook never blocks Claude Code: every error is only logged to stderr.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

STATE_DIR = Path.home() / ".claude" / "ticket-langsmith"
OWNER_FILE = STATE_DIR / "_config-owner.json"

ISSUE_PATTERN = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/issues/(\d+)")

# Prompts injected by the harness (subagent hand-backs, task notifications…)
# are not user prompts and must not switch the ticket.
SYNTHETIC_PROMPT_PREFIXES = ("<agent-message", "<task-notification", "<system-reminder")

TICKET_KEYS = (
    "ticket_key",
    "ticket_number",
    "ticket_repository",
    "ticket_url",
    "ticket_title",
    "ticket_labels",
    "ticket_service_repositories",
)

GH_TIMEOUT_SECONDS = 8


def log(message):
    print(f"[ticket-langsmith] {message}", file=sys.stderr)


# --------------------------------------------------------------------------- paths


def project_dir(data):
    return Path(os.getenv("CLAUDE_PROJECT_DIR") or data.get("cwd") or os.getcwd())


def config_path(data):
    # The plugin looks only at `<hook cwd>/.claude/langsmith.json`.
    return Path(data.get("cwd") or project_dir(data)) / ".claude" / "langsmith.json"


def state_path(session_id):
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", session_id):
        raise ValueError(f"Invalid session_id: {session_id!r}")
    return STATE_DIR / f"{session_id}.json"


# --------------------------------------------------------------------------- io


def read_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def write_json_atomic(path, payload):
    # Atomic replace: the plugin discards the whole file if it reads invalid JSON.
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=4, ensure_ascii=False)
            handle.write("\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# --------------------------------------------------------------------------- ticket


def load_service_map(data):
    """`service:*` label -> repository, parsed from .claude/config/repos.yaml."""
    path = project_dir(data) / ".claude" / "config" / "repos.yaml"
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    mapping = {}
    label = None
    for line in text.splitlines():
        label_match = re.match(r'\s*-\s*label:\s*"?([^"]+)"?\s*$', line)
        if label_match:
            label = normalize_label(label_match.group(1))
            continue
        repo_match = re.match(r'\s*repo:\s*"?([^"\s]+)"?\s*$', line)
        if repo_match and label:
            mapping[label] = repo_match.group(1)
            label = None
    return mapping


def normalize_label(label):
    return re.sub(r"\s+", "", label).lower()


def fetch_issue(repository, number):
    try:
        result = subprocess.run(
            ["gh", "issue", "view", str(number), "--repo", repository, "--json", "title,labels"],
            capture_output=True,
            text=True,
            timeout=GH_TIMEOUT_SECONDS,
            check=True,
        )
        return json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        log(f"Could not read {repository}#{number} via gh: {exc}")
        return {}


def build_ticket(data, repository, number):
    ticket = {
        "ticket_key": f"{repository}#{number}",
        "ticket_number": number,
        "ticket_repository": repository,
        "ticket_url": f"https://github.com/{repository}/issues/{number}",
    }

    issue = fetch_issue(repository, number)
    if not issue:
        return ticket

    labels = [label.get("name", "") for label in issue.get("labels") or []]
    service_map = load_service_map(data)
    service_repos = sorted(
        {service_map[normalize_label(name)] for name in labels if normalize_label(name) in service_map}
    )

    ticket["ticket_title"] = issue.get("title", "")
    ticket["ticket_labels"] = labels
    # Same rule as the ticket workflow: no matching service label -> ticket repository.
    ticket["ticket_service_repositories"] = service_repos or [repository]
    return ticket


def capture_ticket(data):
    """Remember a ticket URL from a real user prompt. Returns the session ticket (or None)."""
    session_id = data["session_id"]
    path = state_path(session_id)
    current = read_json(path, None)

    prompt = (data.get("prompt") or "").lstrip()
    if prompt.startswith(SYNTHETIC_PROMPT_PREFIXES):
        return current

    match = ISSUE_PATTERN.search(prompt)
    if not match:
        return current

    repository, number = match.group(1), int(match.group(2))
    if current and current.get("ticket_key") == f"{repository}#{number}":
        return current

    ticket = build_ticket(data, repository, number)
    write_json_atomic(path, ticket)
    log(f"Ticket captured: {ticket['ticket_key']}")
    return ticket


# --------------------------------------------------------------------------- config


def apply_to_config(data, ticket):
    """Put `ticket` (or nothing) into the plugin config's metadata."""
    path = config_path(data)
    config = read_json(path, {})
    metadata = {k: v for k, v in (config.get("metadata") or {}).items() if k not in TICKET_KEYS}
    if ticket:
        metadata.update({k: ticket[k] for k in TICKET_KEYS if k in ticket})

    if metadata == (config.get("metadata") or {}):
        return
    if metadata:
        config["metadata"] = metadata
    else:
        config.pop("metadata", None)
    write_json_atomic(path, config)

    if ticket:
        write_json_atomic(OWNER_FILE, {"session_id": data["session_id"], "config": str(path)})
    log(f"LangSmith metadata: {ticket['ticket_key'] if ticket else 'ticket removed'}")


def release_config(data):
    owner = read_json(OWNER_FILE, {})
    if owner.get("session_id") != data["session_id"]:
        return
    apply_to_config(data, None)
    OWNER_FILE.unlink(missing_ok=True)


# --------------------------------------------------------------------------- main


def main():
    data = json.load(sys.stdin)
    event = data.get("hook_event_name")
    if not data.get("session_id"):
        return

    STATE_DIR.mkdir(parents=True, exist_ok=True)

    if event == "UserPromptSubmit":
        apply_to_config(data, capture_ticket(data))
    elif event in ("Stop", "SubagentStop"):
        apply_to_config(data, read_json(state_path(data["session_id"]), None))
    elif event == "SessionEnd":
        release_config(data)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 – a tracing helper must never break Claude Code
        log(f"Failed: {exc}")
