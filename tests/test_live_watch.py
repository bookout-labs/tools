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
from foreman.stage_output import StoredSlice
from foreman.adapters import Outcome, Usage
from foreman.cli import main
from foreman.config import load_config
from foreman.delivery import LiveRun, compile_delivery
from foreman.github import Forge, ForgeError, LiveForge
from foreman.runner import run_live_delivery, saved_workspace
from foreman.skill_rules import SkillMissing
from foreman.workspace import WorkspaceError
from foreman.dry_run import ScriptHandler, SCRIPTED_USAGE
from foreman.worker import CliWorker
from skill_fixture import fixture_skills
from test_runner import EMPTY_SLICE, FakeGitHub, FakeWorkspace, slice_body, worker_reply
from foreman.dry_run import CLEAR
from skill_fixture import checker_reply


class LiveWatchTest(unittest.TestCase):
    def setUp(self):
        # The checkpoint sits beside the checkout, so the run's scratch stays in the test's own directory.
        self.root = Path(self.enterContext(TemporaryDirectory())) / 'checkout'
        self.root.mkdir()
        self.checkpoint = self.root.parent / 'runs.sqlite3'
        self.fake = FakeGitHub()
        self.forge = Mock(spec=Forge, wraps=self.fake)
        self.run = LiveRun(self.forge, FakeWorkspace(self.root), skills=fixture_skills, human_login="fixture-owner")
        self.flags = ['run', 'delivery', '--repo', 'Example/Engine', '--issue', '8']

    def invoke(self, **kwargs):
        if saved_workspace(self.checkpoint, 'example/engine#8') is None:
            self.fake.ready_again(8)  # A fresh run starts from Ready.
        return run_live_delivery(repo='Example/Engine', issue=8, checkpoint=self.checkpoint, run=self.run, **({'dispatcher': lambda *args: Outcome('failed', detail='bad output', usage=SCRIPTED_USAGE)} | kwargs))

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
        self.run = LiveRun(self.forge, self.run.workspace, skills=missing, human_login="fixture-owner")
        first = self.invoke()
        stored = self.stored()
        self.assertEqual((stored['run'].issue, stored['run'].workspace, stored['run'].repo, stored['checks'].check, stored['run'].token_cap),
                         (8, str(self.root.resolve()), 'Example/Engine', '', 20_000_000))
        self.assertEqual(stored['pstack_pin'], first['pstack_pin'])
        self.assertIn('File to change: src/foreman/skill_rules.py', self.fake.created[0][1])
        self.assertEqual(self.invoke(), first)
        missing.assert_called_once()
        # The halt comes before triage. Only the status mirror reads the issue, once per invocation.
        self.assertEqual([call.args for call in self.forge.load_issue.call_args_list], [('Example/Engine', 8)] * 2)
        self.forge.create_issue.assert_called_once()

    def test_resumed_git_failure_adds_one_comment_per_new_halt(self):
        workspace = FakeWorkspace(self.root)
        workspace.create_branch = Mock(side_effect=WorkspaceError('git failed'))
        self.run = LiveRun(self.forge, workspace, skills=fixture_skills, human_login="fixture-owner")
        dispatcher = CliWorker(self.root, lambda args, *unused: worker_reply(args, checker_reply(args) or {"triage": "supplied_slice"}),
                               scratch_root=self.checkpoint.parent)
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
            self.checkpoint = self.root.parent / f'{target is None}.sqlite3'
            self.forge.open_issues.return_value = []
            flags = self.flags + (['--watch-repo', target] if target else [])
            self.fake.ready_again(8)
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
            self.fake.labels = ['Ready']
            code = main(self.flags, checkpoint=self.checkpoint, live_run=self.run,
                        worker_runner=Mock(side_effect=FileNotFoundError('missing CLI')))
        # With no CLI installed, dispatch pauses for a qualified worker; a pause files nothing.
        self.assertEqual(code, 75)
        self.assertNotIn('watch filed', log.getvalue())
        self.forge.open_issues.assert_not_called()
        self.forge.create_issue.assert_not_called()
        self.forge.comment_issue.assert_not_called()
        self.forge.reset_mock()
        for exit_code, expected in ((0, 0), (1, 1)):
            with patch('foreman.cli.LiveForge', side_effect=AssertionError('dry Forge')), patch('foreman.delivery.DryRun.run', return_value=exit_code), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(['run', 'delivery', '--repo', 'Example/Engine', '--request', 'test', '--dry-run'], live_run=self.run), expected)
        self.assertEqual(self.forge.mock_calls, [])

    def test_blocked_run_reports_once_without_watching(self):
        seam = {'name': 'items', 'exists': False, 'path': None}
        blocker = StoredSlice.model_validate({**EMPTY_SLICE, 'title': 'blocker', 'builds': [seam]})
        number = self.fake.create_issue('Example/Engine', blocker.title, '', ['slice'], child=blocker, marker="")
        self.fake.dependencies[8] = [number]
        self.fake.body = slice_body({**EMPTY_SLICE, 'uses': [seam], 'depends_on': [blocker.title], 'blocker_numbers': [number]})
        reports = []
        result = self.invoke(report=reports.append)
        self.assertEqual([report['stop'] for report in reports], ['blocked'])
        self.assertEqual(result['stop'], 'blocked')
        self.forge.open_issues.assert_not_called()
        self.forge.create_issue.assert_not_called()
        self.fake.closed.add(number)
        self.fake.merged.add(number)
        def dispatch(stage, *args):
            return (Outcome('ran', {'triage': 'supplied_slice', 'human_question': None, 'work_kind': 'feature', 'slice': None},
                            usage=Usage(cache_read_input_tokens=0, cache_creation_input_tokens=0, input_tokens=1, output_tokens=1, total_cost_usd=0.0)) if stage == 'triage' else Outcome('quota', usage=SCRIPTED_USAGE))
        quota = dispatch
        paused = self.invoke(dispatcher=quota)
        self.assertEqual(paused['stop'], 'quota_exhausted')
        again = self.invoke(dispatcher=quota)
        self.assertEqual(again['stop'], 'quota_exhausted')
        self.assertGreater(len(again['attempts']), len(paused['attempts']))
        self.assertEqual([attempt['stage'] for attempt in again['attempts']].count('triage'), 1)
        reads = [call.args[-1] for call in self.forge.load_issue.call_args_list]
        # The status mirror reads the issue's labels at each admitted stage start and after each invocation.
        # Resuming the quota wait re-enters spec's check step: the runner rereads the issue identity,
        # and validate's checkpointed result stands, so the blocker is not read again.
        self.assertEqual(reads, [8, 8, 8, 8, number, 8, 8, 8])
        self.assertEqual({attempt['stage'] for attempt in again['attempts']}, {'triage', 'spec_check'})

    def test_skill_failure_preserves_resumable_checkpoints(self):
        for stop in ('git_failed', 'quota_exhausted'):
            with self.subTest(stop=stop):
                self.checkpoint = self.root.parent / f'{stop}.sqlite3'
                workspace = FakeWorkspace(self.root)
                workspace.create_branch = Mock(side_effect=WorkspaceError('git failed'))
                self.run = LiveRun(self.forge, workspace, skills=fixture_skills, human_login="fixture-owner")
                def dispatch(stage, *args):
                    if stop == 'quota_exhausted':
                        return Outcome('quota', usage=SCRIPTED_USAGE)
                    if stage == 'spec_check':
                        return Outcome('ran', CLEAR.model_dump(), usage=SCRIPTED_USAGE)
                    return Outcome('ran', {'triage': 'supplied_slice', 'human_question': None, 'work_kind': 'feature', 'slice': None},
                                   usage=Usage(cache_read_input_tokens=0, cache_creation_input_tokens=0, input_tokens=1, output_tokens=1, total_cost_usd=0.0))
                dispatcher = dispatch
                self.assertEqual(self.invoke(dispatcher=dispatcher)['stop'], stop)
                before = self.stored()
                self.run = LiveRun(self.forge, workspace, skills=Mock(side_effect=SkillMissing(load_config().pstack)), human_login="fixture-owner")
                reports = []
                failed = self.invoke(dispatcher=dispatcher, report=reports.append)
                self.assertEqual([result['stop'] for result in reports], ['skill_missing'])
                self.assertIsNone(failed['watch'])
                self.assertEqual(self.stored(), before)
                self.run = LiveRun(self.forge, workspace, skills=fixture_skills, human_login="fixture-owner")
                self.assertEqual(self.invoke(dispatcher=dispatcher)['stop'], stop)

    def test_live_halt_detail_stays_redacted_on_the_run_issue_and_out_of_the_watch_repo(self):
        secret = 'ghp_' + 'a' * 36
        status = []
        def gh(args, **kwargs):
            endpoint = args[2]
            fields = dict(arg.split('=', 1) for arg in args if '=' in arg and not arg.startswith('repos/'))
            if args[1] == 'issue' or endpoint.startswith(('repos/Example/Engine/labels', 'repos/Example/Engine/issues/8/comments')):
                if '--method' in args and 'body' in fields:
                    status.append(fields['body'])
                result = [[]] if '?' in endpoint else {}  # Foreman's status mirror on the run's own issue.
            elif endpoint == 'repos/Example/Engine/issues/8':
                result = {'number': 8, 'repository_url': 'https://api.github.com/repos/Example/Engine', 'title': 'Issue', 'body': self.fake.body, 'labels': [{'name': 'slice'}, {'name': 'Ready'}]}
            elif args[1] == 'repo':
                result = {'defaultBranchRef': {'name': 'main'}}
            elif args[1] == 'variable':
                result = [{'name': 'FOREMAN_HUMAN_LOGIN', 'value': 'fixture-owner'}]
            elif endpoint.endswith('/collaborators/fixture-owner/permission'):
                result = {'permission': 'write', 'role_name': 'write'}
            elif '/rules/branches/' in endpoint:
                result = [{'type': 'merge_queue', 'parameters': {'merge_method': 'SQUASH'}}]
            elif '--method' in args:
                if endpoint.endswith('/comments'):
                    self.fake.comment_issue('bookout-labs/tools', 101, fields['body'], marker="")
                    result = {}
                else:
                    result = {'number': self.fake.create_issue('bookout-labs/tools', fields['title'], fields['body'], [], marker="")}
            elif endpoint.endswith('/dependencies/blocked_by'):
                result = [[]]
            elif '/comments?' in endpoint:
                result = [[{'body': body} for body in self.fake.issue_comments('bookout-labs/tools', 101)]]
            else:
                result = [[issue.model_dump() for issue in self.fake.open_issues('bookout-labs/tools')]]
            return subprocess.CompletedProcess(args, 0, json.dumps(result), '')
        workspace = FakeWorkspace(self.root)
        workspace.create_branch = Mock(side_effect=WorkspaceError('#48 ' + secret))
        self.run = LiveRun(LiveForge(), workspace, skills=fixture_skills, human_login="fixture-owner")
        dispatcher = CliWorker(self.root, lambda args, *unused: worker_reply(args, checker_reply(args) or {'triage': 'supplied_slice'}),
                               scratch_root=self.checkpoint.parent)
        real_run = subprocess.run
        fake_gh = lambda args, **kwargs: gh(args, **kwargs) if args[0] == 'gh' else real_run(args, **kwargs)
        with patch('foreman.github.subprocess.run', side_effect=fake_gh), \
                patch.object(LiveForge, 'app_installation', return_value='installed'):
            self.invoke(dispatcher=dispatcher)
            self.invoke(dispatcher=dispatcher)
        # The watch repo is another repository, so the halt detail stays on the run's own issue, redacted.
        for body in (self.fake.created[0][1], self.fake.comments['bookout-labs/tools', 101][0]):
            self.assertNotIn(secret, body)
            self.assertNotIn('#48', body)
        self.assertTrue(status)
        for body in status:
            self.assertNotIn(secret, body)
        self.assertTrue(any('#48 [redacted:GitHub Token]' in body for body in status))

    def test_watch_errors_keep_exit_codes_are_redacted_ordered_and_retried(self):
        secret = 'ghp_' + 'a' * 36
        for stop, code in (('invalid_output', 1), ('git_failed', 75), ('needs_human', 3)):
            self.checkpoint = self.root.parent / f'{stop}.sqlite3'
            if code == 75:
                self.run.workspace.create_branch = Mock(side_effect=WorkspaceError('git failed'))
                dispatcher = CliWorker(self.root, lambda args, *unused: worker_reply(args, checker_reply(args) or {'triage': 'supplied_slice'}),
                                       scratch_root=self.checkpoint.parent)
                self.invoke(dispatcher=dispatcher)
            else:
                self.invoke()
            with SqliteSaver.from_conn_string(str(self.checkpoint)) as saver:
                saver.serde = checkpoint_serializer()
                graph = compile_delivery(lambda *args: Outcome('failed', detail='bad output', usage=SCRIPTED_USAGE), load_config(), run=self.run, checkpointer=saver)
                graph.update_state({'configurable': {'thread_id': 'example/engine#8'}},
                                   {'stop': stop, 'watch': None}, as_node='research')
            self.forge.open_issues.side_effect = ForgeError('#48 ' + secret)
            stream = io.StringIO()
            with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
                actual = main(self.flags, checkpoint=self.checkpoint, live_run=self.run, worker_runner=lambda *args: subprocess.CompletedProcess([], 0, 'invalid', ''))
            self.assertEqual(actual, code)
            text = stream.getvalue()
            self.assertNotIn(secret, text)
            self.assertTrue(text.endswith('watch failed: #48 [redacted:GitHub Token]\n'), text)
            # Stage stops no longer pass through `done`, so check the run report prints before the watch error.
            self.assertLess(text.index('this invocation'), text.index('watch failed:'))
            self.assertIsNone(self.stored()['watch'])
            self.forge.open_issues.side_effect = None
            self.assertIsNotNone(self.invoke()['watch'])
