"""Consistency and scenario tests for the ticket orchestration config.

Run: python3 -m unittest discover -s .claude/tests -t .claude/tests   (needs PyYAML)

`plan()` is a reference implementation of the deterministic part of .claude/docs/orchestration.md
(scope §2, step resolution §3/§4, validation §5, correction limits §5). It is not used at runtime —
the orchestrator follows the docs; this file keeps the docs and the config consistent with each other.
"""
import os
import re
import sys
import unittest

import yaml

CLAUDE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKSPACE = os.path.dirname(CLAUDE_DIR)
sys.path.insert(0, os.path.join(CLAUDE_DIR, "lib"))
import repo_scope  # noqa: E402  — the runtime implementation of scope_policy


def load(rel):
    with open(os.path.join(CLAUDE_DIR, rel), encoding="utf-8") as f:
        return yaml.safe_load(f)


def read(rel):
    with open(os.path.join(CLAUDE_DIR, rel), encoding="utf-8") as f:
        return f.read()


REPOS = load("config/repos.yaml")
WORKFLOWS = load("config/workflows.yaml")
SCENARIOS = load("tests/scenarios.yaml")
AGENTS = {f[:-3] for f in os.listdir(os.path.join(CLAUDE_DIR, "agents")) if f.endswith(".md")}
VALIDATION_CATEGORIES = set(REPOS["categories"])
# STRICT RULE 1: nothing in a validation command may deploy or touch AWS state.
FORBIDDEN = re.compile(r"\b(aws|cdk|sam|serverless|sls|deploy|apply|destroy|plan|init|import)\b")


def layer_of(repo):
    return REPOS["stacks"][repo["stack"]]["layer"]


def owner_of(repo):
    return REPOS["stacks"][repo["stack"]]["owner"]


def resolve_check(stack, category):
    """Command, COVERED BY <x>, MANUAL: <how> or UNAVAILABLE — orchestration.md §5."""
    if category in REPOS["manual_checks"]:
        return "MANUAL: " + REPOS["manual_checks"][category]
    if category in stack.get("validation", {}):
        return stack["validation"][category]
    if category in stack.get("covered", {}):
        return "COVERED BY " + stack["covered"][category]
    return "UNAVAILABLE"


def plan(ticket_repo, labels, workflow_type):
    resolved = repo_scope.resolve_scope(ticket_repo, labels, REPOS["repositories"])
    scope = resolved["scope"]
    result = {"scope": [r["path"] for r in scope], "unknown_labels": resolved["unknown_labels"],
              "fallback": resolved["fallback"], "steps": [], "skipped_layers": [], "validation": {},
              "reviewers": []}
    if resolved["status"] == "BLOCKED":
        result["status"] = "BLOCKED"
        return result
    result["status"] = "READY"
    implemented_layers = set()
    for step in WORKFLOWS["workflows"][workflow_type]["steps"]:
        if isinstance(step, str):
            name, arg = step, None
        else:
            name = next(k for k in step if k != "mode")
            arg = step[name]
        if name == "implement":
            if arg == "docs":
                repos = scope
            elif arg == "any":
                repos = scope
            else:
                repos = [r for r in scope if layer_of(r) == arg]
            if not repos:
                result["skipped_layers"].append(arg)
                continue
            owners = []
            for r in repos:
                owner = "orchestrator" if arg == "docs" else owner_of(r)
                if owner not in owners:
                    owners.append(owner)
                implemented_layers.add(layer_of(r))
            for owner in owners:
                result["steps"].append({"step": "implement", "layer": arg, "owner": owner,
                                        "mode": step.get("mode") if isinstance(step, dict) else None})
        elif name == "integrate":
            if {"frontend", "backend"} <= implemented_layers:
                result["steps"].append({"step": "integrate", "owner": "orchestrator"})
        elif name == "contract_analysis":
            result["steps"].append({"step": "contract_analysis", "owner": "orchestrator"})
        elif name == "validate":
            for r in scope:
                stack = REPOS["stacks"][r["stack"]]
                result["validation"][r["path"]] = {c: resolve_check(stack, c) for c in arg}
        elif name == "review":
            result["reviewers"] = list(arg)
    return result


