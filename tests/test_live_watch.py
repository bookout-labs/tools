import tests

import contextlib
import io
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from langgraph.checkpoint.sqlite import SqliteSaver

from foreman.state import checkpoint_serializer
from foreman.adapters import Outcome, Usage
from foreman.cli import main
from foreman.config import load_config
from foreman.delivery import LiveRun, compile_delivery
from foreman.github import Forge, ForgeError, LiveForge
from foreman.runner import run_live_delivery
from foreman.skill_rules import SkillMissing
from foreman.workspace import WorkspaceError
from foreman.dry_run import ScriptHandler, SCRIPTED_USAGE
from foreman.worker import CliWorker
from skill_fixture import fixture_skills
from test_runner import EMPTY_SLICE, FakeGitHub, FakeWorkspace, slice_body, worker_reply


class LiveWatchTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.checkpoint = self.root / 'runs.sqlite3'
        self.fake = FakeGitHub()
        self.forge = Mock(spec=Forge, wraps=self.fake)
        self.run = LiveRun(self.forge, FakeWorkspace(self.root), skills=fixture_skills)
        self.flags = ['run', 'delivery', '--repo', 'Example/Engine', '--issue', '8', '--check', 'exit 0']

    def invoke(self, **kwargs):
        return run_live_delivery(repo='Example/Engine', issue=8, check='exit 0',
                                 checkpoint=self.checkpoint, run=self.run, **({'dispatcher': lambda *args: Outcome('failed', detail='bad output', usage=SCRIPTED_USAGE)} | kwargs))

    def stored(self):
        with SqliteSaver.from_conn_string(str(self.checkpoint)) as saver:
            saver.serde = checkpoint_serializer()
            return saver.get({'configurable': {'thread_id': 'example/engine#8'}})['channel_values']

    def test_reopen_records_once_and_reports_before_filing(self):
        events = []
        result = self.invoke(report=lambda result: events.append(result['stop']), log=events.append)
        self.assertEqual(events, ['invalid_output', 'watch filed #101'])
        self.assertEqual(result['watch'], {'issue': 101, 'action': 'filed'})
        self.assertEqual(self.stored()['watch'], result['watch'])
        self.assertEqual(self.invoke(), result)
        self.forge.open_issues.assert_called_once()
        self.forge.create_issue.assert_called_once()
        self.forge.comment_issue.assert_not_called()

    def test_crash_after_create_before_record_dedupes_from_original_snapshot(self):
        def crash(*args, **kwargs):
            self.fake.create_issue(*args, **kwargs)
            raise RuntimeError('crash before checkpoint write')
        self.forge.create_issue.side_effect = crash
        with self.assertRaises(RuntimeError):
            self.invoke()
        self.assertIsNone(self.stored()['watch'])
        self.forge.create_issue.side_effect = None
        result = self.invoke()
        self.assertEqual(result['watch'], {'issue': 101, 'action': 'filed'})
        self.assertEqual(self.stored()['watch'], result['watch'])
        self.forge.create_issue.assert_called_once()
        self.forge.comment_issue.assert_not_called()

    def test_pregraph_skill_failure_is_durable_and_files_skill_rules(self):
        missing = Mock(side_effect=SkillMissing(load_config().pstack))
        self.run = LiveRun(self.forge, self.run.workspace, skills=missing)
        first = self.invoke()
        stored = self.stored()
        self.assertEqual((stored['run'].issue, stored['run'].workspace, stored['run'].repo, stored['checks'].check, stored['run'].token_cap),
                         (8, str(self.root.resolve()), 'Example/Engine', 'exit 0', 20_000_000))
        self.assertEqual(stored['pstack_pin'], first['pstack_pin'])
        self.assertIn('File to change: src/foreman/skill_rules.py', self.fake.created[0][1])
        self.assertEqual(self.invoke(), first)
        missing.assert_called_once()
        self.forge.load_issue.assert_not_called()
        self.forge.create_issue.assert_called_once()

    def test_resumed_git_failure_adds_one_comment_per_new_halt(self):
        workspace = FakeWorkspace(self.root)
        workspace.create_branch = Mock(side_effect=WorkspaceError('git failed'))
        self.run = LiveRun(self.forge, workspace, skills=fixture_skills)
        dispatcher = CliWorker(self.root, lambda args, *unused: worker_reply(args, {"triage": "implement"}))
        first = self.invoke(dispatcher=dispatcher)
        self.assertEqual(first['halt'], 'git_failed')
        second = self.invoke(dispatcher=dispatcher)
        self.assertEqual(second['watch'], {'issue': 101, 'action': 'commented'})
        self.assertEqual(self.stored()['watch'], second['watch'])
        self.forge.create_issue.assert_called_once()
        self.forge.comment_issue.assert_called_once()
        markers = []
        for body in (self.fake.created[0][1], self.fake.comments['bookout-labs/tools', 101][0]):
            lines = [line for line in body.splitlines() if line.startswith('<!-- foreman-watch-run: ')]
            self.assertEqual(len(lines), 1)
            markers.append(lines[0])
        self.assertNotEqual(*markers)

    def test_cli_watch_target_and_validation(self):
        for target in (None, 'Other/Watch'):
            self.checkpoint = self.root / f'{target is None}.sqlite3'
            self.forge.open_issues.return_value = []
            flags = self.flags + (['--watch-repo', target] if target else [])
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(flags, checkpoint=self.checkpoint, live_run=self.run, worker_runner=lambda *args: subprocess.CompletedProcess([], 0, 'invalid', '')), 1)
            self.assertEqual(self.forge.create_issue.call_args.args[0], target or 'bookout-labs/tools')
        for target in ('repo', 'owner/repo/extra', '/repo', 'owner/'):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                main(self.flags + ['--watch-repo', target])
            self.assertEqual(error.exception.code, 2)

    def test_skipped_halt_and_dry_run_never_call_forge(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as log:
            self.fake.body = 'Issue body'
            self.fake.labels = []
            code = main(self.flags, checkpoint=self.checkpoint, live_run=self.run,
                        worker_runner=Mock(side_effect=FileNotFoundError('missing CLI')))
        self.assertEqual(code, 1)
        self.assertEqual(log.getvalue(), 'watch skipped cli_missing\n')
        self.forge.open_issues.assert_not_called()
        self.forge.create_issue.assert_not_called()
        self.forge.comment_issue.assert_not_called()
        self.forge.reset_mock()
        for exit_code, expected in ((0, 0), (1, 1)):
            with patch('foreman.cli.LiveForge', side_effect=AssertionError('dry Forge')), patch('foreman.delivery.DryRun.run', return_value=exit_code), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(['run', 'delivery', '--repo', 'Example/Engine', '--request', 'test', '--dry-run'], live_run=self.run), expected)
        self.assertEqual(self.forge.mock_calls, [])

    def test_blocked_run_reports_once_without_watching(self):
        self.fake.create_issue('Example/Engine', 'blocker', '', [])
        self.fake.dependencies[8] = [101]
        self.fake.body = slice_body({**EMPTY_SLICE, 'depends_on': ['blocker'], 'blocker_numbers': [101]})
        reports = []
        result = self.invoke(report=reports.append)
        self.assertEqual([report['stop'] for report in reports], ['blocked'])
        self.assertEqual(result['stop'], 'blocked')
        self.forge.open_issues.assert_not_called()
        self.forge.create_issue.assert_not_called()
        self.fake.closed.add(101)
        self.fake.merged.add(101)
        def dispatch(stage, *args):
            return (Outcome('ran', {'triage': 'implement', 'human_question': None, 'work_kind': 'feature', 'slice': None},
                            usage=Usage(cache_read_input_tokens=0, cache_creation_input_tokens=0, input_tokens=1, output_tokens=1)) if stage == 'triage' else Outcome('quota', usage=SCRIPTED_USAGE))
        quota = dispatch
        paused = self.invoke(dispatcher=quota)
        self.assertEqual(paused['stop'], 'quota_exhausted')
        again = self.invoke(dispatcher=quota)
        self.assertEqual(again['stop'], 'quota_exhausted')
        self.assertGreater(len(again['attempts']), len(paused['attempts']))
        self.assertEqual([attempt['stage'] for attempt in again['attempts']].count('triage'), 1)
        self.assertEqual(self.forge.load_issue.call_count, 2)

    def test_skill_failure_preserves_resumable_checkpoints(self):
        for stop in ('git_failed', 'quota_exhausted'):
            with self.subTest(stop=stop):
                self.checkpoint = self.root / f'{stop}.sqlite3'
                workspace = FakeWorkspace(self.root)
                workspace.create_branch = Mock(side_effect=WorkspaceError('git failed'))
                self.run = LiveRun(self.forge, workspace, skills=fixture_skills)
                def dispatch(stage, *args):
                    if stop == 'quota_exhausted':
                        return Outcome('quota', usage=SCRIPTED_USAGE)
                    return Outcome('ran', {'triage': 'implement', 'human_question': None, 'work_kind': 'feature', 'slice': None},
                                   usage=Usage(cache_read_input_tokens=0, cache_creation_input_tokens=0, input_tokens=1, output_tokens=1))
                dispatcher = dispatch
                self.assertEqual(self.invoke(dispatcher=dispatcher)['stop'], stop)
                before = self.stored()
                self.run = LiveRun(self.forge, workspace, skills=Mock(side_effect=SkillMissing(load_config().pstack)))
                reports = []
                failed = self.invoke(dispatcher=dispatcher, report=reports.append)
                self.assertEqual([result['stop'] for result in reports], ['skill_missing'])
                self.assertIsNone(failed['watch'])
                self.assertEqual(self.stored(), before)
                self.run = LiveRun(self.forge, workspace, skills=fixture_skills)
                self.assertEqual(self.invoke(dispatcher=dispatcher)['stop'], stop)

    def test_live_forge_receives_redacted_halt_on_issue_and_repeat_comment(self):
        secret = 'ghp_' + 'a' * 36
        def gh(args, **kwargs):
            endpoint = args[2]
            fields = dict(arg.split('=', 1) for arg in args if '=' in arg and not arg.startswith('repos/'))
            if args[1] == 'issue':
                result = {'title': 'Issue', 'body': self.fake.body, 'labels': [{'name': 'slice'}]}
            elif args[1] == 'repo':
                result = {'defaultBranchRef': {'name': 'main'}}
            elif '--method' in args:
                if endpoint.endswith('/comments'):
                    self.fake.comment_issue('bookout-labs/tools', 101, fields['body'])
                    result = {}
                else:
                    result = {'number': self.fake.create_issue('bookout-labs/tools', fields['title'], fields['body'], [])}
            elif '/comments?' in endpoint:
                result = [[{'body': body} for body in self.fake.issue_comments('bookout-labs/tools', 101)]]
            else:
                result = [[issue.model_dump() for issue in self.fake.open_issues('bookout-labs/tools')]]
            return subprocess.CompletedProcess(args, 0, json.dumps(result), '')
        workspace = FakeWorkspace(self.root)
        workspace.create_branch = Mock(side_effect=WorkspaceError('#48 ' + secret))
        self.run = LiveRun(LiveForge(), workspace, skills=fixture_skills)
        dispatcher = CliWorker(self.root, lambda args, *unused: worker_reply(args, {'triage': 'implement'}))
        real_run = subprocess.run
        fake_gh = lambda args, **kwargs: gh(args, **kwargs) if args[0] == 'gh' else real_run(args, **kwargs)
        with patch('foreman.github.subprocess.run', side_effect=fake_gh):
            self.invoke(dispatcher=dispatcher)
            self.invoke(dispatcher=dispatcher)
        for body in (self.fake.created[0][1], self.fake.comments['bookout-labs/tools', 101][0]):
            self.assertNotIn(secret, body)
            self.assertIn('#48 [redacted:GitHub Token]', body)

    def test_watch_errors_keep_exit_codes_are_redacted_ordered_and_retried(self):
        secret = 'ghp_' + 'a' * 36
        for stop, code in (('invalid_output', 1), ('git_failed', 75), ('needs_human', 3)):
            self.checkpoint = self.root / f'{stop}.sqlite3'
            if code == 75:
                self.run.workspace.create_branch = Mock(side_effect=WorkspaceError('git failed'))
                dispatcher = CliWorker(self.root, lambda args, *unused: worker_reply(args, {'triage': 'implement'}))
                self.invoke(dispatcher=dispatcher)
            else:
                self.invoke()
            with SqliteSaver.from_conn_string(str(self.checkpoint)) as saver:
                saver.serde = checkpoint_serializer()
                graph = compile_delivery(lambda *args: Outcome('failed', detail='bad output', usage=SCRIPTED_USAGE), load_config(), run=self.run, checkpointer=saver)
                graph.update_state({'configurable': {'thread_id': 'example/engine#8'}},
                                   {'stop': stop, 'watch': None}, as_node='done')
            self.forge.open_issues.side_effect = ForgeError('#48 ' + secret)
            stream = io.StringIO()
            with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
                actual = main(self.flags, checkpoint=self.checkpoint, live_run=self.run, worker_runner=lambda *args: subprocess.CompletedProcess([], 0, 'invalid', ''))
            self.assertEqual(actual, code)
            text = stream.getvalue()
            self.assertNotIn(secret, text)
            self.assertTrue(text.endswith('watch failed: #48 [redacted:GitHub Token]\n'), text)
            self.assertLess(text.index('done '), text.index('watch failed:'))
            self.assertIsNone(self.stored()['watch'])
            self.forge.open_issues.side_effect = None
            self.assertIsNotNone(self.invoke()['watch'])
