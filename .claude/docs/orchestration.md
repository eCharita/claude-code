# Ticket orchestration

The main Claude Code session is the **orchestrator**. It reads this file when starting a ticket
(`/ticket-start`) and drives the ticket from analysis to the summary comment. It does not run every
agent — only those the selected workflow needs.

Data lives in config, not here:
- `.claude/config/repos.yaml` — label → repo → stack, layer, **owner**, validation commands.
- `.claude/config/workflows.yaml` — workflow types, their steps, retry limits.
- `.claude/docs/agent-handoff.md` — handoff prompt and result format for implementation agents.

STRICT RULES in `.claude/CLAUDE.md` always apply (no AWS deployment, no commit/push to main/master).

## 1. Phases

`ANALYZING → PREPARING → IMPLEMENTING → VALIDATING → REVIEWING → COMPLETED`, with the terminal states
`BLOCKED` (cannot continue safely — missing information, scope needs approval, unresolved check) and
`FAILED` (attempted and broken, e.g. a check still fails after the correction limit).
A failed check or a blocking review finding moves back to `IMPLEMENTING`.
Unresolved dependency: the workflow state model belongs to ticket eCharita/claude-code#3 (still open).
These names follow #3; this section is provisional — when #3 lands, merge it here instead of keeping two models.

| Phase | What happens | Who |
|---|---|---|
| ANALYZING | read ticket + AC, map labels, classify workflow, build the plan | `ticket-analyzer` |
| PREPARING | create/reuse ticket branches, stash dirty trees | `ticket-analyzer` |
| IMPLEMENTING | run the plan's `implement` / `contract_analysis` / `integrate` steps + Marko loops | owners, orchestrator |
| VALIDATING | run required checks per repo (§5) | orchestrator |
| REVIEWING | `/clean-code-review`, fix blockers, re-validate | `clean-code-reviewer`, orchestrator |
| COMPLETED / BLOCKED / FAILED | ticket summary comment (§7) | orchestrator |

Routine steps run without asking the developer. Ask (or end as `BLOCKED`) only for: a repository outside
the ticket scope, a decision the ticket leaves open that changes behaviour, or a STRICT RULE conflict.

## 2. Scope

- In-scope repositories = the `service:` labels mapped through `repos.yaml`. Nothing else is edited.
- Unknown label → skip it and report it. No mapped label at all → the ticket's own repository if it is in
  `repos.yaml` (reported); otherwise `BLOCKED`.
- A needed change in an out-of-scope repository (e.g. a second frontend consuming a changed endpoint) →
  do not touch it — stop and ask (CLAUDE.md, Ticket Workflow step 2). In unattended runs that part ends
  as BLOCKED and is reported as an open follow-up; the in-scope work continues.
- Every handoff lists `allowed_repositories`; an agent never edits outside that list. Reading other
  repositories (e.g. FE types from the Lambda agent) is allowed.

## 3. Workflow selection

The analyzer picks exactly one type from `workflows.yaml` and states a one-line rationale; the
orchestrator repeats the plan in one short paragraph before IMPLEMENTING and records it in the summary.

| Signal in ticket / labels | Workflow |
|---|---|
| new behaviour, frontend + backend repos, endpoint does not exist yet | `fullstack_feature` (UI-First, mock data) |
| new behaviour, only frontend repos (or backend already serves the data) | `frontend_feature` |
| defect, only backend repos, payload shape unchanged | `backend_bugfix` |
| defect, only frontend repos | `frontend_bugfix` |
| endpoint/payload added, renamed or changed for an existing consumer | `api_change` (contract first) |
| "refactor", "cleanup", no behaviour change requested | `refactoring` |
| only `aws-infrastructure` | `infrastructure_change` |
| only README/docs/comments | `documentation_only` |

Rules:
- Labels limit the layers: no frontend repo in scope → no `ui-agent`; no Lambda repo → no `lambda-agent`.
  A step whose layer has no in-scope repo is skipped (and listed as skipped in the plan); `integrate`
  runs only when both a frontend and a backend step ran.
- `fullstack_feature` keeps UI-First: frontend with strongly typed mock data → backend built to those
  types → integration. Choose `api_change` instead when the backend contract must be settled first
  (existing consumers, other apps).
- Refactoring must preserve behaviour; existing tests must pass unchanged (a changed assertion needs a
  reason in the summary).
- `infrastructure_change` never runs `terraform init/plan/apply/import/state` or any AWS command (STRICT RULE 1);
  only `terraform fmt -check` and `validate` on an already initialized environment.
- `documentation_only` runs no implementation agent and no Marko; the orchestrator edits the docs.
- Ticket mixes types (e.g. bugfix + small feature) → choose the broader workflow.
- Ambiguous ticket → make the safest assumption that stays within the labels, write it into the plan
  and the summary; if critical information is missing (what should happen, which repo) → `BLOCKED`.

## 4. Agents and ownership

The owner of each stack is in `repos.yaml`. Responsibilities:

