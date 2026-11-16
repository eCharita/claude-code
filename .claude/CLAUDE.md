# HelioCare Development Guidelines

## Project Structure
- `assistance/`: Frontend application for OSA (eCharita/assistance)
- `day-care-center/`: Frontend application for DCC (eCharita/day-care-center)
- `PersonalAssistanceLambda/`: AWS Lambda backend (eCharita/PersonalAssistanceLambda)
- `timesheetGenerate/`: Java/Maven microservice that generates PDF timesheet reports
- `shell-app/`: Angular host application (module federation shell)
- `employee-managment/`: Angular micro-frontend for employee management (module federation remote)
- `social-consulting/`: Angular micro-frontend for social consulting (Native Federation remote `socialConsulting`, port 4203, exposes `./Routes`, mounted in `shell-app` at `/social-consulting`; blueprint = `employee-managment`). Has its own layout (toolbar + sidenav mirroring `assistance/`) in `src/app/layout/`; `shell-app` hides its global toolbar on this route (`OWN_LAYOUT_ROUTES` in `shell-app/src/app/app.component.ts`)
- `manager-cockpit/`: Angular micro-frontend for the manager cockpit (eCharita/manager-cockpit; Native Federation remote, port 4202, exposes `./Routes`; registered in `shell-app` as `mcockpit` and mounted at `/mcockpit`)
- `relax-and-fun/`: Angular app RelaxAndFun (eCharita/RelaxAndFun; ticket label `service:RelaxAndFun`)
- `aws-infrastructure/`: AWS infrastructure definitions (eCharita/aws-infrastructure) — STRICT RULE 1 applies, never deploy
- `presentation/`: Product presentation of HelioCare — plain static HTML (own git repo), see [Presentation](#presentation-subproject)

## STRICT RULES (NON-NEGOTIABLE)
1. **NO AWS DEPLOYMENT**: Never execute any AWS CLI, CDK, Serverless Framework, SAM, or deployment scripts. Deployment to AWS is 
handled strictly by the developer manually.
2. **NO COMMITS OR PUSHES TO MAIN/MASTER** (enforced for Claude Code sessions started in the HelioCare root, in every repository they touch, by `.claude/hooks/git_guard.py`; remote protection via GitHub rulesets: `.claude/docs/git-safety.md`): 
   - Never commit directly to `main` or `master` branches in any repository.
   - Always check the current git branch before committing.
   - If work needs to be saved, create or use the ticket branch (see [Ticket Workflow](#ticket-workflow-before-any-coding)); only for work without a ticket use `feature/...`.
   - Do not perform `git push` to origin main/master.

## Ticket Workflow (BEFORE ANY CODING)
Every task starts from a GitHub ticket URL (e.g. `https://github.com/eCharita/assistance/issues/912`). Before writing any code, always do the following. Steps 1–3 are automated by `/ticket-start <url>` (agent `ticket-analyzer`); the label → repository map is in `.claude/config/repos.yaml`. If a coding task comes without a ticket URL, ask for it first.

1. **Read the ticket** from the URL — do not rely only on text pasted into the chat:
   `gh issue view <id> --repo eCharita/<repo> --json number,title,body,labels,comments`
   If the pasted text and the ticket differ, the ticket wins; mention the difference.

2. **Determine the affected repositories from the labels starting with `service:`** (e.g. `service: assistance` → `assistance/`). Each `service:` label = one repository from [Project Structure](#project-structure). The repository in which the ticket lives is NOT automatically in scope — only the `service:` labels decide.
   - The mapping and its policy live in `.claude/config/repos.yaml` (`scope_policy`) and are applied only by `python3 .claude/lib/repo_scope.py <owner/repo> <labels...>`. An unknown `service:` label → BLOCKED before any branch (continue only after the developer approves or the mapping is added); no `service:` label → the ticket's own repository if it is mapped, otherwise BLOCKED.
   - If the implementation turns out to need a change in a repository that is NOT listed in the `service:` labels, **stop and ask before touching it** (do not create a branch or edit files there).

3. **Create a branch linked to the ticket in every affected repository**, so that the remote branch shows up in the ticket's *Development* section:
   - Name: `<ticket-id>-<slugified-ticket-title>` (same format GitHub's "Create a branch" produces and the repos already use, e.g. `912-filtrovani-podle-data`). Use the same branch name in all affected repositories.
   - Create it with `gh issue develop` — a plain `git push` of a branch does NOT link it to the ticket:
     `gh issue develop <id> --repo eCharita/<ticket-repo> --branch-repo eCharita/<service-repo> --name <branch> --base main`
     (`--branch-repo` is needed when the service repository differs from the repository of the ticket.)
   - Then locally in each affected repository: `git fetch origin && git checkout <branch>`.
   - If a branch for the ticket already exists (check `gh issue develop --list <id> --repo ...` and `git branch -r`), reuse it instead of creating a new one.
   - If the local working tree has uncommitted changes, stash them (`git stash push -u -m "auto-stash before #<id> (from <branch>)"`) and switch; never discard changes. Being on another branch is fine — just switch. Report every stash in the analysis and in the final ticket comment.

4. Only then implement — following the **workflow selected for the ticket** (see [Ticket Orchestration](#ticket-orchestration)). All commits go to the ticket branch; STRICT RULE 2 still applies.

5. **Before finishing, run the validation and reviews the workflow requires** (final review `/clean-code-review`, agent `clean-code-reviewer`, for every workflow except `documentation_only`) and resolve blockers.

6. **After the work is finished, always post a summary as a comment on the ticket**:
   `gh issue comment <id> --repo eCharita/<ticket-repo> --body-file <file>`
   - In the language of the ticket, using the summary template in `.claude/docs/orchestration.md` §7 (workflow, agents, repos + branches, validation, reviews, AC, open items such as AWS deployment, final status). State honestly what was NOT verified or is failing.
   - One comment per finished piece of work; if work on the same ticket continues later, add a new comment instead of editing the old one.
   - Only comment — do not close the ticket, change its labels or assignees.

## Ticket Orchestration
The main session is the orchestrator and runs **only the agents the ticket needs**. Authoritative rules: `.claude/docs/orchestration.md` (phases, scope, workflow selection, ownership, integration, validation, reviews, summary). Data: `.claude/config/workflows.yaml` (workflow types + steps + retry limits) and `.claude/config/repos.yaml` (label → repo → stack, owner, validation commands). Handoff/result format for agents: `.claude/docs/agent-handoff.md`.

After changing anything in `.claude/config`, `.claude/docs`, agents or skills, run the config tests from the HelioCare root (needs PyYAML): `python3 -m unittest discover -s .claude/tests -t .claude/tests` (consistency checks + orchestration scenarios in `.claude/tests/scenarios.yaml`).

Workflow states (ANALYZING → PREPARING → IMPLEMENTING → VALIDATING → REVIEWING → COMPLETED, or BLOCKED / FAILED) and their transitions: orchestration.md §1, `states` in workflows.yaml.

LangSmith tracing gets the session's ticket as metadata via `.claude/hooks/ticket_langsmith.py` (per session, never the shared `langsmith.json`): `.claude/docs/langsmith.md`.

Workflow types: `fullstack_feature`, `frontend_feature`, `backend_bugfix`, `frontend_bugfix`, `api_change`, `refactoring`, `infrastructure_change`, `documentation_only`.

**UI-First with Mock Data** stays the default for new full-stack features (`fullstack_feature`):
1. Frontend with strongly typed mock data (`ui-agent`) — UI logic, state and interaction complete; the types are the contract.
2. Lambda backend built to those types (`lambda-agent`), FE and BE types kept aligned.
3. Integration (orchestrator): FE calls the real endpoints instead of mock data; contract, auth and error handling verified (completion criteria in orchestration.md §4).

## Presentation Subproject
`presentation/` is a standalone product presentation of HelioCare (marketing/demo), not part of the app runtime.

- **Tech**: plain static HTML + inline CSS/JS in `index.html`. No framework, no build step, no npm. Static assets (SVG, images, fonts) may be added later as separate files next to `index.html` and referenced by relative paths.
- **Out of scope**: the UI-First / Lambda workflow above does not apply here — never touch `assistance/`, `day-care-center/` or backend code when working on the presentation.
- **Logo**: `presentation/HelioCare-H.svg`.
- **Languages**: cs / en / de. Czech is the default text in the HTML; every text has a `data-i18n="key"` (or `data-i18n-alt` for image alt) and translations live in the `I18N` object in the script. Language is picked from browser settings (`sk` → `cs`, unsupported → `en`), overridable via `?lang=cs|en|de`. Any new or changed text must be added to all three languages. Use the same terminology as the apps.
- **Design must mirror `assistance/` and `day-care-center/`** (Angular Material 3, see `src/styles.scss` and `src/styles/_colors.scss` in each app):
  - OSA (`assistance/`) = green identity (M3 `spring-green` primary); DCC (`day-care-center/`) = blue identity (M3 `azure` primary); both use `cyan` as tertiary. When showing a specific product, use its color.
  - Light theme, neutral light surfaces (sidenav uses `$color-neutral-50`), rounded cards, soft shadows, bold table headers.
  - Material Icons / Material Symbols Outlined for icons; typography in the spirit of the apps (Roboto / clean sans-serif).
  - App screenshots/mockups in the presentation should look like the real app UI (sidenav + toolbar + Material components), not a generic design.
  - Keep colors as CSS custom properties in `:root`.
- **Git**: `presentation/` has its own repository — STRICT RULE 2 applies (no commits to `main`, use a feature branch).

## Sub-Agents / Custom Commands

Subagents in `.claude/agents/` (ownership per stack in `repos.yaml`, details in orchestration.md §4):
- `ticket-analyzer` — reads the ticket, scope, branches, technical analysis and **workflow plan**. Trigger: `/ticket-start <issue-url>` (which then orchestrates the whole ticket).
- `ui-agent` — Angular apps (`stack: angular`): UI with mock data, FE bugfixes, FE integration. Also when addressed as `@ui-agent`.
- `lambda-agent` — `PersonalAssistanceLambda/` (Python Lambdas): handlers, endpoints, backend tests.
- `clean-code-reviewer` — read-only final review of the branch diff, verdict PASSED / NEEDS_REVISION / BLOCKED. Trigger: `/clean-code-review [repo ...]`.
- `timesheetGenerate` (Java/Maven), `aws-infrastructure` (Terraform, never deployed), `presentation` and docs are implemented by the orchestrator itself — no extra agent.

- `marko` skill (`.claude/skills/marko/SKILL.md`, adapted from github.com/julian-galluzzo/marko) — fast, terse inner-loop review in a forked context (read-only): verdict catastrophe / bad / mediocre / ok + `file:line` gripes, no fixes suggested. Trigger: `/marko [repo ...]` or addressing him ("marko?", "hey marko"). Does NOT replace `clean-code-reviewer` or tests.

**Marko loop**: after each implementation/integration step that changed code, without asking the developer — checkpoints, fix rules and round limit in `.claude/docs/orchestration.md` §6. Include Marko's last verdict per repository in the ticket comment.

Each agent must strictly respect the scope restrictions in its definition and the `allowed_repositories` of its handoff.
