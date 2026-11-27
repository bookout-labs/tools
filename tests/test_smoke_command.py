"""Offline command proofs through provisioning, the host's receiver, the saved run and GitHub receipts."""
import tests

from contextlib import closing, contextmanager, redirect_stdout, redirect_stderr
from copy import deepcopy
import io
import os
from pathlib import Path
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch, Mock

from langgraph.checkpoint.sqlite import SqliteSaver
from watchfiles._rust_notify import RustNotify

from foreman.cli import main
from foreman.github import BOT_LOGIN, ForgeError, IssueComment, LiveForge
from foreman.runner import SavedRun, saved_run
from foreman.state import SCHEMA_VERSION
from foreman.smoke_command import SMOKE_REPO, published_file, receiving, run_end, run_smoke, smoke_command
from foreman.status import STATUS_MARKER
from smoke_host import COMMIT, GH_TOKENS, HUMAN, FakeHost, fake_gh, gh_calls
from foreman.smoke_ownership import add_ready as real_add_ready, human_session as real_human_session
from tests.test_smoke import passing

REAL_RUN = subprocess.run


def gh_only(args, **kwargs):
    """The smoke's subprocesses, with only gh run for real."""
    return REAL_RUN(args, **kwargs) if args[0] == 'gh' else subprocess.CompletedProcess(args, 0, b'', b'')