def run_validation(results, limit):
    """Correction loop of orchestration.md §5 for one check: returns (PASS | FAILED, corrections used).

    Simplifications: BLOCKED (fix unclear) vs FAILED is the orchestrator's judgment and is not modelled;
    `pre_existing` counts as PASS here, while the summary reports it separately as PRE-EXISTING.
    """
    corrections = 0
    for outcome in results:
        assert outcome in ("pass", "fail", "pre_existing"), outcome
        if outcome in ("pass", "pre_existing"):
            return "PASS", corrections
        if corrections == limit:
            return "FAILED", corrections
        corrections += 1
    return "FAILED", corrections


class ConfigConsistency(unittest.TestCase):
    def test_every_repo_has_a_known_stack(self):
        for repo in REPOS["repositories"]:
            with self.subTest(repo=repo["path"]):
                self.assertIn(repo["stack"], REPOS["stacks"])

    def test_every_owner_exists(self):
        for name, stack in REPOS["stacks"].items():
            with self.subTest(stack=name):
                self.assertTrue(stack["owner"] == "orchestrator" or stack["owner"] in AGENTS,
                                f"owner {stack['owner']} has no file in .claude/agents/")

    def test_required_agents_exist(self):
        self.assertTrue({"ticket-analyzer", "ui-agent", "lambda-agent", "clean-code-reviewer"} <= AGENTS)
        self.assertTrue(os.path.isfile(os.path.join(CLAUDE_DIR, "skills/marko/SKILL.md")))

    def test_no_validation_command_deploys(self):
        for name, stack in REPOS["stacks"].items():
            for category, command in stack.get("validation", {}).items():
                with self.subTest(stack=name, category=category):
                    self.assertIsNone(FORBIDDEN.search(command), command)

    def test_all_required_workflow_types_exist(self):
        self.assertEqual(set(WORKFLOWS["workflows"]), {
            "fullstack_feature", "frontend_feature", "backend_bugfix", "frontend_bugfix",
            "api_change", "refactoring", "infrastructure_change", "documentation_only"})

    def test_workflow_steps_use_known_vocabulary(self):
        layers = {s["layer"] for s in REPOS["stacks"].values()} | {"any", "docs"}
        for name, wf in WORKFLOWS["workflows"].items():
            for step in wf["steps"]:
                with self.subTest(workflow=name, step=step):
                    if isinstance(step, str):
                        self.assertIn(step, {"integrate", "contract_analysis"})
                    elif "implement" in step:
                        self.assertIn(step["implement"], layers)
                    elif "validate" in step:
                        self.assertTrue(set(step["validate"]) <= VALIDATION_CATEGORIES)
                    else:
                        self.assertTrue(set(step["review"]) <= {"marko", "clean-code-reviewer"})

    def test_stack_categories_are_known(self):
        self.assertTrue(set(REPOS["manual_checks"]) <= VALIDATION_CATEGORIES)
        for name, stack in REPOS["stacks"].items():
            with self.subTest(stack=name):
                self.assertTrue(set(stack.get("validation", {})) <= VALIDATION_CATEGORIES)
                self.assertTrue(set(stack.get("covered", {})) <= VALIDATION_CATEGORIES)
                for covered_by in stack.get("covered", {}).values():
                    self.assertIn(covered_by, stack["validation"])

    def test_manual_checks_never_resolve_to_unavailable(self):
        for name, stack in REPOS["stacks"].items():
            for category in ("contract", "docs"):
                with self.subTest(stack=name, category=category):
                    self.assertTrue(resolve_check(stack, category).startswith("MANUAL"))

    def test_limits_are_bounded(self):
        for key in ("correction_cycles", "marko_rounds", "final_review_rounds"):
            self.assertTrue(1 <= WORKFLOWS["limits"][key] <= 3, key)

    def test_docs_mention_every_workflow_type(self):
        for doc in ("docs/orchestration.md", "CLAUDE.md"):
            text = read(doc)
            for name in WORKFLOWS["workflows"]:
                with self.subTest(doc=doc, workflow=name):
                    self.assertIn(f"`{name}`", text)

    def test_claude_md_lists_every_agent(self):
        text = read("CLAUDE.md")
        for agent in AGENTS:
            with self.subTest(agent=agent):
                self.assertIn(f"`{agent}`", text)

    def test_referenced_config_files_exist(self):
        for doc in ("CLAUDE.md", "docs/orchestration.md", "skills/ticket-start/SKILL.md",
                    "agents/ticket-analyzer.md", "agents/ui-agent.md", "agents/lambda-agent.md",
                    "agents/clean-code-reviewer.md", "docs/agent-handoff.md",
                    "skills/clean-code-review/SKILL.md", "skills/marko/SKILL.md",
                    "docs/git-safety.md", "docs/langsmith.md"):
            for ref in re.findall(r"`(?:python3 )?\.claude/([\w./-]+\.(?:md|yaml|py))", read(doc)):
                with self.subTest(doc=doc, ref=ref):
                    self.assertTrue(os.path.isfile(os.path.join(CLAUDE_DIR, ref)), ref)

    def test_no_truncated_instructions(self):
        self.assertNotIn("≈", read("CLAUDE.md"))

    @unittest.skipUnless(os.path.isdir(os.path.join(WORKSPACE, "assistance")), "workspace not checked out")
    def test_stack_matches_checked_out_repos(self):
        markers = {"angular": "angular.json", "java-maven": "pom.xml", "static-html": "index.html",
                   "terraform": "modules", "python-lambda": "layers"}
        for repo in REPOS["repositories"]:
            path = os.path.join(WORKSPACE, repo["path"])
            if os.path.isdir(path):
                with self.subTest(repo=repo["path"]):
                    self.assertTrue(os.path.exists(os.path.join(path, markers[repo["stack"]])))


