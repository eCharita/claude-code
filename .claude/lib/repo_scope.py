#!/usr/bin/env python3
"""Ticket scope resolution — the one implementation of `scope_policy` in .claude/config/repos.yaml.

Used by the ticket-analyzer (CLI below), the LangSmith hook and the config tests, so all of them map
`service:*` labels to repositories the same way. Standard library only (no PyYAML at runtime).

CLI:  python3 .claude/lib/repo_scope.py [--approved-without-unknown] <owner/ticket-repo> [<label> ...]
      -> JSON {status, scope: [{label, repo, path, stack}], unknown_labels, fallback, approved, reason}
      --approved-without-unknown: only after the developer explicitly approved continuing without the
      unknown labels; the mapped repositories become READY, the unknown labels stay reported.
"""
import json
import os
import re
import sys

REPOS_YAML = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "repos.yaml")
# The policy values this module implements; the tests check repos.yaml states the same.
IMPLEMENTED_POLICY = {
    "unknown_service_label": "block",
    "no_service_label": "ticket_repository",
    "outside_scope_change": "ask",
}


def normalize_label(label):
    """`service: Assistance` == `service:assistance` (GitHub label names are case-insensitive)."""
    return re.sub(r"\s+", "", label).lower()


def load_repositories(path=REPOS_YAML):
    """Parses the `repositories:` list of repos.yaml (flat `key: value` items only)."""
    with open(path, encoding="utf-8") as handle:
        lines = handle.read().splitlines()
    entries, current, inside = [], None, False
    for line in lines:
        if re.match(r"^\S", line):
            inside = line.startswith("repositories:")
            continue
        if not inside or not line.strip() or line.strip().startswith("#"):
            continue
        match = re.match(r'^\s*(-\s+)?(\w+):\s*"?([^"#]*?)"?\s*(#.*)?$', line)
        if not match:
            continue
        if match.group(1):
            current = {}
            entries.append(current)
        if current is not None:
            current[match.group(2)] = match.group(3)
    return entries


def resolve_scope(ticket_repo, labels, repositories=None, approved_without_unknown=False):
    """Applies scope_policy. Returns {status, scope, unknown_labels, fallback, reason}.

    status READY   -> `scope` is the complete list of repositories the ticket may change.
    status BLOCKED -> nothing may be changed (no branch either) until the developer adds the mapping,
                      removes the label, or explicitly approves continuing with `scope` only.
    """
    repositories = load_repositories() if repositories is None else repositories
    by_label = {normalize_label(r["label"]): r for r in repositories}
    service_labels, seen = [], set()
    for label in labels:
        norm = normalize_label(label)
        if norm.startswith("service:") and norm not in seen:
            seen.add(norm)
            service_labels.append(label)
    scope = []
    for label in service_labels:
        entry = by_label.get(normalize_label(label))
        if entry and entry not in scope:
            scope.append(entry)
    unknown = [label for label in service_labels if normalize_label(label) not in by_label]
    result = {"status": "READY", "scope": scope, "unknown_labels": unknown, "fallback": False,
              "approved": False, "reason": ""}

    if unknown and approved_without_unknown and scope:
        result.update(approved=True, reason=f"developer approved continuing without {', '.join(unknown)}")
    elif unknown:
        result["status"] = "BLOCKED"
        result["reason"] = (f"unmapped service label(s) {', '.join(unknown)}: add the mapping to "
                            ".claude/config/repos.yaml or remove the label; continuing with the mapped "
                            "repositories only needs explicit developer approval")
    elif not service_labels:
        own = [r for r in repositories if r["repo"].lower() == ticket_repo.lower()]
        if own:
            result.update(scope=own, fallback=True,
                          reason=f"no service: label; using the ticket repository {ticket_repo}")
        else:
            result.update(status="BLOCKED",
                          reason=f"no service: label and the ticket repository {ticket_repo} is not in "
                                 ".claude/config/repos.yaml")
    return result


def main(argv):
    approved = "--approved-without-unknown" in argv
    args = [a for a in argv[1:] if a != "--approved-without-unknown"]
    if not args:
        print(__doc__, file=sys.stderr)
        return 1
    print(json.dumps(resolve_scope(args[0], args[1:], approved_without_unknown=approved), indent=2,
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
