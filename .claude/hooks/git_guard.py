#!/usr/bin/env python3
"""PreToolUse hook (Bash): blocks commits and pushes to protected branches (STRICT RULE 2).

The hook does not pattern-match the command text. It tokenizes the shell command, follows the
working directory (`cd`, `pushd`, `git -C`, subshells), simulates branch switches made earlier in
the same command (`git switch -c x && git commit`) and asks git itself for the repository state
(current branch, upstream, `push.default`, `remote.<name>.push`, aliases). Then it decides per git
invocation:

* commit-like commands (`commit`, `merge`, `rebase`, `cherry-pick`, `revert`, `am`) on a protected
  branch -> block;
* ref rewrites onto a protected branch (`update-ref`, `branch -f/-m/-c`, `checkout -B`, `switch -C`)
  -> block;
* `push` whose effective destination (explicit refspec, `HEAD`, upstream, `push.default`,
  configured push refspecs) is a protected branch, or that pushes every branch
  (`--all`, `--branches`, `--mirror`, `matching`, wildcards) -> block.

Fail-safe: when a relevant git command cannot be evaluated reliably (shell expansion in a decisive
position, unknown directory, not a repository yet, `xargs`, the hook itself crashing) it is
blocked with the reason. Everything else passes untouched.

Exit code 2 = block; the stderr message is shown to Claude. Docs: .claude/docs/git-safety.md.
Tests: .claude/tests/test_git_guard.py.
"""
import json
import os
import re
import shlex
import subprocess
import sys

PROTECTED = frozenset({"main", "master"})

COMMIT_LIKE = {"commit", "merge", "rebase", "cherry-pick", "revert", "am"}
# Options with which a commit-like command does not create a commit on the current branch.
NON_COMMITTING = {
    "merge": {"--abort", "--quit"},
    "rebase": {"--abort", "--quit", "--show-current-patch", "--edit-todo"},
    "cherry-pick": {"--abort", "--quit", "--skip", "-n", "--no-commit"},
    "revert": {"--abort", "--quit", "--skip", "-n", "--no-commit"},
    "am": {"--abort", "--quit", "--show-current-patch"},
    "commit": {"--dry-run"},
}
RELEVANT = COMMIT_LIKE | {"pull", "push", "update-ref", "symbolic-ref", "branch", "checkout", "switch"}
# Built-in subcommands that never need an alias lookup.
BUILTINS = RELEVANT | {
    "add", "bisect", "blame", "clone", "config", "describe", "diff", "fetch", "grep", "init", "log",
    "ls-files", "ls-remote", "mv", "remote", "reset", "restore", "rev-parse", "rm", "show",
    "stash", "status", "submodule", "symbolic-ref", "tag", "worktree", "help", "version", "gc",
    "reflog", "shortlog", "notes", "clean", "for-each-ref", "cat-file", "rev-list", "merge-base",
}

GIT_GLOBAL_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env",
                         "--exec-path", "--super-prefix", "--list-cmds"}
PUSH_WITH_VALUE = {"--repo", "-o", "--push-option", "--receive-pack", "--exec"}
COMMAND_PREFIXES = {"command", "builtin", "exec", "time", "nohup", "noglob", "!", "{", "}", "then",
                    "do", "else", "if", "elif", "while", "until", "fi", "done"}
SHELLS = {"sh", "bash", "zsh", "dash", "ksh"}
SEPARATORS = {";", "&&", "||", "|", "&", ";;", "|&"}
REDIRECTIONS = {">", ">>", "<", ">&", "<&", "&>", "&>>", ">|", "<>"}
EXPANSION = re.compile(r"[$`*?\[]")
HEREDOC = re.compile(r"(?<!<)<<(-?)\s*(['\"]?)([A-Za-z_]\w*)\2")
RELEVANT_TEXT = re.compile(r"\b(" + "|".join(sorted(RELEVANT)) + r")\b")
UNKNOWN = object()  # directory or branch that cannot be determined
MAX_DEPTH = 5


class Unsafe(Exception):
    """A relevant git command whose effect cannot be evaluated reliably."""


class Blocked(Exception):
    """A git command that would modify a protected branch."""


def is_protected(name):
    return name is not None and name is not UNKNOWN and short_branch(name).lower() in PROTECTED


def short_branch(ref):
    for prefix in ("refs/heads/", "heads/"):
        if ref.startswith(prefix):
            return ref[len(prefix):]
    return ref


def has_expansion(token):
    return bool(EXPANSION.search(token))