class ScopePolicy(unittest.TestCase):
    def test_runtime_parser_matches_yaml(self):
        parsed = repo_scope.load_repositories()
        expected = [{k: str(v) for k, v in r.items()} for r in REPOS["repositories"]]
        self.assertEqual(parsed, expected)

    def test_policy_in_config_is_the_implemented_one(self):
        self.assertEqual(REPOS["scope_policy"], repo_scope.IMPLEMENTED_POLICY)

    def test_labels_match_case_and_whitespace_insensitively(self):
        result = repo_scope.resolve_scope("eCharita/x", ["Service: Assistance", "service:assistance"])
        self.assertEqual(([r["path"] for r in result["scope"]], result["status"]), (["assistance"], "READY"))

    def test_duplicate_labels_are_reported_once(self):
        result = repo_scope.resolve_scope("eCharita/x", ["Service: Youth", "service:youth"])
        self.assertEqual(result["unknown_labels"], ["Service: Youth"])

    def test_explicit_approval_continues_with_mapped_repositories_only(self):
        labels = ["service:assistance", "service:youth-center"]
        result = repo_scope.resolve_scope("eCharita/assistance", labels, approved_without_unknown=True)
        self.assertEqual((result["status"], result["approved"]), ("READY", True))
        self.assertEqual([r["path"] for r in result["scope"]], ["assistance"])
        only_unknown = repo_scope.resolve_scope("eCharita/x", ["service:youth"], approved_without_unknown=True)
        self.assertEqual(only_unknown["status"], "BLOCKED")

    def test_unknown_label_never_expands_scope(self):
        result = repo_scope.resolve_scope("eCharita/assistance", ["service:assistance", "service:youth-center"])
        self.assertEqual(result["status"], "BLOCKED")
        self.assertNotIn("youth-center", [r["path"] for r in result["scope"]])

    def test_agents_and_skills_use_the_resolver(self):
        for doc in ("agents/ticket-analyzer.md", "CLAUDE.md", "docs/orchestration.md",
                    "skills/ticket-start/SKILL.md"):
            with self.subTest(doc=doc):
                self.assertIn("repo_scope.py", read(doc))
        self.assertNotIn("skip it, continue with the mapped ones", read("agents/ticket-analyzer.md"))


