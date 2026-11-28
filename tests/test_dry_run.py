import tests
import ast
import re
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
import unittest
from unittest.mock import patch

from langgraph.checkpoint.memory import InMemorySaver
from foreman.state import ReviewState, PlanState, ChecksState, RunState
from foreman.cli import _print_run_rows, main
from foreman.contract import ContractError, parse_contract
from foreman.delivery import compile_delivery, run_delivery
from foreman.dry_run import DRY_CONTRACT, ScriptHandler
from test_delivery import walk


def walk_contract(contract):
    return run_delivery(ScriptHandler(), repo="a/b", request="x", contract=contract)


def transcript(text):
    return re.sub(r"^(?:usage below is cumulative across all invocations|raw tokens|work tokens|attempt |input tokens|output tokens|cache_read tokens|cache_creation tokens).*\n", "", text, flags=re.M)


class DryRunTests(unittest.TestCase):
    def test_graph_has_one_check_step_and_no_dry_run_branches(self):
        with patch("foreman.delivery.compile_delivery", wraps=compile_delivery) as compile_graph:
            result = walk(ScriptHandler())
        self.assertIsInstance(compile_graph.call_args.kwargs["checkpointer"], InMemorySaver)
        self.assertEqual(result["merge"].status, "skipped")
        self.assertIsNone(result["merge"].receipt)
        self.assertNotIn("commands", result)
        graph = compile_delivery(*compile_graph.call_args.args, **{**compile_graph.call_args.kwargs, "scratch": None})
        self.assertNotIn("verify_call", graph.get_graph().nodes)
        with self.assertRaisesRegex(ValueError, "thread_id"):
            graph.invoke({"review": ReviewState(), "run": RunState(repo="org/repo"), "checks": ChecksState(check=""), "path": [], "plan": PlanState()})
        root = Path(__file__).resolve().parents[1] / "src/foreman"
        for path in root.rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "isinstance":
                    self.assertFalse(any(isinstance(arg, ast.Name) and arg.id == "run" for arg in ast.walk(node.args[0])), str(path))
    def test_cli_dry_run_takes_its_check_from_the_parsed_contract_fixture(self):
        output = StringIO()
        with redirect_stdout(output), patch("foreman.cli.run_delivery", wraps=run_delivery) as dry:
            self.assertEqual(main(["run", "delivery", "--dry-run", "--repo", "DoctorCRE/doctorcre-engine", "--request", "Add a field"]), 0)
        self.assertNotIn("contract", dry.call_args.kwargs)
        self.assertEqual(parse_contract(DRY_CONTRACT).check, "<check from .foreman/contract.yaml>")
        self.assertIn("check <check from .foreman/contract.yaml>\n", output.getvalue())
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            main(["run", "delivery", "--dry-run", "--repo", "a/b", "--request", "x", "--check", "exit 0"])
        # A malformed fixture fails in the parser, as a target repository's own file would.
        with self.assertRaisesRegex(ContractError, "verify_skill"):
            walk_contract(b"check: exit 0\n")

    def test_paused_dry_transcript_keeps_tokens(self):
        output = StringIO()
        with redirect_stdout(output):
            _print_run_rows(walk(ScriptHandler(triage="needs_human")), dry_run=True)
        self.assertRegex(transcript(output.getvalue()), "^claude spend \\$0\\.0 of \\$3\ntriage claude-opus-5-5-high\ndone needs_human\nWhich scope should this change cover\\?\n\n"
                         "Reply on the issue under the configured GitHub identity. Start with foreman answer [0-9a-f-]{71} and")

    def test_transcript_records_commands_without_running_them(self):
        commands = []
        output = StringIO()
        with redirect_stdout(output):
            _print_run_rows(walk(ScriptHandler(), commands=commands), commands=commands, dry_run=True)
        self.assertEqual(transcript(output.getvalue()), """claude spend $0.0 of $3
dry run: git switch -c '<issue>-board-saves-a-move-0a1b2c3d' 'refs/remotes/origin/<default-branch>'
dry run: git add --all
dry run: git commit -m '<issue>: Board saves a move

Co-Authored-By: gpt-6.1-sol-high <noreply@openai.com>'
dry run: git push --no-follow-tags origin 'refs/heads/<issue>-board-saves-a-move-0a1b2c3d:refs/heads/<issue>-board-saves-a-move-0a1b2c3d'
dry run: gh pr create --repo DoctorCRE/doctorcre-engine --base '<default-branch>' --head '<issue>-board-saves-a-move-0a1b2c3d' --title 'Board saves a move' --body 'Closes #<issue>'
dry run: gh pr comment 1 --repo DoctorCRE/doctorcre-engine --body 'Verify evidence.

- passed: A move is stored.
  Observed: The scripted drive used the change.
  Evidence: captures/scripted.txt

Tested head: <sha>.

<!-- foreman-verify:0a1b2c3d0a1b2c3d0a1b2c3d0a1b2c3d:1:<sha>:[redacted:Hex High Entropy String] -->'
triage claude-opus-5-5-high
spec_check grok-4.7-high
prepare
implement gpt-6.1-sol-high
check npm run check
mutate
commit
publish
verify claude-opus-5-5-high
review claude-opus-5-5-high
merge skipped (dry run): gh pr merge '<number>' --repo DoctorCRE/doctorcre-engine --auto --squash --match-head-commit '<sha>'
done merge_pending
PR #1 at <sha>: dry run, so Foreman did not arm it. Waiting for GitHub to report the merge.
""")