| Agent | Owns | Must not |
|---|---|---|
| `ticket-analyzer` | ticket reading, scope, workflow plan, branches | write application code |
| `ui-agent` | Angular apps (`stack: angular`): components, UX, mock data, FE unit tests, FE build | edit backend repos unless the handoff authorizes it |
| `lambda-agent` | `PersonalAssistanceLambda` (Python 3.12): handlers, business logic, endpoints, payload validation, unittest tests, contract implementation | edit frontend repos unless authorized; deploy |
| orchestrator | `java-maven` (`timesheetGenerate`), `terraform` (`aws-infrastructure`), `static-html` (`presentation`), docs, contract analysis, integration, validation, summary | deploy; leave the ticket scope |
| Marko (`/marko`) | incremental review at checkpoints | edit |
| `clean-code-reviewer` | final review verdict | edit |

**No separate service agent or integration agent.** `timesheetGenerate` is the only non-Lambda service
and rarely changes; a dedicated agent would cost a cold start per ticket without adding a boundary the
orchestrator lacks. Revisit when there are two or more Java services or Java tickets become frequent.
Integration stays with the orchestrator for the same reason.

Execution order: the order of steps in the workflow. Within one step, independent repositories of the
same owner go into one handoff; agents with no dependency on each other may run in parallel.

### Integration (orchestrator)
Done when all of these hold:
1. FE services call the real endpoints; mock data is removed or kept only as test fixtures.
2. Contract table matches (below) — method, path, request fields, response fields, types, nullability.
3. Auth: the FE sends the same token/claims the Lambda checks (layers/auth); 401/403 handled in FE.
4. Errors: Lambda error status/body handled in FE (message to the user, no silent failure).
5. Build + tests of every touched repo pass.
Small integration edits are made by the orchestrator; larger FE rewrites go to `ui-agent` with
`mode: real` in the handoff.

### Contract analysis / validation
There is no OpenAPI schema, so contract validation is a documented diff executed by the orchestrator:
- List each touched endpoint: `METHOD path` | Lambda handler file | FE model/service file.
- Compare fields name by name (FE interface vs Lambda request parsing / response dict).
- Find every other consumer: Grep the endpoint path in all FE repos from `repos.yaml`.
- Breaking change (removed/renamed field, changed type) for a consumer outside the scope → BLOCKED
  or ask; prefer additive changes.
Report it as `Contract: PASS/FAIL (manual diff, N endpoints)` — never as an automated check.

## 5. Validation

- Commands come from `repos.yaml` for the repo's stack. Never invent a command; a category the stack
  lists under `unavailable` (or does not list) is reported as `UNAVAILABLE (<reason>)`; a `covered`
  category takes the result of the category it is covered by. `manual_checks` (`contract`, `docs`) are
  done by the orchestrator and always end PASS or FAIL.
- Every touched repository needs at least one check that actually ran (automated or manual). If all its
  required categories are UNAVAILABLE (e.g. `presentation`), do a manual check and report it as
  `manual: PASS/FAIL (<what was checked>)` — for `presentation`: every `data-i18n` key exists in cs/en/de.
- Run the categories required by the workflow for every touched repository: Angular → build + test;
  Lambda → py_compile changed files + unittest of every changed module that has `tests/`; Java → mvn
  compile + test; Terraform → fmt on changed files + validate of affected environments; docs → referenced
  paths/commands exist.
- Failing check → back to IMPLEMENTING: the owner of that repo gets a correction handoff with the error
  output; re-run the check. At most `limits.correction_cycles` per check, then `BLOCKED` (unclear fix)
  or `FAILED` (attempted, still broken).
- Pre-existing failure (the failing test/file is not touched by the branch and fails on `origin/main`
  too) → report as `PRE-EXISTING`, do not fix out of scope, does not block.
- Re-run validation after every correction from review.
- A review verdict never replaces a check. No check is reported as PASS unless it ran in this session.

## 6. Reviews

- **Marko loop** (checkpoints = after each `implement`/`integrate` step that changed code; not for
  docs-only or a few-line trivial change): `/marko <repos of that step>`, fix gripes that affect
  correctness, project rules or readability (skip nits), run again. Max `limits.marko_rounds`; open gripes
  go to the final review.
- **Final review** after validation passes: `/clean-code-review <touched repos>`; fix every Blocker,
  re-validate, re-review. Max `limits.final_review_rounds`. Unresolved blocker → `BLOCKED`.
- The orchestrator evaluates each finding (fix / reject with reason); the summary lists them.

## 7. Ticket summary (developer handoff)

Posted with `gh issue comment <id> --repo eCharita/<ticket-repo> --body-file <file>`, in the ticket's
language, one new comment per finished piece of work. Only facts from this session.

```
## Implementation Summary
### Ticket            #<id> — <title>
### Workflow          <type> — <one-line rationale>; skipped steps: <...>
### Agents executed   <agent: what it did>  (and agents deliberately not run)
### Repositories      <repo>: <branch> — <changes>; stashes: <...>
### Validation        <repo> <category>: PASS | FAIL | UNAVAILABLE (<reason>) | PRE-EXISTING | NOT RUN
### Code review       Marko: <last verdict per repo>; Clean Code Reviewer: <verdict>; findings resolved/open
### Acceptance criteria  AC<n>: PASS | FAIL | NOT VERIFIED (<why>)
### Remaining risks / open  <follow-ups, out-of-scope repos, manual AWS deployment>
### Final status      COMPLETED | BLOCKED | FAILED
```

`COMPLETED` only if every required check is PASS / UNAVAILABLE / PRE-EXISTING (manual checks: PASS), no review blocker is
open and no AC is FAIL. Do not close the ticket or change labels/assignees.
