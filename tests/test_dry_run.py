import ast
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import unittest
from unittest.mock import patch

from langgraph.checkpoint.memory import InMemorySaver
from foreman.cli import _print_run_rows
from foreman.delivery import compile_delivery
from foreman.dry_run import ScriptHandler
from test_delivery import walk


class DryRunTests(unittest.TestCase):
    def test_graph_has_one_check_step_and_no_dry_run_branches(self):
        with patch("foreman.delivery.compile_delivery", wraps=compile_delivery) as compile_graph:
            result = walk(ScriptHandler())
        self.assertIsInstance(compile_graph.call_args.kwargs["checkpointer"], InMemorySaver)
        self.assertEqual(result["merge"], "skipped")
        self.assertNotIn("merge_receipt", result)
        self.assertIn("pr_size", result)
        self.assertNotIn("commands", result)
        graph = compile_delivery(*compile_graph.call_args.args, **{**compile_graph.call_args.kwargs, "scratch": None})
        self.assertNotIn("verify_call", graph.get_graph().nodes)
        with self.assertRaisesRegex(ValueError, "thread_id"):
            graph.invoke({"repo": "org/repo", "check": "", "path": []})
        root = Path(__file__).resolve().parents[1] / "src/foreman"
        for path in root.rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "isinstance":
                    self.assertFalse(any(isinstance(arg, ast.Name) and arg.id == "run" for arg in ast.walk(node.args[0])), str(path))
    def test_paused_dry_transcript_keeps_tokens(self):
        output = StringIO()
        with redirect_stdout(output):
            _print_run_rows(walk(ScriptHandler(triage="needs_human")), dry_run=True)
        self.assertEqual(output.getvalue(), "triage claude-opus-5-5-high\ntokens 2 of 20000000\ndone needs_human\nWhat should Foreman know to continue?\n")

    def test_transcript_records_commands_without_running_them(self):
        commands = []
        output = StringIO()
        with redirect_stdout(output):
            _print_run_rows(walk(ScriptHandler(), commands=commands), commands=commands, dry_run=True)
        self.assertEqual(output.getvalue(), """dry run: git switch -c '<issue>-board-saves-a-move' 'refs/heads/<default-branch>'
dry run: git add --all
dry run: git commit -m '<issue>: Board saves a move

Co-Authored-By: gpt-6.1-sol-high <noreply@openai.com>'
dry run: git push --no-follow-tags origin 'refs/heads/<issue>-board-saves-a-move:refs/heads/<issue>-board-saves-a-move'
dry run: gh pr create --repo DoctorCRE/doctorcre-engine --base '<default-branch>' --head '<issue>-board-saves-a-move' --title 'Board saves a move' --body 'Closes #<issue>'
triage claude-opus-5-5-high
implement gpt-6.1-sol-high
publish
verify npm run check
review claude-opus-5-5-high risk low
merge skipped (dry run): gh pr merge '<number>' --repo DoctorCRE/doctorcre-engine --auto --squash --match-head-commit '<sha>'
check exit 0
tokens 6 of 20000000
done verified
""")
