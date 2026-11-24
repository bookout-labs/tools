"""Offline command proofs through provisioning, the runner and GitHub receipts."""
import tests

from contextlib import contextmanager, redirect_stdout, redirect_stderr
from copy import deepcopy
import io
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch, Mock

from foreman.cli import main
from foreman.github import BOT_LOGIN, ForgeError, IssueComment, LiveForge
from foreman.delivery import FIXTURE_HUMAN
from foreman.readiness import MergeState
from foreman.smoke_command import SMOKE_REPO, SMOKE_TOKEN_CAP, SMOKE_CLAUDE_USD, run_smoke, smoke_command
from foreman.status import STATUS_MARKER
from tests.test_smoke import passing


class SmokeCommandTest(unittest.TestCase):
    @contextmanager
    def provision(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (patch('foreman.smoke_command.SMOKE_TARGETS', (SMOKE_REPO,)),
                  patch('foreman.smoke_command.credentials.environment', return_value={'PATH': '/app', 'GH_TOKEN': 'app'}) as credentials,
                  patch('foreman.smoke_command.LiveForge') as forge,
                  patch('foreman.smoke_command.subprocess.run') as process,
                  patch('foreman.smoke_command.subprocess.check_output', return_value='abc'),
                  patch('foreman.smoke_command.run_live_delivery', return_value=passing()) as runner,
                  patch('foreman.smoke_command.smoke_lock'),
                  patch.dict(os.environ), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors):
                forge.return_value.smoke_issues.return_value = []
                forge.return_value.smoke_pull_requests.return_value = []
                forge.return_value.create_issue.return_value = 17
                forge.return_value.default_branch.return_value = "trunk"
                forge.return_value.smoke_identity.return_value = (True, True, True)
                forge.return_value.load_issue.return_value = {"repo": SMOKE_REPO, "number": 17, "labels": ["smoke", "Doing"]}
                forge.return_value.issue_comment_records.return_value = [IssueComment.model_validate(
                    {"id": 1, "user": {"login": BOT_LOGIN}, "body": f"Foreman is running merge.\n\n{STATUS_MARKER}"})]
                yield root, forge.return_value, process, runner, credentials, errors

    def test_cli_routes_to_smoke(self):
        with patch('foreman.smoke_command.smoke_command', return_value=78) as command:
            self.assertEqual(main(['smoke']), 78)
            command.assert_called_once_with()

    def test_live_runner_and_limits(self):
        with self.provision() as (root, forge, process, runner, credentials, errors):
            self.assertEqual(run_smoke(root, time.monotonic() + 30), 0)
            kwargs = runner.call_args.kwargs
            self.assertEqual((kwargs['repo'], kwargs['issue']), (SMOKE_REPO, 17))
            self.assertIn('smoke_17.txt', kwargs['request'])
            self.assertEqual(kwargs['max_tokens'], SMOKE_TOKEN_CAP)
            self.assertEqual(kwargs['max_budget_usd'], SMOKE_CLAUDE_USD)
            self.assertEqual(kwargs['run'].workspace.root, root / 'checkout')
            self.assertIs(kwargs['run'].forge, forge)
            self.assertEqual(kwargs['run'].human_login, FIXTURE_HUMAN)
            self.assertFalse(os.environ['PATH'].startswith(str(root / 'python/bin')))
            self.assertEqual(process.call_args_list[0].kwargs['env'], credentials.return_value)
            forge.create_issue.assert_called_once()
            self.assertEqual(forge.create_issue.call_args.args[-1], ['smoke', 'Ready'])
            forge.smoke_identity.assert_called_once_with(SMOKE_REPO, 2, 'abc', 1)
            for observed in ((False, True, True), (True, False, True), (True, True, False)):
                forge.smoke_identity.return_value = observed
                self.assertEqual(run_smoke(root, time.monotonic() + 30), 1)
            runner.return_value = {**passing(), 'attempts': [{**passing()['attempts'][0], 'outcome': 'quota'}]}
            forge.smoke_identity.return_value = (True, True, True)
            forge.smoke_identity.reset_mock()
            self.assertEqual(run_smoke(root, time.monotonic() + 30), 78)
            forge.smoke_identity.assert_called_once()
            # The armed fixture shows only Doing and one status comment.
            for labels, comments in ((["smoke", "Doing", "Done"], 1), (["smoke", "Ready"], 1), (["smoke", "Doing"], 2)):
                with self.subTest(labels=labels, comments=comments):
                    forge.load_issue.return_value = {"repo": SMOKE_REPO, "number": 17, "labels": labels}
                    forge.issue_comment_records.return_value = forge.issue_comment_records.return_value[:1] * comments
                    self.assertEqual(run_smoke(root, time.monotonic() + 30), 1)
            # An issue status GitHub can't show fails the smoke instead of passing on the checkpoint's verdict.
            forge.issue_comment_records.return_value = forge.issue_comment_records.return_value[:1]
            forge.load_issue.side_effect = ForgeError("GitHub is unavailable.")
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(run_smoke(root, time.monotonic() + 30), 1)
            self.assertIn("smoke exit 1 status.unavailable attempt GitHub issue status", output.getvalue())

    def test_cleanup_failure_stops_before_filing_and_reports_error(self):
        with self.provision() as (root, forge, process, runner, credentials, errors):
            forge.smoke_pull_requests.side_effect = ForgeError('cleanup denied')
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(run_smoke(root, time.monotonic() + 30), 78)
            self.assertIn('smoke exit 78 cleanup attempt prerequisite', output.getvalue())
            self.assertIn('cleanup denied', errors.getvalue())
            forge.ensure_label.assert_called_once_with(SMOKE_REPO, "smoke", "ededed")
            forge.create_issue.assert_not_called()
            runner.assert_not_called()

    def test_lock_oserror_stops_before_provisioning(self):
        with self.provision() as (root, forge, process, runner, credentials, errors):
            with patch('foreman.smoke_command.smoke_lock', side_effect=OSError('lock denied')), \
                 redirect_stdout(io.StringIO()) as output:
                self.assertEqual(run_smoke(root, time.monotonic() + 30), 78)
            self.assertEqual(output.getvalue().splitlines()[0], f'smoke {SMOKE_REPO} exit 78 lock attempt prerequisite')
            self.assertIn('lock denied', errors.getvalue())
            credentials.assert_not_called()
            forge.create_issue.assert_not_called()
            runner.assert_not_called()

    def test_merge_wait_resume_preserves_checkpoint_and_limits(self):
        waiting = {**passing(), 'merge': MergeState()}
        with self.provision() as (root, forge, process, runner, credentials, errors):
            runner.side_effect = [waiting, passing()]
            with patch('foreman.smoke_command.time.sleep'):
                self.assertEqual(run_smoke(root, time.monotonic() + 30), 0)
            self.assertEqual(runner.call_count, 2)
            for call in runner.call_args_list:
                self.assertEqual(call.kwargs['checkpoint'], root / 'checkpoint.sqlite3')
                self.assertEqual(call.kwargs['max_tokens'], 1_500_000)
                self.assertEqual(call.kwargs['max_budget_usd'], SMOKE_CLAUDE_USD)
            runner.side_effect = None
            runner.return_value = waiting
            self.assertEqual(run_smoke(root, time.monotonic() - 1), 1)

    def test_prerequisites_fail_before_issue_and_runner(self):
        real_run = subprocess.run
        for failure in ('scope', 'python_missing', 'python_version'):
            with self.subTest(failure=failure), self.provision() as (root, forge, process, runner, credentials, errors):
                if failure == 'scope':
                    forge.default_branch.side_effect = ForgeError('scope denied')
                else:
                    def provision(args, **kwargs):
                        if args[0] == 'python3.12' and failure == 'python_missing':
                            raise FileNotFoundError('python3.12')
                        if args[0] == 'python':
                            code = args[2].replace('import sys;', 'import sys; sys.version_info = (3, 14);')
                            return real_run([sys.executable, "-c", code], capture_output=True, check=True)
                    def checked(args, **kwargs):
                        try:
                            return provision(args, **kwargs)
                        except subprocess.CalledProcessError:
                            raise subprocess.CalledProcessError(1, args, stderr=b'version mismatch ghp_' + b'a' * 36)
                    process.side_effect = checked
                self.assertEqual(run_smoke(root, time.monotonic() + 30), 78)
                forge.create_issue.assert_not_called()
                runner.assert_not_called()
                if failure == 'python_version':
                    self.assertIn('version mismatch', errors.getvalue())
                    self.assertNotIn('ghp_' + 'a' * 36, errors.getvalue())

    def test_personal_credentials_rejected_through_real_environment(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'GH_TOKEN': 'personal', 'XDG_STATE_HOME': directory}, clear=True), \
             patch('foreman.smoke_command.LiveForge') as forge, redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(run_smoke(Path(directory), time.monotonic() + 30), 78)
            forge.assert_not_called()

    def test_provisioned_check_accepts_worktree_and_commit_rejects_bad_fixtures(self):
        real_run = subprocess.run
        with self.provision() as (root, forge, process, runner, credentials, errors):
            checkout = root / 'checkout'
            checkout.mkdir()
            (checkout / 'tests').mkdir()
            (checkout / 'tests/test_fixture.py').write_text('import unittest\nclass Fixture(unittest.TestCase):\n    def test_fixture(self): self.assertTrue(True)\n')
            (checkout / '.gitignore').write_text('__pycache__/\n')
            def git(*args):
                result = real_run(['git', *args], cwd=checkout, capture_output=True, text=True, check=True)
                return result.stdout.strip()
            git('init')
            git('add', '.')
            git('-c', 'user.name=test', '-c', 'user.email=test@example.com', 'commit', '-m', 'base')
            base = 'refs/remotes/origin/trunk'
            git('update-ref', base, 'HEAD')
            def provision(args, **kwargs):
                if args[0] == 'python3.12':
                    bin_dir = root / 'python/bin'
                    bin_dir.mkdir(parents=True)
                    (bin_dir / 'python').symlink_to(sys.executable)
            process.side_effect = provision
            def delivery(**kwargs):
                (checkout / 'origin.txt').write_text('Intervening origin change')
                git('add', 'origin.txt')
                git('-c', 'user.name=test', '-c', 'user.email=test@example.com', 'commit', '-m', 'Origin moved')
                git('update-ref', base, 'HEAD')
                command = kwargs['check']
                env = {**os.environ, 'PATH': str(root / 'python/bin') + os.pathsep + os.defpath}
                def check(code):
                    result = real_run(command, cwd=checkout, env=env, shell=True, capture_output=True)
                    self.assertEqual(result.returncode == 0, code, result.stderr.decode())
                path = checkout / 'smoke_17.txt'
                check(False)
                path.write_text('smoke 17\n')
                check(True)
                path.write_text('wrong\n')
                check(False)
                path.write_text('smoke 17\n')
                (checkout / 'other').write_text('other')
                check(False)
                (checkout / 'other').unlink()
                git('add', '.')
                check(False)
                git('-c', 'user.name=test', '-c', 'user.email=test@example.com', 'commit', '-m', 'fixture')
                check(True)
                committed = git('rev-parse', 'HEAD')
                origin_parent = git('rev-parse', base)
                git('switch', '--detach', origin_parent)
                (checkout / 'later-origin.txt').write_text('Later origin change')
                git('add', 'later-origin.txt')
                git('-c', 'user.name=test', '-c', 'user.email=test@example.com', 'commit', '-m', 'Origin advanced again')
                moved = git('rev-parse', 'HEAD')
                git('switch', '--detach', committed)
                git('update-ref', base, moved)
                check(True)
                saved_command = command
                command = command.replace(base, committed)
                check(False)
                command = saved_command
                path.write_text('wrong\n')
                check(False)
                git('checkout', '--', 'smoke_17.txt')
                check(True)
                path.write_text('old\n')
                git('add', '.')
                git('-c', 'user.name=test', '-c', 'user.email=test@example.com', 'commit', '-m', 'existing')
                replacement_base = git('rev-parse', 'HEAD')
                path.write_text('smoke 17\n')
                git('add', '.')
                git('-c', 'user.name=test', '-c', 'user.email=test@example.com', 'commit', '-m', 'replace')
                command = command.replace(base, replacement_base)
                check(False)
                return passing()
            runner.side_effect = delivery
            self.assertEqual(run_smoke(root, time.monotonic() + 30), 0)

    def test_observed_identity_receipts(self):
        email = '359513816+' + BOT_LOGIN + '@users.noreply.github.com'
        receipts = [{'user': {'login': BOT_LOGIN}, 'head': {'sha': 'ABC'}},
                    {'sha': 'ABC', 'author': {'login': BOT_LOGIN}, 'committer': {'login': BOT_LOGIN},
                     'commit': {'author': {'name': BOT_LOGIN, 'email': email}, 'committer': {'name': BOT_LOGIN, 'email': email}}},
                    {'user': {'login': BOT_LOGIN}, 'state': 'COMMENTED', 'commit_id': 'ABC'}]
        variants = [(receipts, (True, True, True))]
        for index, path, value in (
            (0, ('user', 'login'), 'personal'), (0, ('head', 'sha'), 'old'),
            (1, ('sha',), 'old'), (1, ('author',), None), (1, ('author', 'login'), 'personal'),
            (1, ('committer',), None), (1, ('committer', 'login'), 'personal'),
            (1, ('commit', 'author', 'name'), 'personal'), (1, ('commit', 'author', 'email'), 'personal@example.com'),
            (1, ('commit', 'committer', 'name'), 'personal'), (1, ('commit', 'committer', 'email'), 'personal@example.com'),
            (2, ('user', 'login'), 'personal'), (2, ('state',), 'APPROVED'), (2, ('commit_id',), 'old')):
            changed = deepcopy(receipts)
            target = changed[index]
            for field in path[:-1]:
                target = target[field]
            target[path[-1]] = value
            variants.append((changed, tuple(i != index for i in range(3))))
        coauthor_only = deepcopy(receipts)
        coauthor_only[1]['commit'].update(
            author={'name': 'personal', 'email': 'personal@example.com'},
            committer={'name': 'personal', 'email': 'personal@example.com'},
            message=f'Fixture\n\nCo-authored-by: {BOT_LOGIN} <{email}>',
        )
        variants.append((coauthor_only, (True, False, True)))
        for data, expected in variants:
            with self.subTest(data=data):
                forge = LiveForge()
                def read(repo, model, command):
                    return model.model_validate(data[1 if '/commits/' in command[-1] else 2 if '/reviews/' in command[-1] else 0])
                with patch.object(forge, '_read', side_effect=read), patch.object(forge, 'bot_identity', return_value={'GIT_AUTHOR_EMAIL': email}):
                    self.assertEqual(forge.smoke_identity(SMOKE_REPO, 2, 'abc', 1), expected)

    def test_deadline_and_signals_clean_process_group_and_directory(self):
        real_popen = subprocess.Popen
        for stop in ('deadline', 'SIGTERM', 'SIGINT'):
            with self.subTest(stop=stop), tempfile.TemporaryDirectory() as directory:
                receipt = Path(directory) / 'pid'
                child_code = ("import subprocess,sys,time; from pathlib import Path; "
                              "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
                              f"Path({str(receipt)!r}).write_text(str(p.pid)); time.sleep(60)")
                parent_code = (
                    "import subprocess; from unittest.mock import patch; import foreman.smoke_command as m; "
                    "original=subprocess.Popen; "
                    f"child={child_code!r}; "
                    "launch=lambda args, **kw: original([args[0], '-c', child], **kw); "
                    f"patch('foreman.smoke_command.SMOKE_SECONDS', {0.7 if stop == 'deadline' else 30}).start(); "
                    "patch('foreman.process_group.subprocess.Popen', side_effect=launch).start(); "
                    "raise SystemExit(m.smoke_command())"
                )
                parent = real_popen([sys.executable, '-c', parent_code], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    for _ in range(300):
                        if receipt.exists():
                            break
                        time.sleep(0.01)
                    self.assertTrue(receipt.exists())
                    if stop != 'deadline':
                        parent.send_signal(getattr(signal, stop))
                    out, err = parent.communicate(timeout=5)
                    self.assertEqual(parent.returncode, 1 if stop == 'deadline' else 128 + getattr(signal, stop), (out, err))
                    pid = int(receipt.read_text())
                    for _ in range(100):
                        status = subprocess.run(['ps', '-p', str(pid), '-o', 'stat='], capture_output=True, text=True)
                        if status.returncode or 'Z' in status.stdout:
                            break
                        time.sleep(0.01)
                    else:
                        self.fail('worker survived group cleanup')
                finally:
                    if parent.poll() is None:
                        parent.kill()
                        parent.wait()

    def test_parent_uses_new_session_and_cleans_temp_directory(self):
        process = Mock(pid=99999999)
        process.wait.side_effect = [subprocess.TimeoutExpired('child', 0), 0, 0]
        def signal_group(pid, sig):
            if sig == 0:
                raise ProcessLookupError
        with patch('foreman.process_group.subprocess.Popen', return_value=process) as launch, \
             patch('foreman.process_group.os.killpg', side_effect=signal_group) as kill, redirect_stdout(io.StringIO()):
            self.assertEqual(smoke_command(), 1)
        self.assertTrue(launch.call_args.kwargs['start_new_session'])
        self.assertFalse(Path(launch.call_args.args[0][3]).exists())
        kill.assert_any_call(process.pid, signal.SIGTERM)
        kill.assert_any_call(process.pid, signal.SIGKILL)

    def test_watcher_files_failure_and_stays_silent_for_unavailable_and_budget(self):
        from foreman.adapters import Outcome
        from foreman.delivery import LiveRun
        from foreman.runner import run_live_delivery
        from skill_fixture import fixture_skills
        from test_runner import FakeGitHub, FakeWorkspace
        real_run, real_output = subprocess.run, subprocess.check_output
        for kind, cap, expected, filed in (('failed', SMOKE_TOKEN_CAP, 1, True),
                                           ('cli_missing', SMOKE_TOKEN_CAP, 78, False),
                                           ('failed', 1, 1, False)):
            with self.subTest(kind=kind, cap=cap), self.provision() as (root, forge, process, runner, credentials, errors):
                fake = FakeGitHub(labels=[])
                forge.create_issue.side_effect = fake.create_issue
                forge.load_issue.side_effect = fake.load_issue
                for name in ('open_issues', 'comment_issue', 'marked_issues'):
                    getattr(forge, name).side_effect = getattr(fake, name)
                def delivery(**kwargs):
                    kwargs['run'] = LiveRun(forge, FakeWorkspace(root / 'checkout'), skills=fixture_skills, human_login=kwargs['run'].human_login)
                    from foreman.dry_run import ScriptHandler
                    kwargs['dispatcher'] = ScriptHandler() if cap == 1 else lambda *args: Outcome(kind, detail='bad output')
                    with patch('subprocess.run', real_run), patch('subprocess.check_output', real_output):
                        result = run_live_delivery(**kwargs)
                    self.assertEqual(result.get('watch') is not None, filed)
                    self.assertEqual(result['run'].token_cap, cap)
                    return result
                runner.side_effect = delivery
                with patch('foreman.smoke_command.SMOKE_TOKEN_CAP', cap):
                    self.assertEqual(run_smoke(root, time.monotonic() + 30), expected)
                self.assertEqual(len(fake.created), 2 if filed else 1)
                if filed:
                    self.assertEqual(fake.issue_repos[102], 'bookout-labs/tools')

    def test_real_runner_resumes_the_merge_wait_without_redispatch_or_resetting_usage(self):
        from dataclasses import replace
        from foreman.delivery import LiveRun
        from foreman.dry_run import ScriptHandler
        from foreman.runner import run_live_delivery
        from skill_fixture import fixture_skills
        from test_runner import FakeGitHub, FakeWorkspace
        real_run, real_output = subprocess.run, subprocess.check_output
        with self.provision() as (root, forge, process, runner, credentials, errors):
            fake = FakeGitHub(labels=[])
            transport = Mock(wraps=fake)
            transport.smoke_identity = Mock(return_value=(True, True, True))
            transport.smoke_issues = Mock(return_value=[])
            transport.smoke_pull_requests = Mock(return_value=[])
            handler = ScriptHandler()
            workspace = FakeWorkspace(root / 'checkout')
            workspace.run = Mock(return_value=0)
            def dispatch(node, *args):
                if node == 'implement':
                    from foreman.workspace import WorktreeState
                    workspace.current = WorktreeState(workspace.current.head, False, workspace.current.branch)
                outcome = handler(node, *args)
                return replace(outcome, prompt_bytes=123, native_usage={
                    "family": args[0].family, "aggregation": "turn_total",
                    "entries": {"turn": {"input_tokens": 1, "output_tokens": 1,
                        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
                        "total_cost_usd": 0.0}},
                })
            results = []
            observations = 0
            def readiness(*args):
                nonlocal observations
                observations += 1
                observation = fake.readiness(*args)
                return observation.model_copy(update={'outcome': 'pending'}) if observations == 1 else observation
            transport.readiness.side_effect = readiness
            def delivery(**kwargs):
                kwargs['run'] = LiveRun(transport, workspace, skills=fixture_skills, human_login=kwargs['run'].human_login)
                kwargs['dispatcher'] = dispatch
                with patch('subprocess.run', real_run), patch('subprocess.check_output', real_output):
                    result = run_live_delivery(**kwargs)
                results.append(result)
                return result
            runner.side_effect = delivery
            with patch('foreman.smoke_command.LiveForge', return_value=transport), patch('foreman.smoke_command.time.sleep'):
                self.assertEqual(run_smoke(root, time.monotonic() + 30), 0, results)
            self.assertEqual(len(results), 2)
            self.assertEqual((results[0]['stop'], results[0].get('merge', MergeState()).status), ('merge_pending', None))
            self.assertNotIn('halt', results[0])
            self.assertEqual(results[0]['attempts'], results[1]['attempts'])
            for attempt in results[1]['attempts']:
                self.assertEqual(attempt['prompt_bytes'], 123)
                self.assertEqual(attempt['native_usage']['entries']['turn']['input_tokens'], 1)
            self.assertEqual(results[0]['review'], results[1]['review'])
            transport.smoke_identity.assert_called_once_with(
                SMOKE_REPO, 12, results[1]['checks'].verified_head,
                results[1]['review'].review_receipt['id'])
            for result in results:
                self.assertEqual(result['run'].token_cap, SMOKE_TOKEN_CAP)
                self.assertEqual(result['run'].max_budget_usd, '2')
            self.assertEqual([call['node'] for call in handler.calls], ['triage', 'spec_check', 'implement', 'review'])