class StateModel(unittest.TestCase):
    STATES = {"ANALYZING", "PREPARING", "IMPLEMENTING", "VALIDATING", "REVIEWING", "BLOCKED", "COMPLETED", "FAILED"}

    def setUp(self):
        self.model = WORKFLOWS["states"]
        self.edges = self.model["transitions"]

    def test_states_are_the_required_ones(self):
        self.assertEqual(set(self.edges), self.STATES)
        for targets in self.edges.values():
            self.assertTrue(set(targets) <= self.STATES)

    def test_terminal_states_have_no_exit(self):
        self.assertEqual(set(self.model["terminal"]), {"COMPLETED", "BLOCKED", "FAILED"})
        for state in self.model["terminal"]:
            self.assertEqual(self.edges[state], [])

    def test_every_state_can_reach_completed_or_stop(self):
        for start in set(self.edges) - set(self.model["terminal"]):
            seen, todo = set(), [start]
            while todo:
                state = todo.pop()
                if state not in seen:
                    seen.add(state)
                    todo.extend(self.edges[state])
            with self.subTest(state=start):
                self.assertIn("COMPLETED", seen)
                self.assertIn("BLOCKED", seen)

    def test_failed_validation_returns_to_implementing(self):
        self.assertIn("IMPLEMENTING", self.edges["VALIDATING"])
        self.assertIn("IMPLEMENTING", self.edges["REVIEWING"])
        self.assertIn("FAILED", self.edges["VALIDATING"])

    def test_completed_only_after_review(self):
        sources = [s for s, targets in self.edges.items() if "COMPLETED" in targets]
        self.assertEqual(sources, ["REVIEWING"])

    def test_states_documented(self):
        text = read("docs/orchestration.md")
        for state in self.STATES:
            self.assertIn(state, text)

    def test_validation_loop_states(self):
        """Scenario E: fail -> IMPLEMENTING -> VALIDATING ... bounded, then FAILED."""
        limit = WORKFLOWS["limits"]["correction_cycles"]
        trace, state = ["VALIDATING"], "VALIDATING"
        status, corrections = run_validation(["fail"] * (limit + 1), limit)
        for _ in range(corrections):
            for nxt in ("IMPLEMENTING", "VALIDATING"):
                self.assertIn(nxt, self.edges[state])
                state = nxt
                trace.append(state)
        self.assertEqual(status, "FAILED")
        self.assertIn(status, self.edges[state])
        self.assertEqual(trace.count("IMPLEMENTING"), limit)


class Scenarios(unittest.TestCase):
    def test_scenarios(self):
        for name, sc in SCENARIOS["scenarios"].items():
            with self.subTest(scenario=name):
                result = plan(sc["ticket_repo"], sc["labels"], sc["workflow"])
                exp = sc["expect"]
                if exp.get("status") == "BLOCKED":
                    self.assertEqual(result["status"], "BLOCKED")
                    self.assertEqual(result["steps"], [])
                    continue
                self.assertEqual(result["status"], "READY")
                self.assertEqual(result["scope"], exp["scope"])
                implementers = [s["owner"] for s in result["steps"] if s["step"] == "implement"]
                self.assertEqual(implementers, exp["implementers"])
                invoked = set(implementers) | set(result["reviewers"])
                for agent in exp["not_invoked"]:
                    self.assertNotIn(agent, invoked)
                self.assertEqual(any(s["step"] == "integrate" for s in result["steps"]), exp["integrate"])
                if "first_step" in exp:
                    self.assertEqual(result["steps"][0]["step"], exp["first_step"])
                if "first_frontend_mode" in exp:
                    fe = next(s for s in result["steps"] if s.get("layer") == "frontend")
                    self.assertEqual(fe["mode"], exp["first_frontend_mode"])
                for layer in exp.get("skipped_layers", []):
                    self.assertIn(layer, result["skipped_layers"])
                if "unknown_labels" in exp:
                    self.assertEqual(result["unknown_labels"], exp["unknown_labels"])
                self.assertEqual(result["fallback"], exp.get("fallback", False))
                if "validation_categories" in exp:
                    for checks in result["validation"].values():
                        self.assertEqual(list(checks), exp["validation_categories"])
                for category, prefix in exp.get("validation_resolves", {}).items():
                    for checks in result["validation"].values():
                        self.assertTrue(checks[category].startswith(prefix), checks[category])
                for fragment in exp.get("validation_contains", []):
                    self.assertTrue(any(fragment in cmd for checks in result["validation"].values()
                                        for cmd in checks.values()), fragment)
                self.assertTrue(set(result["scope"]) >= {s for s in result["validation"]},
                                "validation outside scope")
                if implementers and sc["workflow"] != "documentation_only":
                    self.assertEqual(result["reviewers"][-1], "clean-code-reviewer")

    def test_full_stack_keeps_ui_first_order(self):
        steps = plan("eCharita/assistance", ["service:assistance", "service:PersonalAssistanceLambda"],
                     "fullstack_feature")["steps"]
        order = [s.get("layer") or s["step"] for s in steps]
        self.assertEqual(order, ["frontend", "backend", "integrate"])

    def test_failed_validation_is_bounded(self):
        limit = WORKFLOWS["limits"]["correction_cycles"]
        for name, run in SCENARIOS["validation_runs"].items():
            with self.subTest(run=name):
                self.assertEqual(run_validation(run["results"], limit), (run["status"], run["corrections"]))


if __name__ == "__main__":
    unittest.main()
