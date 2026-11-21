import ast
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import unittest

from langgraph.checkpoint.memory import InMemorySaver

from foreman.cli import _print_run_rows
from foreman.config import load_config
from foreman.delivery import DryRun, compile_delivery
from foreman.dry_run import ScriptHandler
from test_delivery import walk


class DryRunTests(unittest.TestCase):
    def test_graph_has_one_check_step_and_no_dry_run_branches(self):
        graph = compile_delivery(ScriptHandler(), load_config(), run=DryRun())
        self.assertIsInstance(graph.checkpointer, InMemorySaver)
        self.assertNotIn("verify_call", graph.get_graph().nodes)
        root = Path(__file__).resolve().parents[1] / "src/foreman"
        for path in root.rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "isinstance":
                    self.assertNotIn("DryRun", ast.unparse(node), str(path))

    def test_transcript_records_commands_without_running_them(self):
        output = StringIO()
        with redirect_stdout(output):
            _print_run_rows(walk(ScriptHandler()))
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
merge skipped (dry run): gh pr merge '<number>' --repo DoctorCRE/doctorcre-engine --auto --match-head-commit '<sha>'
check exit 0
tokens 6 of 20000000
done verified
""")