# --------------------------------------------------------------------------- git state


class Git:
    """Read-only queries against one repository (with the `-C/--git-dir/-c` of the invocation)."""

    def __init__(self, base_args):
        self.base = ["git", *base_args]

    def _run(self, *args):
        try:
            out = subprocess.run([*self.base, *args], capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError) as exc:
            raise Unsafe(f"git is not available ({exc})") from exc
        return out.returncode, out.stdout.strip()

    def toplevel(self):
        code, out = self._run("rev-parse", "--absolute-git-dir")
        if code != 0:
            raise Unsafe("the target directory is not a git repository (yet)")
        return out

    def current_branch(self):
        code, out = self._run("symbolic-ref", "--short", "-q", "HEAD")
        if code == 0:
            return out
        if code == 1:
            return None  # detached HEAD: not a branch
        raise Unsafe("the current branch cannot be determined")

    def config(self, key):
        code, out = self._run("config", "--get-all", key)
        return out.splitlines() if code == 0 and out else []

    def has_ref(self, ref):
        code, _ = self._run("rev-parse", "--verify", "--quiet", ref)
        return code == 0

    def remotes(self):
        code, out = self._run("remote")
        return out.splitlines() if code == 0 else []

    def remote_branch_exists(self, name):
        code, out = self._run("for-each-ref", "--format=%(refname)", f"refs/remotes/*/{name}")
        return code == 0 and bool(out)


# --------------------------------------------------------------------------- shell parsing


def strip_heredocs(command):
    """Removes heredoc bodies (commit messages often mention `git push`)."""
    out, pending = [], []
    for line in command.split("\n"):
        if pending:
            delimiter, dash = pending[0]
            if (line.lstrip("\t") if dash else line) == delimiter:
                pending.pop(0)
            continue
        out.append(line)
        pending.extend((m.group(3), m.group(1) == "-") for m in HEREDOC.finditer(line)
                       if outside_quotes(line[:m.start()]))
    return "\n".join(out)


def outside_quotes(prefix):
    """True when the end of `prefix` is not inside a '...' or "..." string."""
    single = double = False
    for i, char in enumerate(prefix):
        if char == "'" and not double:
            single = not single
        elif char == '"' and not single and (i == 0 or prefix[i - 1] != "\\"):
            double = not double
    return not single and not double


def tokenize(command):
    text = strip_heredocs(command).replace("\\\n", " ")
    # `2>&1`, `2>/dev/null`: drop the fd number so it is not taken for an argument.
    text = re.sub(r"(?<![\w-])\d+(?=[<>])", " ", text)
    lexer = shlex.shlex(text.replace("\n", " ; "), posix=True, punctuation_chars=";&|()<>")
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError as exc:
        raise Unsafe(f"the command cannot be parsed ({exc})") from exc
    return [part for token in tokens for part in split_operators(token)]


OPERATORS = sorted(SEPARATORS | REDIRECTIONS | {"(", ")"}, key=len, reverse=True)


def split_operators(token):
    """shlex glues adjacent punctuation (`);`, `)&&`): split it into shell operators."""
    if not token or set(token) - set(";&|()<>"):
        return [token]
    parts = []
    while token:
        operator = next((op for op in OPERATORS if token.startswith(op)), token[0])
        parts.append(operator)
        token = token[len(operator):]
    return parts


def substitutions(token):
    """Inner commands of `$(...)` inside one (quoted) token."""
    inner = []
    start = token.find("$(")
    while start != -1:
        depth, i = 1, start + 2
        while i < len(token) and depth:
            depth += {"(": 1, ")": -1}.get(token[i], 0)
            i += 1
        inner.append(token[start + 2:i - 1])
        start = token.find("$(", i)
    return inner


# --------------------------------------------------------------------------- evaluation


