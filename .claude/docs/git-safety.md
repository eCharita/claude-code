# Git safety (STRICT RULE 2)

Two layers protect `main` and `master` in every HelioCare repository. Neither relies on instructions alone.

## 1. Local: `.claude/hooks/git_guard.py` (PreToolUse, Bash)

Runs before every Bash command of the main session and of every subagent. It tokenizes the command,
follows the directory (`cd`, `pushd`, subshells, `git -C`, `GIT_DIR`), follows branch switches earlier in
the same command, and asks git for the real state (current branch, upstream, `push.default`,
`remote.<name>.push`, `remote.<name>.mirror`, aliases). Exit code 2 blocks the command with a reason.

| Blocked | Examples |
|---|---|
| commit-like on a protected branch | `git commit`, `merge`, `pull`, `rebase`, `cherry-pick`, `revert`, `am`, aliases of them — except catching up with the remote: `pull --ff-only [<remote> <same branch>]`, `merge --ff-only <remote>/<same branch>` or `@{u}` |
| ref rewrite of a protected branch | `git update-ref refs/heads/main …` / `update-ref HEAD …` on main, `git symbolic-ref HEAD refs/heads/main` followed by a commit, `git branch -f/-M/-C … main`, `git checkout -B main`, `git switch -C master` |
| push to a protected branch | `git push origin main`, `HEAD:main`, `x:refs/heads/main`, `+x:main`, `:main`, `--delete main`, plain `git push` / `git push origin HEAD` on main, `push.default=upstream` to main, configured push refspecs to main |
| push of every branch | `--all`, `--branches`, `--mirror`, mirror remotes, `push.default=matching`, wildcard refspecs, `--prune` without explicit refspecs |
| cannot be evaluated (fail-safe) | shell expansion in the directory, subcommand or refspec (`git push origin "$B"`), `xargs git …`, a shell reading commands from stdin (`bash <<EOF`, `… | sh`), unknown directory (`cd -`), a target that is not a repository yet, a crash of the guard while the command contains a relevant git subcommand, unreadable hook input |

Allowed: everything on ticket branches (incl. force pushes), `fetch`, `status`, `log`, `diff`, `stash`,
`checkout <branch>`, `pull --ff-only` on main, `--dry-run` pushes, `git push --tags`. `bash -c`/`-lc`/`sh -ec` strings and
readable `bash <script>` files are evaluated like the command itself. Commit
messages in heredocs or quotes are text, not commands.

Known limits: `git reset <commit>` on main is not blocked (it creates no commit; `reset --hard` is denied in
settings.json). `gh` commands that change refs on GitHub (`gh api`, `gh pr merge` — the latter needs
confirmation) are not evaluated. The hook only runs in Claude Code sessions that load this project's
`.claude/settings.json` (started in the HelioCare root). Layer 2 covers everything else.

The deny list in `settings.json` (`git push * main*`, …) stays as a second, coarse net.
Tests: `.claude/tests/test_git_guard.py` (real temporary repositories).

## 2. Remote: GitHub rulesets (configured by the developer, not by Claude)

Configure for **every** eCharita repository in `.claude/config/repos.yaml` and `eCharita/claude-code`
(Settings → Rules → Rulesets, or an organization ruleset targeting all repositories):

- Target branches: `main`, `master` (or "Default branch").
- Restrict deletions — on.
- Block force pushes — on.
- Require a pull request before merging — on (≥ 1 approval if more than one developer works on the repo).
- Bypass list: empty, or only the repository admins for emergencies.
- Optional: require status checks once CI exists.

Check the current state (read-only): `gh api repos/eCharita/<repo>/rulesets` and
`gh api repos/eCharita/<repo>/branches/main/protection`.
