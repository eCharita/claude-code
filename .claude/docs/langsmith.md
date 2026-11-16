# LangSmith ticket metadata (per session)

Every trace of a session that works on a GitHub ticket carries `ticket_key`, `ticket_number`,
`ticket_repository`, `ticket_url`, `ticket_title`, `ticket_labels` and `ticket_service_repositories`.

## How the plugin reads metadata (langsmith-tracing 0.6.1, verified in its bundle)

On every hook the plugin merges metadata from, highest precedence first: the env var
`CC_LANGSMITH_METADATA`, `<cwd>/.claude/langsmith.json`, `<cwd>/langsmith-plugins.json`,
`~/.claude/langsmith.json`, `~/.langsmith-plugins.json`. The files are shared by all sessions in a directory,
so writing the ticket there (the previous implementation) lets parallel sessions overwrite each other.
Atomic writes do not fix that.

## Mechanism

- `.claude/settings.json` disables the plugin's own hooks for this project
  (`enabledPlugins: {"langsmith-tracing@langsmith-claude-code-plugins": false}`) and registers
  `.claude/hooks/ticket_langsmith.py` for every event the plugin uses, with the same `async` flags and a
  slightly longer timeout.
- The wrapper finds the plugin in `~/.claude/plugins/installed_plugins.json` and runs the plugin's own hook command
  from its `hooks/hooks.json`, with `CC_LANGSMITH_METADATA` = existing env metadata + this session's ticket.
  The env var exists only in that child process, so sessions cannot see each other's ticket.
- The ticket is captured on `UserPromptSubmit` from a GitHub issue URL in a real user prompt (not in
  harness-injected prompts), read via `gh`, and stored in `~/.claude/ticket-langsmith/<session_id>.json`.
  Only that session's own hooks write that file (atomically). `ticket_service_repositories` comes from
  `.claude/lib/repo_scope.py`.
- Cleanup: the file is deleted on `SessionEnd`; files of crashed sessions expire after 7 days. A truncated
  file is ignored. A failed `gh` lookup is retried on at most 3 prompts. The shared `.claude/langsmith.json` is
  never written; ticket keys an older version left there must be removed by hand (they would be overridden by
  the env metadata of sessions with a ticket, but would show up in sessions without one).
- Only the seven keys above, as bounded plain strings/ints/lists, go into the metadata. No env values and no
  credentials. The plugin's own redaction stays on.
- Never blocking: wrapper errors are logged to stderr and it exits 0. Only the plugin's own exit code 2
  is passed through.
- The wrapper runs the plugin only if it is installed **and** enabled in the user settings
  (`~/.claude/settings.json` → `enabledPlugins`). Without the plugin it does nothing.

## Verification

- `.claude/tests/test_ticket_langsmith.py`: a fake plugin records the metadata it receives. It covers concurrent
  sessions (2 sessions × 24 parallel hooks), cleanup, crashed sessions, secrets, plugin failures and the
  registration against the installed plugin's `hooks.json`. A canary test fails if a plugin update stops
  reading `CC_LANGSMITH_METADATA` with top precedence.
- Manual end-to-end check on 2026-11-16: the real plugin 0.6.1 (binary) ran through the wrapper against a
  local fake LangSmith endpoint. Two sessions with different tickets ran in parallel, and every uploaded
  run carried the ticket of its own session.

## Limitations

- The plugin's slash commands `/langsmith-tracing:trace|mute|unmute` are not available in this project.
  The plugin is disabled here, so Claude Code does not register them. Use them from a session in another
  directory, or temporarily re-enable the plugin.
- Re-enabling the plugin in `.claude/settings.local.json` would trace every event twice.
- Any GitHub issue URL in a real user prompt switches the session's ticket, also a passing mention
  ("see also …/issues/5"). Paste the working ticket's URL again to switch back.
- After a plugin update that adds hook events, `test_wrapper_matches_installed_plugin` fails until
  `settings.json` registers the new events.
