---
name: ticket-start
description: Start work on a GitHub ticket — runs the ticket-analyzer agent (read ticket, map service labels to repos, create ticket-linked branches, technical analysis). Usage /ticket-start <issue URL>
argument-hint: <github-issue-url>
---

Start work on ticket: $ARGUMENTS

1. If no ticket URL/number was given, ask for it and stop.
2. Delegate to the `ticket-analyzer` agent with the ticket URL and any extra context from the conversation.
3. Show the developer the analysis it returns (branches, per-repo changes, open questions).
4. Do not stop for confirmation — the developer runs this unattended and reviews the result afterwards. Unmapped labels and auto-stashes are only reported, not asked about.
5. Continue with the implementation following the UI-First workflow, then run `/clean-code-review` and post the ticket summary comment (Ticket Workflow in `.claude/CLAUDE.md`), including any stashes that were made.
