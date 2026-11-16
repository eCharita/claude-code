"""Tests for .claude/hooks/git_guard.py against real temporary git repositories.

Run: python3 -m unittest discover -s .claude/tests -t .claude/tests
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

HOOKS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hooks")
sys.path.insert(0, HOOKS)
import git_guard  # noqa: E402

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def git(directory, *args):
    subprocess.run(["git", "-C", directory, *args], check=True, capture_output=True,
                   env={**os.environ, **GIT_ENV})


class GitGuardTest(unittest.TestCase):
    """Workspace: <tmp>/origin.git (bare), <tmp>/app (clone, on main) and <tmp>/lib (on a ticket branch)."""

    @classmethod
    def setUpClass(cls):
        cls._env = {k: os.environ.get(k) for k in GIT_ENV}
        os.environ.update(GIT_ENV)
        cls.tmp = tempfile.TemporaryDirectory()
        root = cls.root = cls.tmp.name
        cls.app, cls.lib = os.path.join(root, "app"), os.path.join(root, "lib")
        git(root, "init", "-q", "--bare", "-b", "main", "origin.git")
        for repo in (cls.app, cls.lib):
            git(root, "clone", "-q", os.path.join(root, "origin.git"), os.path.basename(repo))
            git(repo, "commit", "-q", "--allow-empty", "-m", "init")
            git(repo, "push", "-q", "origin", "HEAD:main")
            git(repo, "branch", "-q", "--set-upstream-to=origin/main", "main")
        git(cls.app, "branch", "-q", "12-ticket")
        git(cls.app, "branch", "-q", "master")
        # ticket branch created from origin/main tracks origin/main (common with `checkout -b x origin/main`)
        git(cls.lib, "checkout", "-q", "-b", "12-ticket", "origin/main")
        git(cls.lib, "push", "-q", "origin", "12-ticket")
        git(cls.app, "config", "alias.ci", "commit")
        git(cls.app, "config", "alias.pm", "!git push origin main")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()
        for key, value in cls._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def blocked(self, command, cwd=None):
        return git_guard.check(command, cwd or self.app)

    def assertBlocked(self, command, cwd=None):
        self.assertIsNotNone(self.blocked(command, cwd), command)

    def assertAllowed(self, command, cwd=None):
        self.assertIsNone(self.blocked(command, cwd), command)


class Commits(GitGuardTest):
    def test_commit_on_main_is_blocked(self):
        for command in ('git commit -m "x"', f"git -C {self.app} commit -am x", "git commit --amend",
                        f"cd {self.app} && git commit -m x", "git ci -m x", "git merge 12-ticket",
                        "git cherry-pick abc", "git revert HEAD", "git rebase origin/main",
                        "git am < p.patch", "git merge --continue", "/usr/bin/git commit -m x",
                        "env FOO=1 git commit -m x", "bash -c 'git commit -m x'", "eval git commit -m x",
                        "bash -lc 'git commit -m x'", "sh -ec 'git commit -m x'", "git pull",
                        "git pull --rebase", "git pull --ff-only origin 12-ticket", "git merge --ff-only 12-ticket"):
            with self.subTest(command=command):
                self.assertBlocked(command)

    def test_commit_on_ticket_branch_is_allowed(self):
        for command in ('git commit -m "fix"', "git commit --amend --no-edit", "git rebase origin/main",
                        "git merge origin/main", f"git -C {self.lib} commit -m x"):
            with self.subTest(command=command):
                self.assertAllowed(command, self.lib)

    def test_git_dash_c_targets_the_named_repository(self):
        self.assertAllowed(f"git -C {self.lib} commit -m x", self.app)
        self.assertBlocked(f"git -C {self.app} commit -m x", self.lib)
        self.assertBlocked(f"git -C {self.root} -C app commit -m x", self.lib)

    def test_branch_switch_earlier_in_the_command_is_followed(self):
        self.assertAllowed("git switch -c 13-new && git commit -m x")
        self.assertAllowed("git checkout -b 13-new origin/main && git add . && git commit -m x")
        self.assertAllowed("git checkout 12-ticket && git commit -m x")
        self.assertBlocked("git checkout main && git commit -m x", self.lib)
        self.assertBlocked("git switch master; git commit -m x", self.lib)

    def test_subshell_directory_does_not_leak(self):
        self.assertBlocked(f"(cd {self.lib} && git status) && git commit -m x")
        self.assertAllowed(f"(cd {self.app} && git status) && git commit -m x", self.lib)

    def test_non_committing_variants_are_allowed_on_main(self):
        for command in ("git merge --ff-only origin/main", "git merge --abort", "git rebase --abort",
                        "git cherry-pick --abort", "git pull --ff-only", "git pull --ff-only origin main",
                        "git merge --ff-only @{u}", "git fetch origin", "git status",
                        "git log --oneline", "git diff main...HEAD", "git stash push -u -m x",
                        "git checkout -- file.txt", "git checkout 12-ticket"):
            with self.subTest(command=command):
                self.assertAllowed(command)

    def test_ref_rewrites_of_main_are_blocked(self):
        for command in ("git update-ref refs/heads/main HEAD", "git branch -f main HEAD",
                        "git branch -M 12-ticket main", "git checkout -B main origin/main",
                        "git switch -C master"):
            with self.subTest(command=command):
                self.assertBlocked(command, self.lib)
        self.assertAllowed("git branch -f 12-ticket HEAD", self.lib)
        self.assertAllowed("git branch -D old-branch", self.lib)

    def test_commit_message_text_is_not_a_command(self):
        command = "git commit -F - <<'EOF'\nDo not git push origin main\nEOF"
        self.assertAllowed(command, self.lib)
        self.assertAllowed('git commit -m "mention git push origin main"', self.lib)


class Pushes(GitGuardTest):
    def test_explicit_push_to_protected_branch_is_blocked(self):
        for command in ("git push origin main", "git push origin master", "git push origin HEAD:main",
                        "git push origin 12-ticket:main", "git push origin 12-ticket:refs/heads/main",
                        "git push -f origin +12-ticket:main", "git push --force-with-lease origin HEAD:master",
                        "git push origin :main", "git push origin --delete main", "git push -d origin master",
                        "git push --repo=origin main", "git push -u origin main",
                        f"git push {os.path.join(self.root, 'origin.git')} main"):
            with self.subTest(command=command):
                self.assertBlocked(command, self.lib)

    def test_implicit_push_on_main_is_blocked(self):
        for command in ("git push", "git push origin", "git push origin HEAD", "git push -u origin HEAD",
                        "git push origin @", "git pm"):
            with self.subTest(command=command):
                self.assertBlocked(command)

    def test_push_of_ticket_branch_is_allowed(self):
        # lib is on 12-ticket, which tracks origin/main: the push still goes to 12-ticket
        for command in ("git push", "git push origin", "git push origin HEAD", "git push -u origin 12-ticket",
                        "git push origin 12-ticket", "git push --force-with-lease origin 12-ticket",
                        "git push origin HEAD:12-ticket", "git push origin --tags", "git push origin v1.0",
                        "git push origin --dry-run main"):
            with self.subTest(command=command):
                self.assertAllowed(command, self.lib)

    def test_push_everything_is_blocked(self):
        for command in ("git push --all", "git push --all origin", "git push --mirror origin",
                        "git push --branches", "git push origin 'refs/heads/*:refs/heads/*'",
                        "git push --prune origin"):
            with self.subTest(command=command):
                self.assertBlocked(command, self.lib)

    def test_push_config_is_evaluated(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "r")
            git(tmp, "clone", "-q", os.path.join(self.root, "origin.git"), "r")
            git(repo, "checkout", "-q", "-b", "7-x")
            git(repo, "config", "push.default", "upstream")
            git(repo, "config", "branch.7-x.remote", "origin")
            git(repo, "config", "branch.7-x.merge", "refs/heads/main")
            self.assertBlocked("git push", repo)          # upstream mode -> main
            git(repo, "config", "push.default", "current")
            self.assertAllowed("git push", repo)
            git(repo, "config", "push.default", "matching")
            self.assertBlocked("git push", repo)
            git(repo, "config", "--unset", "push.default")
            git(repo, "config", "remote.origin.push", "refs/heads/7-x:refs/heads/master")
            self.assertBlocked("git push", repo)
            self.assertBlocked("git push origin 7-x", repo)
            git(repo, "config", "--unset", "remote.origin.push")
            git(repo, "config", "remote.origin.mirror", "true")
            self.assertBlocked("git push origin 7-x", repo)
            self.assertBlocked("git -c push.default=matching push", self.lib)

    def test_push_in_other_repository_via_dash_c(self):
        self.assertBlocked(f"git -C {self.app} push", self.lib)
        self.assertAllowed(f"git -C {self.lib} push", self.app)


class FailSafe(GitGuardTest):
    def test_unevaluable_relevant_commands_are_blocked(self):
        for command in ('git push origin "$BRANCH"', "git push origin $(git branch --show-current)",
                        "cd $REPO && git commit -m x", "git -C $REPO push", "echo main | xargs git push origin",
                        "git $CMD origin main", f"git -C {self.root}/does-not-exist commit -m x",
                        "cd - && git commit -m x", 'git push origin "unterminated'):
            with self.subTest(command=command):
                reason = self.blocked(command, self.lib)
                self.assertIsNotNone(reason, command)

    def test_shell_reading_stdin_is_blocked(self):
        self.assertBlocked("bash <<'EOF'\ngit push origin main\nEOF", self.lib)
        self.assertBlocked("echo 'git push origin main' | sh", self.lib)

    def test_subshells_do_not_leak_state(self):
        self.assertBlocked(f"ROOT=$(cd {self.lib} && pwd); git push")
        self.assertBlocked(f"X=`cd {self.lib}`; git push")
        self.assertBlocked(f"sh -c 'cd {self.lib}' && git push")
        self.assertBlocked("sh -c 'git switch 12-ticket' && git commit -m x")

    def test_alias_edge_cases(self):
        self.assertBlocked(f"git -C {self.app} pm", self.lib)
        self.assertBlocked("cd $X && git ci -m x", self.lib)

    def test_rebase_onto_main_is_blocked(self):
        self.assertBlocked("git rebase --onto origin/main old main", self.lib)

    def test_quoted_heredoc_marker_hides_nothing(self):
        self.assertBlocked('echo "<<EOF"\ngit push origin main', self.lib)

    def test_git_dir_env_and_head_moves(self):
        self.assertAllowed(f"GIT_DIR={self.lib}/.git git commit -m x", self.lib)
        self.assertBlocked(f"GIT_DIR={self.app}/.git git commit -m x", self.lib)
        self.assertBlocked("git update-ref HEAD HEAD~1")
        self.assertAllowed("git update-ref HEAD HEAD", self.lib)
        self.assertBlocked("git symbolic-ref HEAD refs/heads/main && git commit -m x", self.lib)
        self.assertAllowed("git push --tags")
        self.assertBlocked("git update-ref -m msg refs/heads/main HEAD", self.lib)
        self.assertBlocked("git symbolic-ref -m r HEAD refs/heads/main && git commit -m x", self.lib)
        self.assertBlocked("echo 'update refs/heads/main abc' | git update-ref --stdin", self.lib)

    def test_shell_without_git_relevance_is_allowed(self):
        self.assertAllowed("bash --version && git status", self.lib)

    def test_backticks_inside_heredoc_are_text(self):
        self.assertAllowed("python3 - <<'EOF'\nprint('`xargs git push origin main`')\nEOF", self.lib)

    def test_shell_script_is_evaluated(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = os.path.join(tmp, "s.sh")
            with open(script, "w", encoding="utf-8") as handle:
                handle.write(f"git -C {self.app} push\n")
            self.assertBlocked(f"bash {script}", self.lib)
            with open(script, "w", encoding="utf-8") as handle:
                handle.write(f"git -C {self.lib} push\n")
            self.assertAllowed(f"bash {script}", self.lib)

    def test_unevaluable_but_irrelevant_commands_are_allowed(self):
        for command in ('git -C "$REPO" status', "cd $X && git log", "ls $HOME", "git diff $(git merge-base HEAD main)",
                        "echo 'git push origin main'", "grep -r 'git commit' ."):
            with self.subTest(command=command):
                self.assertAllowed(command, self.lib)

    def test_nested_command_substitution_is_evaluated(self):
        self.assertBlocked('echo "$(git push origin main)"', self.lib)
        self.assertBlocked("echo `git push origin main`", self.lib)
        self.assertBlocked("sh -c 'cd .. && git -C app push'", self.lib)


class HookProtocol(GitGuardTest):
    def run_hook(self, command, cwd):
        payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": cwd,
                   "hook_event_name": "PreToolUse"}
        return subprocess.run([sys.executable, os.path.join(HOOKS, "git_guard.py")], input=json.dumps(payload),
                              capture_output=True, text=True, timeout=30)

    def test_block_exits_2_with_clear_message(self):
        result = self.run_hook("git push origin main", self.lib)
        self.assertEqual(result.returncode, 2)
        self.assertIn("BLOCKED", result.stderr)
        self.assertIn("main", result.stderr)

    def test_allow_exits_0_silently(self):
        result = self.run_hook("git push origin 12-ticket", self.lib)
        self.assertEqual((result.returncode, result.stderr), (0, ""))

    def test_non_object_input_fails_safe(self):
        result = subprocess.run([sys.executable, os.path.join(HOOKS, "git_guard.py")], input="[1]",
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 2)

    def test_unreadable_input_fails_safe(self):
        result = subprocess.run([sys.executable, os.path.join(HOOKS, "git_guard.py")], input="not json",
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 2)

    def test_registered_in_settings(self):
        with open(os.path.join(os.path.dirname(HOOKS), "settings.json"), encoding="utf-8") as handle:
            settings = json.load(handle)
        commands = [h["command"] for entry in settings["hooks"]["PreToolUse"] if entry.get("matcher") == "Bash"
                    for h in entry["hooks"]]
        self.assertTrue(any("hooks/git_guard.py" in c for c in commands), commands)


if __name__ == "__main__":
    unittest.main()