class Guard:
    def __init__(self, cwd, text=""):
        self.cwd = cwd
        self.text = text  # the whole command, for fail-safe decisions on input the guard cannot see
        self.branches = {}  # git dir -> simulated branch after a switch earlier in the command

    # ---- shell level

    def evaluate(self, command, depth=0):
        if depth > MAX_DEPTH:
            raise Unsafe("too deeply nested command")
        for inner in re.findall(r"`([^`]*)`", strip_heredocs(command)):  # shlex keeps backticks in words
            self.in_subshell(inner, depth)
        stack, words = [], []
        tokens = tokenize(command)
        i = 0
        while i <= len(tokens):
            token = tokens[i] if i < len(tokens) else ";"
            if token in SEPARATORS or token in ("(", ")"):
                self.simple_command(words, depth)
                words = []
                if token == "(":
                    stack.append(self.cwd)
                elif token == ")" and stack:
                    self.cwd = stack.pop()
            elif token in REDIRECTIONS:
                i += 1  # skip the redirection target
            else:
                words.append(token)
            i += 1

    def in_subshell(self, command, depth, cwd=None):
        """`$(...)`, backticks, `sh -c`, scripts, `!` aliases: their cd / switch do not leak out."""
        saved_cwd, saved_branches = self.cwd, dict(self.branches)
        if cwd is not None:
            self.cwd = cwd
        try:
            self.evaluate(command, depth + 1)
        finally:
            self.cwd, self.branches = saved_cwd, saved_branches

    def simple_command(self, words, depth):
        for word in words:
            for inner in substitutions(word):
                self.in_subshell(inner, depth)
        env = {}
        while words and (words[0] in COMMAND_PREFIXES or re.match(r"^[A-Za-z_]\w*=", words[0])):
            if "=" in words[0] and words[0] not in COMMAND_PREFIXES:
                key, value = words[0].split("=", 1)
                env[key] = value
            words = words[1:]
        if not words:
            return
        name = os.path.basename(words[0])
        args = words[1:]
        if name in ("env", "sudo", "nice", "timeout"):
            args = drop_wrapper_options(name, args, env)
            if args:
                self.simple_command(args, depth)
            return
        if name in SHELLS:
            self.shell(args, depth)
            return
        if name == "eval":
            self.evaluate(" ".join(args), depth + 1)
            return
        if name == "xargs":
            if "git" in args or any(os.path.basename(a) == "git" for a in args):
                raise Unsafe("`xargs git ...` gets its arguments from stdin")
            return
        if name in ("cd", "pushd"):
            self.change_directory(args)
            return
        if name == "gh" and ("checkout" in args or "--checkout" in args):
            self.branches[UNKNOWN] = UNKNOWN  # gh switched some branch: later commits are unknown
            return
        if name == "git":
            self.git(args, env, depth)

    def shell(self, args, depth):
        """`bash -lc "..."` is evaluated; a shell reading a script or stdin (heredoc, pipe) is not visible."""
        for i, arg in enumerate(args):
            if re.fullmatch(r"-[a-zA-Z]*c[a-zA-Z]*", arg):
                if i + 1 < len(args):
                    self.in_subshell(args[i + 1], depth)
                return
            if arg == "--" or not arg.startswith("-"):
                break
        positional = [a for a in args if not a.startswith("-")]
        if positional and isinstance(self.cwd, str) and not has_expansion(positional[0]):
            try:
                with open(os.path.join(self.cwd, os.path.expanduser(positional[0])), encoding="utf-8") as handle:
                    self.in_subshell(handle.read(), depth)
                return
            except (OSError, UnicodeDecodeError):
                pass
        if RELEVANT_TEXT.search(self.text):
            raise Unsafe("a shell reads commands from stdin or an unreadable script")

    def change_directory(self, args):
        args = [a for a in args if a not in ("-L", "-P", "--")]
        if not args:
            self.cwd = os.path.expanduser("~")
        elif args[0] == "-" or has_expansion(args[0]) or self.cwd is UNKNOWN:
            self.cwd = UNKNOWN
        else:
            self.cwd = os.path.normpath(os.path.join(self.cwd, os.path.expanduser(args[0])))

    # ---- git level

    def git(self, args, env, depth):
        directory, base, i = self.cwd, [], 0
        for key, option in (("GIT_DIR", "--git-dir"), ("GIT_WORK_TREE", "--work-tree")):
            if key in env:
                base += [option, env[key]]
        while i < len(args) and args[i].startswith("-"):
            option = args[i]
            if option in GIT_GLOBAL_WITH_VALUE and i + 1 < len(args):
                value = args[i + 1]
                if option == "-C":
                    directory = join_dir(directory, value)
                elif option in ("-c", "--git-dir", "--work-tree"):
                    base += [option, value]
                i += 2
            else:
                if option.startswith(("--git-dir=", "--work-tree=")):
                    base.append(option)
                i += 1
        if i >= len(args):
            return
        sub, rest = args[i], args[i + 1:]
        if has_expansion(sub):
            raise Unsafe(f"the git subcommand `{sub}` contains shell expansion")
        if sub not in BUILTINS:
            self.expand_alias(sub, rest, directory, base, depth)
            return
        if sub not in RELEVANT:
            return
        if directory is UNKNOWN or any(has_expansion(b) for b in base) or has_expansion(directory):
            raise Unsafe(f"`git {sub}` runs in a directory that cannot be determined")
        repo = Git(["-C", directory, *base])
        key = repo.toplevel()
        if sub in ("checkout", "switch"):
            self.switch(repo, key, sub, rest)
        elif sub in COMMIT_LIKE or sub == "pull":
            self.check_commit(repo, key, sub, rest)
        elif sub == "push":
            check_push(repo, self.branch(repo, key), rest)
        elif sub == "symbolic-ref":
            positional = without_message(rest)
            if len(positional) == 2 and positional[0] == "HEAD":  # `symbolic-ref HEAD <ref>` switches
                self.branches[key] = UNKNOWN if has_expansion(positional[1]) else short_branch(positional[1])
        elif sub == "update-ref":
            if "--stdin" in rest:
                raise Unsafe("`git update-ref --stdin` gets its refs from stdin")
            targets = without_message(rest)[:1]
            if targets and targets[0] in ("HEAD", "@") and "--no-deref" not in rest:
                targets = [f"refs/heads/{self.branch(repo, key)}"]  # moves the current branch
            check_ref_rewrite(sub, targets)
        elif sub == "branch":
            check_ref_rewrite(sub, rest)

    def expand_alias(self, sub, rest, directory, base, depth):
        if directory is UNKNOWN or has_expansion(directory):
            raise Unsafe(f"`git {sub}` may be an alias in a directory that cannot be determined")
        repo = Git(["-C", directory, *base])
        values = repo.config(f"alias.{sub}")
        if not values:
            return  # not an alias: an unknown command git will reject or a harmless one
        alias = values[-1]
        if alias.startswith("!"):  # git runs shell aliases at the repository's top level
            code, top = repo._run("rev-parse", "--show-toplevel")
            self.in_subshell(f"{alias[1:]} {shlex.join(rest)}", depth, top if code == 0 else directory)
        else:
            self.git(["-C", directory, *base, *shlex.split(alias), *rest], {}, depth + 1)

    def branch(self, repo, key):
        if key in self.branches:
            branch = self.branches[key]
        elif UNKNOWN in self.branches:
            branch = UNKNOWN
        else:
            branch = repo.current_branch()
        if branch is UNKNOWN:
            raise Unsafe("an earlier part of the command switches the branch in a way that cannot be followed")
        return branch

    def switch(self, repo, key, sub, rest):
        positional, i = [], 0
        while i < len(rest):
            arg = rest[i]
            if arg == "--":
                if not positional:  # `git checkout -- <paths>`: no branch switch
                    return
                break
            if arg in ("-b", "-B", "-c", "-C", "--orphan") and i + 1 < len(rest):
                name = rest[i + 1]
                if has_expansion(name):
                    raise Unsafe(f"`git {sub} {arg}` with a branch name from shell expansion")
                if arg in ("-B", "-C") and is_protected(name):
                    raise Blocked(f"`git {sub} {arg} {name}` resets a protected branch")
                self.branches[key] = name
                return
            if arg == "--detach" or (sub == "switch" and arg == "-d"):
                self.branches[key] = None
                return
            if not arg.startswith("-"):
                positional.append(arg)
            i += 1
        if not positional:
            return
        target = positional[0]
        if has_expansion(target) or target == "-":
            self.branches[key] = UNKNOWN
        elif repo.has_ref(f"refs/heads/{target}") or repo.remote_branch_exists(target):
            self.branches[key] = target
        elif sub == "switch":
            self.branches[key] = UNKNOWN  # no such branch: git fails, a later `;` command keeps running
        elif len(positional) > 1 or not repo.has_ref(f"{target}^{{commit}}"):
            return  # `git checkout <tree-ish> <paths>` or `git checkout <paths>`: files only
        else:
            self.branches[key] = None  # a commit: detached HEAD

    def check_commit(self, repo, key, sub, rest):
        if set(rest) & NON_COMMITTING.get(sub, set()):
            return
        branch = self.branch(repo, key)
        if is_protected(branch) and sub in ("merge", "pull") and is_remote_sync(repo, branch, sub, rest):
            return
        if sub == "rebase":
            positional, i = [], 0
            while i < len(rest):
                if rest[i] in ("--onto", "-s", "--strategy", "-X", "--strategy-option", "-x", "--exec"):
                    i += 2
                    continue
                if not rest[i].startswith("-"):
                    positional.append(rest[i])
                i += 1
            if len(positional) >= 2:  # `git rebase [--onto <x>] <upstream> <branch>` rebases <branch>
                branch = positional[1]
        if is_protected(branch):
            raise Blocked(f"`git {sub}` would create commits on the protected branch '{branch}'")


