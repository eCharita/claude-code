---
name: clean-code-review
description: Final Clean Code review of the current ticket branch(es) before a PR — runs the clean-code-reviewer agent and returns PASSED / NEEDS_REVISION / BLOCKED. Usage /clean-code-review [repo ...]
argument-hint: [repo folder ...]
---

Run the final review (REVIEWING phase, `.claude/docs/orchestration.md` §6). Repositories: $ARGUMENTS

1. If no repositories were given, use the repos touched in this conversation; if none, all HelioCare repos on a non-main branch (`git -C <repo> branch --show-current`).
2. In the agent prompt pass: open Marko gripes from this conversation (to check explicitly) and the validation results of the VALIDATING phase (command → result).
3. Delegate to the `clean-code-reviewer` agent (one call; for several repos you may run one agent per repo in parallel).
4. Show the report with the verdict for each repository.
5. Do not stop for confirmation (the developer reviews afterwards): fix all Blockers yourself, re-run the affected validation checks, then run the review once more. Clean Code improvements may be applied if they are small and local (re-validate); otherwise list them as open in the ticket comment. At most `limits.final_review_rounds` (`.claude/config/workflows.yaml`) rounds; a blocker still open after that makes the ticket BLOCKED.
6. Include the verdict and how each finding was resolved in the ticket summary comment.
