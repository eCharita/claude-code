"""Tests for .claude/hooks/ticket_langsmith.py (per-session LangSmith ticket metadata).

The hook runs as a real subprocess against a fake Claude config dir (CLAUDE_CONFIG_DIR) holding a
fake plugin install and a fake `gh` on PATH. The fake plugin records the CC_LANGSMITH_METADATA it
receives — that is exactly what the real plugin reads (see test_real_plugin_reads_env_metadata).
Run: python3 -m unittest discover -s .claude/tests -t .claude/tests
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

CLAUDE_DIR = Path(__file__).resolve().parent.parent
HOOK = CLAUDE_DIR / "hooks" / "ticket_langsmith.py"
PLUGIN_ID = "langsmith-tracing@langsmith-claude-code-plugins"
TICKET_KEYS = {"ticket_key", "ticket_number", "ticket_repository", "ticket_url", "ticket_title",
               "ticket_labels", "ticket_service_repositories"}
EVENTS = ["UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop", "StopFailure", "SubagentStop",
          "PreCompact", "PostCompact", "SessionEnd"]

FAKE_PLUGIN = r'''
import json, os, sys
data = json.load(sys.stdin)
record = {"event": sys.argv[1], "session": data.get("session_id"),
          "metadata": json.loads(os.environ["CC_LANGSMITH_METADATA"]) if "CC_LANGSMITH_METADATA" in os.environ else None,
          "root": os.environ.get("CLAUDE_PLUGIN_ROOT")}
line = (json.dumps(record) + "\n").encode()
fd = os.open(os.environ["FAKE_PLUGIN_LOG"], os.O_WRONLY | os.O_APPEND | os.O_CREAT)
os.write(fd, line)  # one O_APPEND write per record: safe across concurrent processes
os.close(fd)
if data.get("prompt") == "/langsmith-tracing:trace":
    print(json.dumps({"decision": "block", "reason": "thread link"}))
sys.exit(int(os.environ.get("FAKE_PLUGIN_EXIT", "0")))
'''

FAKE_GH = r'''#!/usr/bin/env python3
import json, sys
number = int(sys.argv[3])
labels = {101: ["service:assistance", "bug"], 202: ["service:PersonalAssistanceLambda"],
          303: ["service:youth-center"]}.get(number, [])
print(json.dumps({"title": f"Ticket {number}\x07 with control char", "labels": [{"name": l} for l in labels]}))
'''


class Env:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.config = root / "claude"
        self.project = root / "project"
        (self.project / ".claude").mkdir(parents=True)
        self.plugin = root / "plugin"
        (self.plugin / "hooks").mkdir(parents=True)
        (self.plugin / "fake_plugin.py").write_text(FAKE_PLUGIN)
        hooks = {event: [{"hooks": [{"type": "command", "timeout": 20,
                                     "command": f'"{sys.executable}" "$CLAUDE_PLUGIN_ROOT/fake_plugin.py" {event}'}]}]
                 for event in EVENTS}
        (self.plugin / "hooks" / "hooks.json").write_text(json.dumps({"hooks": hooks}))
        (self.config / "plugins").mkdir(parents=True)
        self.set_installed()
        self.set_enabled(True)
        bin_dir = root / "bin"
        bin_dir.mkdir()
        (bin_dir / "gh").write_text(FAKE_GH.replace("/usr/bin/env python3", sys.executable))
        (bin_dir / "gh").chmod(0o755)
        self.log = root / "plugin.log"
        self.env = {**os.environ, "CLAUDE_CONFIG_DIR": str(self.config), "CLAUDE_PROJECT_DIR": str(self.project),
                    "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "FAKE_PLUGIN_LOG": str(self.log),
                    "CC_LANGSMITH_API_KEY": "lsv2_secret_value"}
        self.env.pop("CC_LANGSMITH_METADATA", None)

    def set_installed(self, path=None):
        installs = {"plugins": {PLUGIN_ID: [{"scope": "user", "installPath": str(path or self.plugin)}]}}
        (self.config / "plugins" / "installed_plugins.json").write_text(json.dumps(installs))

    def set_enabled(self, enabled):
        (self.config / "settings.json").write_text(json.dumps({"enabledPlugins": {PLUGIN_ID: enabled}}))

    def hook(self, event, session, prompt=None, extra_env=None):
        payload = {"hook_event_name": event, "session_id": session, "cwd": str(self.project)}
        if prompt is not None:
            payload["prompt"] = prompt
        return subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload), capture_output=True,
                              text=True, env={**self.env, **(extra_env or {})}, timeout=60)

    def records(self, session=None):
        if not self.log.exists():
            return []
        rows = [json.loads(line) for line in self.log.read_text().splitlines()]
        return [r for r in rows if session is None or r["session"] == session]

    def state(self, session):
        return self.config / "ticket-langsmith" / f"{session}.json"


class TicketLangsmithTest(unittest.TestCase):
    def setUp(self):
        self.e = Env()
        self.addCleanup(self.e.tmp.cleanup)

    def test_ticket_metadata_reaches_the_plugin_with_all_fields(self):
        self.e.hook("UserPromptSubmit", "s1", "start https://github.com/eCharita/assistance/issues/101 please")
        self.e.hook("Stop", "s1")
        records = self.e.records("s1")
        self.assertEqual([r["event"] for r in records], ["UserPromptSubmit", "Stop"])
        for record in records:
            meta = record["metadata"]
            self.assertEqual(set(meta), TICKET_KEYS)
            self.assertEqual(meta["ticket_key"], "eCharita/assistance#101")
            self.assertEqual(meta["ticket_number"], 101)
            self.assertEqual(meta["ticket_labels"], ["service:assistance", "bug"])
            self.assertEqual(meta["ticket_service_repositories"], ["eCharita/assistance"])
            self.assertNotIn("\x07", meta["ticket_title"])
            self.assertEqual(record["root"], str(self.e.plugin))

    def test_session_without_ticket_adds_no_metadata(self):
        self.e.hook("UserPromptSubmit", "s1", "hello")
        self.assertIsNone(self.e.records("s1")[0]["metadata"])

    def test_existing_env_metadata_is_kept(self):
        self.e.hook("UserPromptSubmit", "s1", "https://github.com/eCharita/assistance/issues/101",
                    {"CC_LANGSMITH_METADATA": json.dumps({"team": "helio"})})
        meta = self.e.records("s1")[0]["metadata"]
        self.assertEqual(meta["team"], "helio")
        self.assertEqual(meta["ticket_number"], 101)

    def test_synthetic_prompt_does_not_switch_ticket(self):
        self.e.hook("UserPromptSubmit", "s1", "https://github.com/eCharita/assistance/issues/101")
        self.e.hook("UserPromptSubmit", "s1", "<task-notification> https://github.com/eCharita/x/issues/202")
        self.assertEqual(self.e.records("s1")[-1]["metadata"]["ticket_number"], 101)

    def test_new_ticket_url_switches_ticket(self):
        self.e.hook("UserPromptSubmit", "s1", "https://github.com/eCharita/assistance/issues/101")
        self.e.hook("UserPromptSubmit", "s1", "now https://github.com/eCharita/PersonalAssistanceLambda/issues/202")
        meta = self.e.records("s1")[-1]["metadata"]
        self.assertEqual(meta["ticket_service_repositories"], ["eCharita/PersonalAssistanceLambda"])

    def test_unknown_service_label_adds_no_repository(self):
        self.e.hook("UserPromptSubmit", "s1", "https://github.com/eCharita/assistance/issues/303")
        self.assertEqual(self.e.records("s1")[0]["metadata"]["ticket_service_repositories"], [])

    def test_concurrent_sessions_never_exchange_metadata(self):
        tickets = {"sA": ("eCharita/assistance", 101), "sB": ("eCharita/PersonalAssistanceLambda", 202)}
        for session, (repo, number) in tickets.items():
            self.e.hook("UserPromptSubmit", session, f"https://github.com/{repo}/issues/{number}")

        def turn(args):
            session, event = args
            return self.e.hook(event, session).returncode

        jobs = [(s, ev) for _ in range(6) for ev in ("PreToolUse", "PostToolUse", "SubagentStop", "Stop")
                for s in tickets]
        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(set(pool.map(turn, jobs)), {0})
        for session, (repo, number) in tickets.items():
            records = self.e.records(session)
            self.assertEqual(len(records), 1 + 24)
            self.assertEqual({r["metadata"]["ticket_key"] for r in records}, {f"{repo}#{number}"})
        shared = self.e.project / ".claude" / "langsmith.json"
        self.assertFalse(shared.exists(), "the shared config must not be written")

    def test_session_end_cleans_up_and_stale_sessions_expire(self):
        self.e.hook("UserPromptSubmit", "s1", "https://github.com/eCharita/assistance/issues/101")
        self.e.hook("UserPromptSubmit", "crashed", "https://github.com/eCharita/assistance/issues/101")
        old = time.time() - 8 * 24 * 3600
        os.utime(self.e.state("crashed"), (old, old))
        self.e.hook("SessionEnd", "s1")
        self.assertFalse(self.e.state("s1").exists())
        self.assertFalse(self.e.state("crashed").exists())

    def test_interrupted_write_does_not_break_the_session(self):
        self.e.state("s1").parent.mkdir(parents=True, exist_ok=True)
        self.e.state("s1").write_text('{"ticket_key": "eCharita/assist')  # truncated
        result = self.e.hook("Stop", "s1")
        self.assertEqual(result.returncode, 0)
        self.assertIsNone(self.e.records("s1")[0]["metadata"])
        leftovers = [p for p in self.e.state("s1").parent.iterdir() if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_no_secrets_in_metadata(self):
        self.e.hook("UserPromptSubmit", "s1", "https://github.com/eCharita/assistance/issues/101")
        self.e.state("s1").write_text(json.dumps({**json.loads(self.e.state("s1").read_text()),
                                                  "api_key": "lsv2_secret_value", "env": dict(os.environ)}))
        self.e.hook("Stop", "s1")
        meta = self.e.records("s1")[-1]["metadata"]
        self.assertEqual(set(meta), TICKET_KEYS)
        self.assertNotIn("lsv2_secret_value", json.dumps(meta))

    def test_plugin_failures_never_block(self):
        result = self.e.hook("Stop", "s1", extra_env={"FAKE_PLUGIN_EXIT": "1"})
        self.assertEqual(result.returncode, 0)
        self.e.set_installed(self.e.project / "missing")
        self.assertEqual(self.e.hook("Stop", "s1").returncode, 0)

    def test_plugin_output_is_relayed(self):
        result = self.e.hook("UserPromptSubmit", "s1", "/langsmith-tracing:trace")
        self.assertEqual(json.loads(result.stdout)["decision"], "block")

    def test_plugin_disabled_in_user_settings_is_not_run(self):
        self.e.set_enabled(False)
        self.e.hook("UserPromptSubmit", "s1", "https://github.com/eCharita/assistance/issues/101")
        self.assertEqual(self.e.records(), [])

    def test_gh_failure_still_records_basic_ticket(self):
        self.e.env["PATH"] = "/usr/bin:/bin"  # no gh
        self.e.hook("UserPromptSubmit", "s1", "https://github.com/eCharita/assistance/issues/101")
        meta = self.e.records("s1")[0]["metadata"]
        self.assertEqual(meta["ticket_key"], "eCharita/assistance#101")

    def test_gh_failure_is_retried_on_next_prompt(self):
        path = self.e.env["PATH"]
        self.e.env["PATH"] = "/usr/bin:/bin"
        self.e.hook("UserPromptSubmit", "s1", "https://github.com/eCharita/assistance/issues/101")
        self.e.env["PATH"] = path
        self.e.hook("UserPromptSubmit", "s1", "continue")
        self.assertEqual(self.e.records("s1")[-1]["metadata"]["ticket_labels"], ["service:assistance", "bug"])

    def test_failed_lookup_is_retried_a_bounded_number_of_times(self):
        self.e.env["PATH"] = "/usr/bin:/bin"
        for _ in range(5):
            self.e.hook("UserPromptSubmit", "s1", "https://github.com/eCharita/assistance/issues/101")
        self.assertEqual(json.loads(self.e.state("s1").read_text())["fetch_attempts"], 3)

    def test_shared_config_is_never_written(self):
        shared = self.e.project / ".claude" / "langsmith.json"
        shared.write_text('{"metadata": {"ticket_key": "x#1"}}')
        self.e.hook("UserPromptSubmit", "s1", "https://github.com/eCharita/assistance/issues/101")
        self.assertEqual(shared.read_text(), '{"metadata": {"ticket_key": "x#1"}}')


class Registration(unittest.TestCase):
    """settings.json must run the wrapper for every plugin event and disable the plugin's own hooks."""

    @classmethod
    def setUpClass(cls):
        cls.settings = json.loads((CLAUDE_DIR / "settings.json").read_text())
        config = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
        installs = config / "plugins" / "installed_plugins.json"
        cls.plugin = None
        if installs.exists():
            for install in json.loads(installs.read_text()).get("plugins", {}).get(PLUGIN_ID, []):
                if (Path(install["installPath"]) / "hooks" / "hooks.json").exists():
                    cls.plugin = Path(install["installPath"])

    def wrapper_hooks(self, event):
        return [h for g in self.settings["hooks"].get(event, []) for h in g["hooks"]
                if "hooks/ticket_langsmith.py" in h["command"]]

    def test_native_plugin_hooks_are_disabled_in_project(self):
        self.assertIs(self.settings["enabledPlugins"][PLUGIN_ID], False)

    def test_every_event_is_wrapped(self):
        for event in EVENTS:
            with self.subTest(event=event):
                self.assertEqual(len(self.wrapper_hooks(event)), 1)

    def test_wrapper_matches_installed_plugin(self):
        if self.plugin is None:
            self.skipTest("LangSmith plugin not installed")
        hooks = json.loads((self.plugin / "hooks" / "hooks.json").read_text())["hooks"]
        self.assertEqual(set(hooks), set(EVENTS), "plugin events changed: update settings.json + EVENTS")
        for event, groups in hooks.items():
            plugin_hook = groups[0]["hooks"][0]
            mine = self.wrapper_hooks(event)[0]
            with self.subTest(event=event):
                self.assertGreater(mine["timeout"], plugin_hook["timeout"])
                self.assertEqual(mine.get("async", False), plugin_hook.get("async", False))

    def test_real_plugin_reads_env_metadata(self):
        """Canary: the isolation relies on the plugin preferring CC_LANGSMITH_METADATA over the files."""
        if self.plugin is None:
            self.skipTest("LangSmith plugin not installed")
        bundle = (self.plugin / "bundle" / "dispatch.js").read_text(encoding="utf-8")
        self.assertIn("process.env.CC_LANGSMITH_METADATA", bundle)
        self.assertIn("metadata: customMetadata", bundle)          # env source of mergeCommonConfig
        self.assertIn("const precedence = [lock, env, ...files", bundle)


if __name__ == "__main__":
    unittest.main()