def is_remote_sync(repo, branch, sub, rest):
    """`merge --ff-only <remote>/<branch>` or `pull --ff-only`: main only catches up with its remote."""
    ff_only = "--ff-only" in rest or (sub == "pull" and first(repo.config("pull.ff")) == "only")
    if not ff_only or "--rebase" in rest or "-r" in rest:
        return False
    positional = [a for a in rest if not a.startswith("-")]
    if sub == "pull":  # `pull [--ff-only] [<remote> [<branch>]]`: only the branch itself
        return len(positional) < 2 or all(short_branch(p) == branch for p in positional[1:])
    remotes = set(repo.remotes())
    return len(positional) == 1 and (
        positional[0] in ("@{u}", "@{upstream}")
        or positional[0].removeprefix("refs/remotes/") in {f"{r}/{branch}" for r in remotes})


def without_message(rest):
    """Positional arguments of update-ref / symbolic-ref (skips the value of `-m <reason>`)."""
    positional, i = [], 0
    while i < len(rest):
        if rest[i] == "-m":
            i += 2
            continue
        if not rest[i].startswith("-"):
            positional.append(rest[i])
        i += 1
    return positional


def join_dir(directory, value):
    if directory is UNKNOWN or has_expansion(value):
        return UNKNOWN
    return os.path.normpath(os.path.join(directory, os.path.expanduser(value)))


