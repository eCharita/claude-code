# Agent handoff contract

Every orchestrator → implementation agent call (`ui-agent`, `lambda-agent`) uses this prompt shape.
Give the agent only what it needs — not the whole ticket thread or analysis.

## Handoff (prompt)

```yaml
task:
  ticket: eCharita/<ticket-repo>#<id> — <title>
  workflow_type: <type from .claude/config/workflows.yaml>
  step: implement:<layer> | integrate | correction   # layer as in workflows.yaml (frontend, backend, any, ...)
  mode: mock | real            # frontend only: mock data (UI-First) or real API calls
  objective: <one or two sentences>
  scope:
    allowed_repositories: [<absolute path>, ...]   # edit only here, all on branch <branch>
    read_only: [<absolute path>, ...]              # may read (e.g. FE types for the Lambda agent)
  acceptance_criteria:
    - <AC relevant to this step>
  context:                     # concrete facts only: files, types, endpoints, decisions taken
    - <...>
  dependencies:                # outputs of earlier steps this one builds on
    - <e.g. FE model assistance/src/app/x/x.model.ts defines the payload>
  validation:                  # commands from .claude/config/repos.yaml, already filled in
    - <command>
  correction:                  # only for step: correction
    failing_check: <command>
    output: <relevant error lines>
```

## Result (the agent's final message)

```
RESULT: DONE | PARTIAL | BLOCKED
Summary: <what was implemented, 1–5 bullets>
Files: <repo-relative paths, prefixed with repo folder>
Validation: <command> → PASS | FAIL (<first error line>) | NOT RUN (<why>)
Open issues: <unresolved problems, assumptions made> | none
Integration notes: <endpoints, payload types, mock locations the next step needs> | none
```

The orchestrator treats `DONE` with a failing or missing required check as `PARTIAL`, and
`BLOCKED`/`PARTIAL` as a reason to correct (within limits) or to end the ticket as BLOCKED.
Agents do not commit; the orchestrator commits to the ticket branch after validation.