class SmokeCommandTest(unittest.TestCase):
    @staticmethod
    def git_output(args, **kwargs):
        """Git as the clone answers it: the published head adds only the issue's file with its requested line."""
        match args[1:3]:
            case ['diff', '--numstat']:
                return '1\t0\tsmoke_17.txt\n'
            case ['show', _]:
                return 'smoke 17\n'
        return 'abc'

    @contextmanager
    def provision(self):
        """The smoke's own steps, faked, around a saved run that the deliveries left: by default one that merged after a wake."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (patch('foreman.smoke_command.SMOKE_TARGETS', (SMOKE_REPO,)),
                  patch('foreman.smoke_command.credentials.environment', return_value={'PATH': '/app', 'GH_TOKEN': 'app'}) as credentials,
                  patch('foreman.smoke_command.LiveForge') as forge,
                  patch('foreman.smoke_command.subprocess.run') as process,
                  patch('foreman.smoke_command.subprocess.check_output', side_effect=self.git_output),
                  patch('foreman.smoke_command.receiving', return_value=True),
                  patch('foreman.smoke_command.release', return_value=COMMIT),
                  patch('foreman.smoke_command.human_session') as session,
                  patch('foreman.smoke_command.add_ready') as ready,
                  patch('foreman.smoke_command.run_end', return_value=SavedRun(passing(), False, True)) as end,
                  patch('foreman.smoke_command.smoke_lock'),
                  patch.dict(os.environ, {'XDG_STATE_HOME': str(root / 'state')}), redirect_stdout(io.StringIO()),
                  redirect_stderr(io.StringIO()) as errors):
                forge.return_value.smoke_issues.return_value = []
                forge.return_value.smoke_pull_requests.return_value = []
                forge.return_value.create_issue.return_value = 17
                forge.return_value.default_branch.return_value = "trunk"
                forge.return_value.variables.return_value = {"FOREMAN_HUMAN_LOGIN": HUMAN}
                forge.return_value.smoke_identity.return_value = (True, True, True)
                forge.return_value.load_issue.return_value = {"repo": SMOKE_REPO, "number": 17, "labels": ["smoke", "Done"]}
                forge.return_value.issue_comment_records.return_value = [IssueComment.model_validate(
                    {"id": 1, "user": {"login": BOT_LOGIN}, "body": f"Foreman delivered this.\n\n{STATUS_MARKER}"})]
                yield root, forge.return_value, process, (session, ready, end), credentials, errors

    def test_cli_routes_to_smoke(self):
        with patch('foreman.smoke_command.smoke_command', return_value=78) as command:
            self.assertEqual(main(['smoke']), 78)
            command.assert_called_once_with()

    def test_the_saved_run_decides_with_github_receipts(self):
        with self.provision() as (root, forge, process, (session, ready, end), credentials, errors):
            self.assertEqual(run_smoke(root, time.monotonic() + 30), 0)
            session.assert_called_once_with(HUMAN)
            # The App files the issue without Ready, and Ready comes from the human login.
            forge.create_issue.assert_called_once()
            self.assertEqual(forge.create_issue.call_args.args[-1], ['smoke'])
            ready.assert_called_once_with(SMOKE_REPO, 17)
            store, thread = end.call_args.args[1:3]
            self.assertEqual((store, thread), (root / 'state/foreman/checkpoints.sqlite3', 'bookout-labs/foreman-smoke#17'))
            self.assertEqual(process.call_args_list[0].kwargs['env'], credentials.return_value)
            self.assertIn('refs/pull/2/head', process.call_args_list[-1].args[0])
            forge.smoke_identity.assert_called_once_with(SMOKE_REPO, 2, 'abc', 1)
            for observed in ((False, True, True), (True, False, True), (True, True, False)):
                forge.smoke_identity.return_value = observed
                self.assertEqual(run_smoke(root, time.monotonic() + 30), 1)
            forge.smoke_identity.return_value = (True, True, True)
            # The merged fixture shows only Done and one status comment.
            for labels, comments in ((["smoke", "Doing", "Done"], 1), (["smoke", "Ready"], 1), (["smoke", "Done"], 2)):
                with self.subTest(labels=labels, comments=comments):
                    forge.load_issue.return_value = {"repo": SMOKE_REPO, "number": 17, "labels": labels}
                    forge.issue_comment_records.return_value = forge.issue_comment_records.return_value[:1] * comments
                    self.assertEqual(run_smoke(root, time.monotonic() + 30), 1)
            forge.issue_comment_records.return_value = forge.issue_comment_records.return_value[:1]
            forge.load_issue.return_value = {"repo": SMOKE_REPO, "number": 17, "labels": ["smoke", "Done"]}
            end.return_value = SavedRun({**passing(), 'attempts': [{**passing()['attempts'][0], 'outcome': 'quota'}]}, False, True)
            forge.smoke_identity.reset_mock()
            self.assertEqual(run_smoke(root, time.monotonic() + 30), 78)
            forge.smoke_identity.assert_called_once()
            # A run that halts after its merge receipt shows Stopped.
            end.return_value = SavedRun({**passing(), "halt": "env_unavailable", "stop": "env_unavailable",
                                         "merge": passing()["merge"].model_copy(update={"status": "not_armed"})}, False, True)
            for labels, code in ((["smoke", "Stopped"], 78), (["smoke", "Doing"], 1), (["smoke", "Done"], 1)):
                with self.subTest(labels=labels):
                    forge.load_issue.return_value = {"repo": SMOKE_REPO, "number": 17, "labels": labels}
                    self.assertEqual(run_smoke(root, time.monotonic() + 30), code)
            forge.load_issue.side_effect = ForgeError("GitHub is unavailable.")
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(run_smoke(root, time.monotonic() + 30), 1)
            self.assertIn("smoke exit 1 status.unavailable attempt GitHub issue status", output.getvalue())

    def test_the_only_write_under_the_human_login_adds_ready_to_the_smoke_issue(self):
        # Every token variable gh honors is set, beside the variables Actions sets, which are no token.
        with self.provision() as (root, forge, process, (session, ready, end), credentials, errors), \
                patch.dict(os.environ, {**dict.fromkeys(GH_TOKENS, 'app'), 'GITHUB_ACTIONS': 'true', 'GITHUB_WORKSPACE': str(root)}):
            log = fake_gh(root, HUMAN.upper())
            session.side_effect, ready.side_effect, process.side_effect = real_human_session, real_add_ready, gh_only
            self.assertEqual(run_smoke(root, time.monotonic() + 30), 0)
            calls = gh_calls(log)
        # gh reads its own stored session: no token in Foreman's environment reaches it.
        self.assertEqual([call['tokens'] for call in calls], [[], []])
        self.assertEqual([call['args'] for call in calls], [
            ['api', 'user', '--jq', '.login'],
            ['api', '--method', 'POST', f'repos/{SMOKE_REPO}/issues/17/labels', '-f', 'labels[]=Ready']])
        for login in ('someone-else', None):
            with self.subTest(login=login), self.provision() as (root, forge, process, (session, ready, end), credentials, errors):
                log = fake_gh(root, login)
                session.side_effect, ready.side_effect, process.side_effect = real_human_session, real_add_ready, gh_only
                with redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(run_smoke(root, time.monotonic() + 30), 78)
                self.assertIn('exit 78 bootstrap attempt prerequisite', output.getvalue())
                self.assertIn(f'gh must be signed in as {HUMAN}', errors.getvalue())
                self.assertEqual([call['args'][:2] for call in gh_calls(log)], [['api', 'user']])
                forge.create_issue.assert_not_called()

    def test_the_deadline_reports_the_delivery_that_did_not_come(self):
        waiting = SavedRun({**passing(), 'stop': 'merge_pending'}, True, True)
        for saved, where in ((None, 'the Ready delivery'), (waiting, 'a GitHub delivery')):
            with self.subTest(where=where), self.provision() as (root, forge, process, (session, ready, end), credentials, errors), \
                    redirect_stdout(io.StringIO()) as output:
                end.return_value = saved
                self.assertEqual(run_smoke(root, time.monotonic() + 30), 1)
            self.assertIn(f'smoke exit 1 wall_clock_deadline attempt {where}', output.getvalue())
            forge.smoke_identity.assert_not_called()

    def test_a_smoke_whose_own_code_runs_no_commit_files_nothing(self):
        with self.provision() as (root, forge, process, (session, ready, end), credentials, errors), \
                patch('foreman.smoke_command.release', return_value=''), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(run_smoke(root, time.monotonic() + 30), 78)
        self.assertIn('smoke exit 78 bootstrap attempt prerequisite', output.getvalue())
        self.assertIn("The smoke's own code runs no commit", errors.getvalue())
        forge.create_issue.assert_not_called()
        ready.assert_not_called()

    def test_a_listener_that_does_not_speak_http_is_no_receiver(self):
        with socket.socket() as server:
            server.bind(('127.0.0.1', 0))
            server.listen()
            def answer():
                connection, _ = server.accept()
                with connection:
                    connection.recv(1024)
                    connection.sendall(b'not http\r\n\r\n')
            threading.Thread(target=answer, daemon=True).start()
            with self.provision() as (root, forge, process, (session, ready, end), credentials, errors), \
                    patch('foreman.smoke_command.receiving', receiving), patch('foreman.smoke_command.PORT', server.getsockname()[1]), \
                    redirect_stdout(io.StringIO()) as output:
                self.assertEqual(run_smoke(root, time.monotonic() + 30), 78)
        self.assertIn('smoke exit 78 bootstrap attempt prerequisite', output.getvalue())
        self.assertIn('No receiver answers on 127.0.0.1', errors.getvalue())
        forge.create_issue.assert_not_called()

    def test_a_store_the_smoke_cannot_read_fails_through_its_verdict(self):
        with self.provision() as (root, forge, process, (session, ready, end), credentials, errors), \
                redirect_stdout(io.StringIO()) as output:
            end.side_effect = sqlite3.DatabaseError('file is not a database')
            self.assertEqual(run_smoke(root, time.monotonic() + 30), 1)
        self.assertIn('smoke exit 1 live_run attempt checkpoint', output.getvalue())
        self.assertIn('file is not a database', errors.getvalue())

    def test_cleanup_failure_stops_before_filing_and_reports_error(self):
        with self.provision() as (root, forge, process, (session, ready, end), credentials, errors):
            forge.smoke_pull_requests.side_effect = ForgeError('cleanup denied')
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(run_smoke(root, time.monotonic() + 30), 78)
            self.assertIn('smoke exit 78 cleanup attempt prerequisite', output.getvalue())
            self.assertIn('cleanup denied', errors.getvalue())
            forge.ensure_label.assert_called_once_with(SMOKE_REPO, "smoke", "ededed")
            forge.create_issue.assert_not_called()
            ready.assert_not_called()

    def test_lock_oserror_stops_before_provisioning(self):
        with self.provision() as (root, forge, process, (session, ready, end), credentials, errors):
            with patch('foreman.smoke_command.smoke_lock', side_effect=OSError('lock denied')), \
                 redirect_stdout(io.StringIO()) as output:
                self.assertEqual(run_smoke(root, time.monotonic() + 30), 78)
            self.assertEqual(output.getvalue().splitlines()[0], f'smoke {SMOKE_REPO} exit 78 lock attempt prerequisite')
            self.assertIn('lock denied', errors.getvalue())
            credentials.assert_not_called()
            forge.create_issue.assert_not_called()
            ready.assert_not_called()

    def test_prerequisites_are_unavailable_before_the_issue_and_its_ready(self):
        from foreman.smoke_ownership import HumanSessionMissing
        for failure in ('scope', 'receiver', 'human session', 'ready refused'):
            with self.subTest(failure=failure), self.provision() as (root, forge, process, (session, ready, end), credentials, errors), \
                    patch('foreman.smoke_command.receiving', return_value=failure != 'receiver'):
                if failure == 'scope':
                    forge.default_branch.side_effect = ForgeError('scope denied')
                elif failure == 'human session':
                    session.side_effect = HumanSessionMissing('gh must be signed in as ghost: someone-else')
                else:
                    ready.side_effect = ForgeError('gh could not add Ready')
                with redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(run_smoke(root, time.monotonic() + 30), 78)
                end.assert_not_called()
                if failure == 'ready refused':
                    self.assertIn('exit 78 delivery.ready attempt prerequisite', output.getvalue())
                else:
                    forge.create_issue.assert_not_called()
                    ready.assert_not_called()
                if failure == 'receiver':
                    self.assertIn('No receiver answers on 127.0.0.1', errors.getvalue())

    def test_personal_credentials_rejected_through_real_environment(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'GH_TOKEN': 'personal', 'XDG_STATE_HOME': directory}, clear=True), \
             patch('foreman.smoke_command.LiveForge') as forge, redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(run_smoke(Path(directory), time.monotonic() + 30), 78)
            forge.assert_not_called()

    def test_the_published_pull_request_holds_only_the_issue_file(self):
        with self.provision() as (root, forge, process, (session, ready, end), credentials, errors):
            for numstat, content in (('1\t0\tsmoke_17.txt\n1\t0\tother\n', 'smoke 17\n'), ('1\t0\tsmoke_17.txt\n', 'wrong\n')):
                def output(args, **kwargs):
                    return numstat if args[1:3] == ['diff', '--numstat'] else content if args[1] == 'show' else 'abc'
                with self.subTest(numstat=numstat, content=content), \
                        patch('foreman.smoke_command.subprocess.check_output', side_effect=output), redirect_stdout(io.StringIO()) as out:
                    self.assertEqual(run_smoke(root, time.monotonic() + 30), 1)
                self.assertIn('smoke exit 1 pull_request.file attempt published pull request', out.getvalue())

    def test_published_file_reads_the_real_diff_between_the_base_and_the_published_head(self):
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory)

            def git(*args):
                return subprocess.run(['git', '-c', 'user.name=test', '-c', 'user.email=test@example.com', *args], cwd=checkout,
                                      capture_output=True, text=True, check=True).stdout.strip()

            git('init', '-q')
            git('commit', '-q', '--allow-empty', '-m', 'base')
            base = git('rev-parse', 'HEAD')
            (checkout / 'smoke_17.txt').write_text('smoke 17\n')
            git('add', '.')
            git('commit', '-qm', 'fixture')
            published_file(checkout, 17, base, git('rev-parse', 'HEAD'))
            (checkout / 'other').write_text('other\n')
            git('add', '.')
            git('commit', '-qm', 'more')
            with self.assertRaisesRegex(AssertionError, 'changes more than smoke_17.txt'):
                published_file(checkout, 17, base, git('rev-parse', 'HEAD'))
            with self.assertRaisesRegex(AssertionError, 'requested line'):
                (checkout / 'smoke_17.txt').write_text('wrong\n')
                git('rm', '-q', 'other')
                git('commit', '-qam', 'wrong')
                published_file(checkout, 17, base, git('rev-parse', 'HEAD'))

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
                    out, err = parent.communicate(timeout=15)
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

    def test_the_smoke_says_which_delivery_did_not_come_before_its_group_ends(self):
        # The smoke's own process reaches the deadline, then takes a moment to read the run and say what did not come.
        child = ("import sys, time; time.sleep(max(0, float(sys.argv[2]) - time.monotonic()) + 0.5); "
                 "print('smoke exit 1 wall_clock_deadline attempt a GitHub delivery', flush=True); sys.exit(1)")
        code = ("import subprocess; from unittest.mock import patch; import foreman.smoke_command as m; original=subprocess.Popen; "
                f"launch=lambda args, **kw: original([args[0], '-c', {child!r}, *args[3:]], **kw); "
                "patch('foreman.smoke_command.SMOKE_SECONDS', 0.5).start(); "
                "patch('foreman.process_group.subprocess.Popen', side_effect=launch).start(); raise SystemExit(m.smoke_command())")
        finished = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=20)
        self.assertEqual(finished.returncode, 1, finished.stderr)
        self.assertEqual(finished.stdout, 'smoke exit 1 wall_clock_deadline attempt a GitHub delivery\n')

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



class SmokeDeliveryTest(unittest.TestCase):
    """The smoke's run starts and continues only from GitHub's deliveries, through the host's receiver and accept."""

    def setUp(self):
        from test_runner import confine_host
        confine_host(self)
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.root = root
        # The App is configured, so a foreman start or wake the smoke ran itself would reach the runner.
        self.enterContext(patch.dict(os.environ, {'XDG_RUNTIME_DIR': str(root / 'runtime'), 'FOREMAN_APP_ID': '1'}))
        self.enterContext(patch('foreman.smoke_command.SMOKE_TARGETS', (SMOKE_REPO,)))
        self.enterContext(patch('foreman.smoke_command.credentials.environment', return_value={'PATH': '/app'}))
        self.enterContext(patch('foreman.smoke_command.smoke_lock'))
        self.enterContext(patch('foreman.smoke_command.published_file'))
        real_run, real_output = subprocess.run, subprocess.check_output
        checkout = str(root / 'smoke/checkout')
        def run(args, **kwargs):
            # The smoke's own clone and fetch of the published head; the run's own Git is real.
            if args[0] == 'git' and ('clone' in args or 'fetch' in args and kwargs.get('cwd') == Path(checkout)):
                return subprocess.CompletedProcess(args, 0, b'', b'')
            return real_run(args, **kwargs)
        def output(args, **kwargs):
            return 'abc' if str(kwargs.get('cwd')) == checkout else real_output(args, **kwargs)
        self.enterContext(patch('subprocess.run', side_effect=run))
        self.enterContext(patch('subprocess.check_output', side_effect=output))
        self.errors = self.enterContext(redirect_stderr(io.StringIO()))

    def smoke(self, host, seconds=60):
        (self.root / 'smoke').mkdir(exist_ok=True)
        with host.serving(), redirect_stdout(io.StringIO()) as output:
            code = run_smoke(self.root / 'smoke', time.monotonic() + seconds)
        self.assertEqual(host.errors, [])
        return code, output.getvalue()

    def test_the_ready_delivery_starts_the_run_and_deliveries_alone_wake_it_to_merged(self):
        host = FakeHost(self.root)
        # The receiver's install moves to another commit after the start, and the run keeps the release it started under.
        host.release.side_effect = [COMMIT, 'another'.ljust(40, '0'), 'another'.ljust(40, '0')]
        code, output = self.smoke(host)
        self.assertEqual(code, 0, output)
        self.assertEqual([result['run'].release for result in host.results], [COMMIT] * 3)
        host.release.assert_called_once_with()
        issue = host.fake.created[0]
        self.assertEqual(issue[2], ['smoke'])
        # GitHub delivered the human's Ready, the pending check's suite and the queue's merge, and nothing else moved the run.
        self.assertEqual(host.sent, ['issues', 'check_suite', 'pull_request'])
        self.assertEqual([argv[0] for argv in host.commands], ['start', 'wake', 'wake'])
        self.assertEqual([(result['stop'], result.get('merge').status if result.get('merge') else None) for result in host.results],
                         [('merge_pending', None), ('merge_pending', 'armed'), ('merged', 'merged')])
        self.assertEqual(host.results[0]['start'].human_login, HUMAN)
        self.assertEqual(host.results[0]['attempts'], host.results[-1]['attempts'])
        self.assertIn(f'smoke {SMOKE_REPO} exit 0; work tokens 10;', output)
        # A release tag on exit 78 reads every attempt in the smoke's log (standing order T1).
        self.assertRegex(output, r'\nattempt 1 triage .* ran\nattempt 2 spec_check ')
        self.assertEqual([call['node'] for call in host.handler.calls], ['triage', 'spec_check', 'implement', 'verify', 'review'])
        host.forge.smoke_identity.assert_called_once_with(SMOKE_REPO, 12, host.results[-1]['checks'].verified_head,
                                                          host.results[-1]['review'].review_receipt['id'])

    def test_a_run_that_saved_another_release_or_none_is_unavailable_and_names_both_commits(self):
        for release in ('another'.ljust(40, '0'), ''):
            with self.subTest(release=release):
                self.errors.truncate(0)
                # The queue's merge never comes, so only the run's first records show the release.
                host = FakeHost(self.root / (release or 'none'), release=release, lost=['pull_request'])
                started = time.monotonic()
                code, output = self.smoke(host)
                self.assertEqual(code, 78, output)
                # The smoke stops once a record shows the release and does not wait for its deadline.
                self.assertLess(time.monotonic() - started, 30)
                self.assertIn(f'smoke exit 78 release attempt receiver release target {SMOKE_REPO}', output)
                self.assertIn(f"The receiver's run saved release {release or 'no commit'}, and the smoke runs {COMMIT}.", self.errors.getvalue())
                self.assertEqual(host.commands[0][0], 'start')

    def test_a_run_saved_under_another_schema_is_another_release_and_unavailable(self):
        # A receiver on an older release saves its runs under its own schema, from which no release can be read.
        host = FakeHost(self.root, release='another'.ljust(40, '0'), lost=['pull_request'])
        with patch('foreman.runner.SCHEMA_VERSION', SCHEMA_VERSION - 1):
            code, output = self.smoke(host)
        self.assertEqual(code, 78, output)
        self.assertIn(f'smoke exit 78 release attempt receiver release target {SMOKE_REPO}', output)
        self.assertIn(f"The receiver's run was saved under another schema, so its release is unknown, and the smoke runs {COMMIT}.",
                      self.errors.getvalue())

    def test_a_run_never_woken_by_a_delivery_fails(self):
        # The queue merged before the run first observed it, so the run never waited for a delivery.
        host = FakeHost(self.root, merges_at_arm=True)
        # Each read just before the run saves a record finds the step that record finished, or no run yet before its first step.
        reads, put = [], SqliteSaver.put

        def read_then_put(saver, config, *args):
            if config['configurable']['checkpoint_ns'] == '':
                reads.append(saved_run(host.store, config['configurable']['thread_id']))
            return put(saver, config, *args)
        with patch.object(SqliteSaver, 'put', read_then_put):
            code, output = self.smoke(host)
        self.assertEqual(code, 1, output)
        self.assertEqual([argv[0] for argv in host.commands], ['start'])
        self.assertEqual(host.results[0]['stop'], 'merged')
        self.assertIn('smoke exit 1 delivery.wake attempt never woken', output)
        self.assertEqual(reads[:2], [None, None])
        self.assertEqual([read.waiting for read in reads[2:]], [True] * 6)

    def test_a_delivery_that_does_not_come_fails_the_smoke_at_its_deadline(self):
        for lost, step in (('pull_request', 'armed'), ('issues', None)):
            with self.subTest(lost=lost):
                host = FakeHost(self.root / lost, lost=[lost])
                started = time.monotonic()
                code, output = self.smoke(host, seconds=12)
                self.assertEqual(code, 1, output)
                self.assertGreaterEqual(time.monotonic() - started, 12)
                self.assertLess(time.monotonic() - started, 20)
                if step:
                    self.assertEqual(host.results[-1]['merge'].status, step)
                    self.assertIn('smoke exit 1 wall_clock_deadline attempt a GitHub delivery', output)
                    # The smoke reads the waiting run once, and its own read wakes nothing: it reads again only when the run saves.
                    thread = f'{SMOKE_REPO}#{host.results[-1]["run"].issue}'
                    with patch('foreman.smoke_command.saved_run', wraps=saved_run) as read, \
                            RustNotify([str(host.store.parent)], False, False, 0, False, False) as notify:
                        self.assertTrue(run_end(notify, host.store, thread, time.monotonic() + 4, COMMIT).waiting)
                    self.assertEqual(read.call_count, 1)
                    # A save between the read's statements leaves the read at the save it began on.
                    listed = SqliteSaver.list

                    def list_then_save(saver, *args, **kwargs):
                        yield from listed(saver, *args, **kwargs)
                        with closing(sqlite3.connect(host.store)) as writer, writer:
                            writer.executescript('DELETE FROM writes; DELETE FROM checkpoints;')
                    with patch.object(SqliteSaver, 'list', list_then_save):
                        self.assertEqual(saved_run(host.store, thread).result['merge'].status, step)
                    self.assertIsNone(saved_run(host.store, thread))
                else:
                    self.assertEqual(host.commands, [])
                    self.assertIn('smoke exit 1 wall_clock_deadline attempt the Ready delivery', output)

    def test_a_store_its_saver_has_not_set_up_holds_no_run_until_the_deadline(self):
        # Linux reports the store as soon as the saver opens it, before its tables exist, and between creating each table.
        store = self.root / 'state/foreman/checkpoints.sqlite3'
        store.parent.mkdir(parents=True)
        with RustNotify([str(store.parent)], False, False, 0, False, False) as notify:
            with closing(sqlite3.connect(store)) as connection:
                connection.executescript('PRAGMA journal_mode=WAL; CREATE TABLE checkpoints (thread_id TEXT);')
                started = time.monotonic()
                self.assertIsNone(run_end(notify, store, f'{SMOKE_REPO}#1', started + 3, COMMIT))
        self.assertGreaterEqual(time.monotonic() - started, 3)

    def test_a_host_with_no_live_receiver_is_unavailable(self):
        host = FakeHost(self.root, receiver=False)
        code, output = self.smoke(host)
        self.assertEqual(code, 78, output)
        self.assertIn('exit 78 bootstrap attempt prerequisite', output)
        self.assertEqual((host.fake.created, host.sent, host.commands), ([], [], []))