def drop_wrapper_options(name, args, env):
    """Strips options of env/sudo/nice/timeout and returns the wrapped command."""
    i = 0
    while i < len(args):
        arg = args[i]
        if name == "env" and re.match(r"^[A-Za-z_]\w*=", arg):
            key, value = arg.split("=", 1)
            env[key] = value
        elif arg in ("-u", "-n", "-s", "-k", "--signal", "--kill-after", "-g", "-h") and i + 1 < len(args):
            i += 1
        elif not arg.startswith("-"):
            if name == "timeout":  # first positional is the duration
                return args[i + 1:]
            return args[i:]
        i += 1
    return []


def check_ref_rewrite(sub, rest):
    if sub == "update-ref":
        targets = rest  # already resolved by the caller
        if any(t.startswith("refs/heads/") and is_protected(t) for t in targets):
            raise Blocked(f"`git update-ref {targets[0]}` rewrites a protected branch")
        return
    moving = {"-f", "--force", "-m", "-M", "--move", "-c", "-C", "--copy"}
    if not set(rest) & moving:
        return
    positional = [a for a in rest if not a.startswith("-")]
    if set(rest) & {"-m", "-M", "--move", "-c", "-C", "--copy"}:
        target = positional[-1] if positional else None  # `branch -m [<old>] <new>`
    else:
        target = positional[0] if positional else None   # `branch -f <name> [<start>]`
    if target and has_expansion(target):
        raise Unsafe("`git branch` with a branch name from shell expansion")
    if is_protected(target):
        raise Blocked(f"`git branch {' '.join(rest)}` rewrites the protected branch '{target}'")


def check_push(repo, branch, rest):
    flags, positional, remote, i = set(), [], None, 0
    while i < len(rest):
        arg = rest[i]
        if arg in PUSH_WITH_VALUE and i + 1 < len(rest):
            if arg == "--repo":
                remote = rest[i + 1]
            i += 2
            continue
        if arg.startswith("--repo="):
            remote = arg.split("=", 1)[1]
        elif arg == "--":
            positional.extend(rest[i + 1:])
            break
        elif arg.startswith("--"):
            flags.add(arg.split("=", 1)[0])
        elif arg.startswith("-") and len(arg) > 1:
            flags.update(f"-{c}" for c in arg[1:])
        else:
            positional.append(arg)
        i += 1

    if flags & {"-n", "--dry-run"}:
        return
    if flags & {"--all", "--branches", "--mirror"}:
        raise Blocked("`git push --all/--branches/--mirror` also pushes main/master")
    if any(has_expansion(p) for p in positional) or (remote and has_expansion(remote)):
        raise Unsafe("the push target contains shell expansion")
    if positional and remote is None:
        remote, refspecs = positional[0], positional[1:]
    else:
        refspecs = positional
    if remote is None:
        remote = first(repo.config(f"branch.{branch}.pushRemote") if branch else [],
                       repo.config("remote.pushDefault"),
                       repo.config(f"branch.{branch}.remote") if branch else [], ["origin"])
    if first(repo.config(f"remote.{remote}.mirror"), ["false"]).lower() == "true":
        raise Blocked(f"remote '{remote}' is configured as a mirror (pushes every branch)")
    configured = repo.config(f"remote.{remote}.push")
    delete = bool(flags & {"-d", "--delete"})
    if "--prune" in flags and (not refspecs or any("*" in r for r in refspecs)):
        raise Blocked("`git push --prune` without explicit branch refspecs can delete main/master")

    if not refspecs and "--tags" in flags:
        targets = []  # `git push --tags` pushes only tags
    elif not refspecs:
        if configured:
            targets = [destination(repo, branch, spec, delete, []) for spec in configured]
        else:
            targets = implicit_targets(repo, branch, remote)
    else:
        targets = [destination(repo, branch, spec, delete, configured) for spec in refspecs]
    for spec_targets in targets:
        for target in spec_targets:
            if is_protected(target):
                raise Blocked(f"the push would update the protected branch '{short_branch(target)}' "
                              f"on '{remote}'")


