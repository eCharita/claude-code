---
name: ticket-start
description: Start and orchestrate work on a GitHub ticket — runs the ticket-analyzer agent (read ticket, map service labels to repos, create ticket-linked branches, select the workflow), then executes only the agents and checks that workflow needs, through review to the ticket summary. Usage /ticket-start <issue URL>
argument-hint: <github-issue-url>
---

Start work on ticket: $ARGUMENTS

You are the orchestrator. Read `.claude/docs/orchestration.md` now and follow it.

1. If no ticket URL/number was given, ask for it and stop.
2. ANALYZING / PREPARING: delegate to the `ticket-analyzer` agent with the ticket URL and any extra context from the conversation.
3. Show the developer the analysis (branches, per-repo changes, workflow plan, open questions). If the plan says `Status: BLOCKED`: when the cause is an unknown `service:` label and the developer is present, ask once whether to continue with the mapped repositories only (an explicit "yes" is the approval — re-run the analyzer with it — it calls `.claude/lib/repo_scope.py --approved-without-unknown` — and record it in the summary); otherwise post the summary comment with status BLOCKED and stop.
4. State the selected workflow and the agents that will / will not run in one short paragraph, then continue without waiting for confirmation — the developer runs this unattended. Auto-stashes are only reported, not asked about. Report each state transition (`State: X → Y (reason)`, orchestration.md §1).
5. IMPLEMENTING → VALIDATING → REVIEWING exactly as the plan's steps say (handoffs per `.claude/docs/agent-handoff.md`, Marko loops, checks from `repos.yaml`, `/clean-code-review`), within the limits in `.claude/config/workflows.yaml`.
6. Commit the work to the ticket branch of each repo (never main/master) — do not push (`git push` needs the developer's confirmation; the summary states that the commits are not pushed yet) — then post the ticket summary (orchestration.md §7), including stashes, skipped steps and anything not verified.
