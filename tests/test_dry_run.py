from contextlib import redirect_stdout
from io import StringIO
import unittest
from foreman.cli import _print_run_rows
from foreman.dry_run import ScriptHandler
from test_delivery import walk


class DryRunTests(unittest.TestCase):
    def test_paused_dry_transcript_keeps_tokens(self):
        output = StringIO()
        with redirect_stdout(output):
            _print_run_rows(walk(ScriptHandler(triage="needs_human")), dry_run=True)
        self.assertEqual(output.getvalue(), "triage claude-opus-5-5-high\ntokens 2 of 20000000\ndone needs_human\nWhat should Foreman know to continue?\n")

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
merge skipped (dry run): gh pr merge '<number>' --repo DoctorCRE/doctorcre-engine --auto --squash --match-head-commit '<sha>'
check exit 0
tokens 6 of 20000000
done verified
""")
