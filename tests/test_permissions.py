import tests

import json
from pathlib import Path
import unittest

from foreman.adapters import Call, Outcome
from foreman.adapters.claude import ClaudeAdapter
from foreman.config import ModelSpec, NodeConfig, load_config
from foreman.delivery import run_delivery
from foreman.dry_run import ScriptHandler, SCRIPTED_USAGE
from foreman.skill_rules import PACKAGE_SKILLS, SKILL_RULES, export_skills, stage_methods
from foreman.worker import CliWorker
from skill_fixture import skill_fixture
from test_adapters import fixture_process
from test_runner import EMPTY_SLICE


class PermissionsTest(unittest.TestCase):
    def test_method_paths_are_covered_by_precise_roots(self):
        with skill_fixture() as (config, scratch):
            root = export_skills(config.pstack, scratch)
            for stage in {rule.stage for rule in SKILL_RULES}:
                for kind in ("feature", "bug_fix", "perf_issue"):
                    state = {"work_kind": kind, "issue_kind": "slice", "slice": {**EMPTY_SLICE, "stateful": True, "screen": "UI", "properties": [{"name": "rule", "constraint": "unique"}]}}
                    text, methods, roots = stage_methods(stage, state, root)
                    self.assertEqual(len(roots), len(set(roots)))
                    self.assertTrue(set(roots) <= {str(root.resolve()), str(PACKAGE_SKILLS.resolve())})
                    self.assertEqual(len(text.split('\n\n')), len(methods))
                    for instruction in text.split('\n\n'):
                        path = instruction.split(' of ')[-1].split(' as the method.')[0] if ' of ' in instruction else instruction.removeprefix('Read ').split(' in full')[0]
                        self.assertTrue(Path(path).is_file())
                        self.assertTrue(any(Path(path).is_relative_to(Path(r)) for r in roots))

    def test_first_fix_and_quota_fallback_receive_the_bound_grant(self):
        for fallback in (False, True):
            config = load_config()
            names = (["gpt-6.1-sol-high", "gpt-6.1-terra-high"] if fallback else []) + ["claude-opus-5-5-high"]
            config.nodes["implement"] = NodeConfig("implementer", tuple(ModelSpec.parse(n) for n in names))
            with skill_fixture(config) as (config, scratch):
                calls = []
                script = ScriptHandler(reviews=["changes", "approve"])

                def runner(args, cwd, env):
                    calls.append(args)
                    return fixture_process(args, "success", 0)

                worker = CliWorker(scratch, runner)

                def handler(node, model, state, instruction):
                    if node != "implement":
                        return script(node, model, state, instruction)
                    if model.family == "gpt":
                        return Outcome("quota", usage=SCRIPTED_USAGE)
                    worker(node, model, state, instruction)
                    roots = state["dispatch_plan"]["read_roots"]
                    argv = calls[-1]
                    self.assertEqual([argv[i + 1] for i, a in enumerate(argv) if a == "--add-dir"][:-1], roots)
                    permissions = json.loads(argv[argv.index("--settings") + 1])["permissions"]
                    self.assertEqual(permissions["allow"], ["Bash(sh check.sh)", "Edit", "Write"])
                    return script(node, model, state, instruction)

                result = run_delivery(handler, repo="Example/Engine", request="Change", check="sh check.sh",
                                      config=config, slice=EMPTY_SLICE, scratch=scratch)
                self.assertEqual(result["stop"], "verified")
                self.assertEqual(len(calls), 2)

    def test_readonly_has_no_edit_tools_or_allow_rules(self):
        adapter = ClaudeAdapter(ModelSpec.parse("claude-opus-5-5-high"))
        args = adapter.command(Call("prompt", "{}", False, Path("scratch")))
        self.assertEqual(args[args.index("--tools") + 1], "Read,Glob,Grep,Bash")
        self.assertEqual(json.loads(args[args.index("--settings") + 1])["permissions"]["allow"], [])
        self.assertIn("--restricted", args)
        self.assertIn("--strict-mcp-config", args)
        self.assertEqual(args[args.index("--permission-mode") + 1], "dontAsk")

    def test_review_grants_only_the_bound_scope_command(self):
        args_seen = []
        def runner(args, cwd, env):
            args_seen.append(args)
            return fixture_process(args, "success", 0)
        plan = {"models": [], "instruction": "review", "methods": [], "read_roots": [],
                "target": None, "scope": {"command": "git diff --no-color abc...def --", "inline": []}}
        CliWorker(Path.cwd(), runner)("review", ModelSpec.parse("claude-opus-5-5-high"),
                                     {"check": "sh check.sh", "dispatch_plan": plan}, "review")
        args = args_seen[0]
        self.assertEqual(json.loads(args[args.index("--settings") + 1])["permissions"]["allow"],
                         ["Bash(git diff --no-color abc...def --)"])

    def test_captured_permissions_and_effects(self):
        folder = Path(__file__).parent / "fixtures/claude/permissions"
        for mode in ("edit", "inherited", "compound", "readonly"):
            capture = json.loads((folder / f"{mode}.json").read_text())
            self.assertEqual(capture["returncode"], 0)
            files = capture["files"]
            self.assertEqual(files["methods/method.md"], "method original\n")
            self.assertNotIn("repo/x", files)
            self.assertNotIn("repo/f", files)
            denied = capture["stdout"]["permission_denials"]
            commands = {d["tool_input"].get("command") for d in denied if d["tool_name"] == "Bash"}
            self.assertIn("touch x", commands)
            if mode == "readonly":
                self.assertEqual(files["repo/source.py"], "value = 1\n")
                self.assertNotIn("scratch/scratch.txt", files)
                self.assertIn("git diff --output=f", commands)
                self.assertFalse(any(c.startswith("git diff --no-color") for c in commands))
            else:
                self.assertEqual(files["repo/source.py"], "value = 2\n")
                self.assertEqual(files["scratch/scratch.txt"], "scratch")
                self.assertIn('name = "after"', files["repo/pyproject.toml"])
                self.assertEqual(files["repo/.github/workflows/ci.yml"], "name: after\n")
                self.assertTrue({"git commit -m x", "gh pr list", "python -c 1"} <= commands)
                self.assertTrue(any(d["tool_name"] == "Edit" and "methods/" in d["tool_input"]["file_path"] for d in denied))
                for marker in (("first", "second") if mode == "compound" else ("checked",)):
                    self.assertIn(f"repo/{marker}", files)
            content = [c for row in capture["transcript"] for c in row["message"]["content"] if isinstance(c, dict)]
            reads = [c for c in content if c.get("type") == "tool_use" and c.get("name") == "Read" and c["input"]["file_path"].endswith("methods/method.md")]
            self.assertTrue(reads)
            self.assertTrue(any(c.get("type") == "tool_result" and c.get("tool_use_id") == reads[0]["id"] and not c.get("is_error") for c in content))
