"""Consistency and scenario tests for the ticket orchestration config.

Run: python3 -m unittest discover -s .claude/tests -t .claude/tests   (needs PyYAML)

`plan()` is a reference implementation of the deterministic part of .claude/docs/orchestration.md
(scope §2, step resolution §3/§4, validation §5, correction limits §5). It is not used at runtime —
the orchestrator follows the docs; this file keeps the docs and the config consistent with each other.
"""
import os
import re
import unittest

import yaml

CLAUDE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKSPACE = os.path.dirname(CLAUDE_DIR)


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


def resolve_scope(ticket_repo, labels):
    """Returns (repo entries in scope, unknown service labels, fallback used)."""
    by_label = {r["label"].replace(" ", ""): r for r in REPOS["repositories"]}
    service_labels = [l for l in labels if l.replace(" ", "").startswith("service:")]
    scope = [by_label[l.replace(" ", "")] for l in service_labels if l.replace(" ", "") in by_label]
    unknown = [l for l in service_labels if l.replace(" ", "") not in by_label]
    if scope:
        return scope, unknown, False
    own = [r for r in REPOS["repositories"] if r["repo"] == ticket_repo]
    return own, unknown, bool(own)


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
    scope, unknown, fallback = resolve_scope(ticket_repo, labels)
    result = {"scope": [r["path"] for r in scope], "unknown_labels": unknown, "fallback": fallback,
              "steps": [], "skipped_layers": [], "validation": {}, "reviewers": []}
    if not scope:
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
                    "skills/clean-code-review/SKILL.md", "skills/marko/SKILL.md"):
            for ref in re.findall(r"`\.claude/([\w./-]+\.(?:md|yaml))", read(doc)):
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