def implicit_targets(repo, branch, remote):
    """Destinations of `git push [<remote>]` without refspecs (push.default)."""
    mode = first(repo.config("push.default"), ["simple"]).lower()
    if mode == "nothing" or branch is None:
        return []  # nothing to push / git refuses on a detached HEAD
    if mode == "matching":
        return [["main"]]  # pushes every branch that exists on both sides, main/master included
    if mode in ("upstream", "tracking"):
        return [repo.config(f"branch.{branch}.merge")]
    # simple / current: the branch of the same name (simple refuses a differently named upstream)
    return [[branch]]


def destination(repo, branch, spec, delete, configured):
    """Destination branches of one refspec.

    `<src>` without `:<dst>` updates the ref `remote.<name>.push` maps it to, otherwise the ref of
    the same name (git-push(1), <refspec>); the upstream is not used for explicit refspecs.
    """
    spec = spec.lstrip("+")
    if "*" in spec:
        return ["main"]  # a wildcard refspec can include main/master
    if delete:
        return [spec]
    if ":" in spec:
        source, target = spec.split(":", 1)
        if target:
            return [branch if target in ("HEAD", "@") else target]
        spec = source
    if spec in ("HEAD", "@"):
        if branch is None:
            return []  # detached HEAD without a destination: git refuses
        source = branch
    else:
        source = short_branch(spec)
    candidates = [source]
    for mapping in configured:  # conservative: any configured mapping may apply to <src>
        candidates += destination(repo, branch, mapping, False, [])
    return candidates


def first(*lists):
    for values in lists:
        if values:
            return values[-1]
    return None


# --------------------------------------------------------------------------- hook entry


def check(command, cwd):
    """Returns None when the command may run, else the reason it is blocked."""
    if "git" not in command and not re.search(r"\b(sh|bash|zsh|dash|ksh)\b", command):
        return None  # fast path: no git and no shell that could run a script containing git
    try:
        Guard(cwd, command).evaluate(command)
    except Blocked as exc:
        return str(exc)
    except Unsafe as exc:
        return f"cannot be evaluated reliably: {exc}"
    return None


def main():
    try:
        data = json.load(sys.stdin)
    except ValueError:
        print("BLOCKED by .claude/hooks/git_guard.py: unreadable hook input (fail-safe).", file=sys.stderr)
        return 2
    if not isinstance(data, dict) or not isinstance(data.get("tool_input") or {}, dict):
        print("BLOCKED by .claude/hooks/git_guard.py: unexpected hook input (fail-safe).", file=sys.stderr)
        return 2
    command = (data.get("tool_input") or {}).get("command") or ""
    cwd = data.get("cwd") or os.getcwd()
    try:
        reason = check(command, cwd)
    except Exception as exc:  # noqa: BLE001 — fail safe: a crash must not let a push through
        reason = None
        if re.search(r"\b(git|sh|bash|zsh|dash|ksh)\b", command):
            reason = f"the git guard failed to evaluate the command ({exc!r})"
    if reason:
        print(
            f"BLOCKED by .claude/hooks/git_guard.py: {reason}. Commits and pushes to main/master are "
            "forbidden (STRICT RULE 2): work on the ticket branch, or split the command so each git "
            "call has literal arguments (see .claude/docs/git-safety.md).",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
