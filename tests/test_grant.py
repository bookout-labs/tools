import tests

import json
import os
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import unittest

from unittest.mock import patch

from foreman.adapters import Outcome
from foreman.config import ModelSpec
from foreman.delivery import LiveRun, run_delivery
from foreman.dispatch import FailoverDispatcher
from foreman.dry_run import ScriptHandler
from foreman.runner import checkpoint_state, run_live_delivery
from foreman.skill_rules import PACKAGE_SKILLS, export_skills, stage_methods
from foreman.worker import CliWorker
from skill_fixture import skill_fixture
from test_adapters import fixture_process
from test_delivery import EMPTY_SLICE
from test_runner import FakeGitHub, FakeWorkspace

CHECK = "python -m unittest discover -s tests && pyright && sh evals/eval.sh"


class GrantTest(unittest.TestCase):
    def test_checks_are_canonical_commands(self):
        from foreman.grant import CheckCommand, parse_check

        self.assertEqual(parse_check(CHECK), (
            CheckCommand(("python", "-m", "unittest", "discover", "-s", "tests")),
            CheckCommand(("pyright",)), CheckCommand(("sh", "evals/eval.sh")),
        ))
        self.assertEqual(parse_check(""), ())
        self.assertEqual(parse_check("  python   'file with spaces.py'  "),
                         (CheckCommand(("python", "file with spaces.py")),))

    def test_shell_programs_cannot_be_granted_as_commands(self):
        from foreman.grant import UngrantableCheck, parse_check

        for command in ("a; b", "a | b", "a || b", "a > f", "$(x)", "a `x`", "a\nb", "\n", "\r",
                        "a & b", "echo '&&' pyright", "a < f", "a &&", "'' && a", "&& a", "a && && b", "a 'unterminated"):
            with self.subTest(command=command), self.assertRaises(UngrantableCheck):
                parse_check(command)

    def test_both_grants_survive_json_checkpoint_round_trip(self):
        from foreman.grant import Editing, ReadOnly, grant_from_record, parse_check

        for grant in (ReadOnly((Path("/pinned/skills"),)),
                      Editing((Path("/pinned/skills"), Path("/package/skills")), parse_check(CHECK))):
            with self.subTest(grant=grant):
                record = json.loads(json.dumps(grant.record()))
                self.assertEqual(grant_from_record(record), grant)
                state = checkpoint_state({"repo": "a/b", "check": CHECK, "path": [], "token_cap": 100, "dispatch_plan": {
                    "models": [], "instruction": "work", "methods": [], "target": None, "scope": None,
                    "grant": record,
                }})
                self.assertEqual(state["dispatch_plan"]["grant"], record)
        self.assertIsNotNone(checkpoint_state({"repo": "a/b", "check": CHECK, "path": [], "token_cap": 100, "dispatch_plan": {
            "models": [], "instruction": "legacy", "methods": [], "target": None, "scope": None,
        }}))

    def test_ungrantable_live_check_is_rejected_before_graph_compilation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("foreman.runner.compile_delivery", side_effect=AssertionError("graph compiled")):
                with self.assertRaises(ValueError):
                    run_live_delivery(repo="a/b", issue=1, check="a; b", checkpoint=root / "run.sqlite",
                                      run=LiveRun(FakeGitHub(), FakeWorkspace(root)))

    def test_every_named_method_is_readable_under_an_export_or_package_root(self):
        for work_kind in ("bug_fix", "perf_issue"):
            with self.subTest(work_kind=work_kind), skill_fixture() as (config, scratch):
                root = export_skills(config.pstack, scratch)
                state = {"issue_kind": "slice", "work_kind": work_kind,
                         "slice": {**EMPTY_SLICE, "stateful": True, "screen": "screen.md",
                                   "properties": [{"name": "Unique", "constraint": "unique_id"}]}}
                for stage in ("triage", "research", "spec", "implement", "review"):
                    methods = stage_methods(stage, state, Path(os.path.relpath(root)))
                    paths = [Path(path) for path in re.findall(r"(?:Read |of )(/\S+\.md)", methods.text)]
                    self.assertEqual(len(paths), len(methods.methods), stage)
                    self.assertTrue(paths, stage)
                    self.assertNotIn(scratch.resolve(), methods.read_roots)
                    self.assertTrue(set(methods.read_roots) <= {root.resolve(), PACKAGE_SKILLS.resolve()})
                    for path in paths:
                        self.assertTrue(path.is_file(), path)
                        self.assertTrue(any(path.is_relative_to(allowed) for allowed in methods.read_roots), path)

    def test_graph_sends_stage_grants_through_review_fix_and_quota_fallback(self):
        from foreman.grant import Editing, ReadOnly, parse_check

        received = []

        class RecordingScript(ScriptHandler):
            def __call__(self, node, model, state, instruction, grant):
                if node != "verify":
                    received.append((node, model.name, state.get("review"), grant))
                if node == "implement" and state.get("review") == "changes" and model.name == "gpt-6.1-sol-high":
                    return Outcome("quota")
                return super().__call__(node, model, state, instruction, grant)

        with skill_fixture() as (config, scratch):
            result = run_delivery(FailoverDispatcher(RecordingScript(triage="spec", reviews=["changes", "approve"])),
                                  repo="a/b", request="Save", check=CHECK, slice=EMPTY_SLICE,
                                  config=config, scratch=scratch)
            self.assertEqual(result["stop"], "verified")
        with skill_fixture() as (config, scratch):
            result = run_delivery(FailoverDispatcher(RecordingScript(triage="question")), repo="a/b", request="Explain",
                                  check=CHECK, slice=EMPTY_SLICE, config=config, scratch=scratch)
            self.assertEqual(result["stop"], "answered", result)
        readonly_stages = set()
        implementations = []
        for stage, model, review, grant in received:
            self.assertTrue(grant.read_roots)
            if stage == "implement":
                self.assertIsInstance(grant, Editing)
                self.assertEqual(grant.checks, parse_check(CHECK))
                implementations.append((model, review))
            else:
                self.assertIsInstance(grant, ReadOnly)
                readonly_stages.add(stage)
        self.assertEqual(readonly_stages, {"triage", "research", "spec", "review"})
        self.assertEqual(implementations, [("gpt-6.1-sol-high", None),
                                          ("gpt-6.1-sol-high", "changes"),
                                          ("gpt-6.1-terra-high", "changes")])

    def test_cli_worker_uses_the_supplied_grant_rather_than_the_stage_name(self):
        from foreman.grant import Editing, ReadOnly

        for model in ("gpt-6.1-sol-high", "claude-opus-5-5-high", "grok-4.7-high"):
            for stage, grant, codex_mode, native_mode in (
                ("implement", ReadOnly(), "read-only", "plan"),
                ("review", Editing(), "workspace-write", "acceptEdits"),
            ):
                with self.subTest(model=model, stage=stage):
                    commands = []

                    def run(args, cwd, env):
                        commands.append(args)
                        return fixture_process(args, "success", 0)

                    CliWorker(Path("."), run)(stage, ModelSpec.parse(model), {}, "Work", grant)
                    argv = commands[0]
                    flag, expected = ("-s", codex_mode) if argv[0] == "codex" else ("--permission-mode", native_mode)
                    self.assertEqual(argv[argv.index(flag) + 1], expected)
