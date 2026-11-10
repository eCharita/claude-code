---
name: clean-code-review
description: Final Clean Code review of the current ticket branch(es) before a PR — runs the clean-code-reviewer agent and returns PASSED / NEEDS_REVISION / BLOCKED. Usage /clean-code-review [repo ...]
argument-hint: [repo folder ...]
---

Run the final review. Repositories: $ARGUMENTS

1. If no repositories were given, use the repos touched in this conversation; if none, all HelioCare repos on a non-main branch (`git -C <repo> branch --show-current`).
2. If a Marko loop ran in this conversation and left gripes open, pass them to the agent in its prompt so it checks them explicitly.
3. Delegate to the `clean-code-reviewer` agent (one call; for several repos you may run one agent per repo in parallel).
4. Show the report with the verdict for each repository.
5. Do not stop for confirmation (the developer reviews afterwards): fix all Blockers yourself, then run the review once more. Clean Code improvements may be applied if they are small and local; otherwise list them as open in the ticket comment. Never loop more than twice.
6. Include the verdict in the final ticket summary comment (Ticket Workflow step 6 in `.claude/CLAUDE.md`).
