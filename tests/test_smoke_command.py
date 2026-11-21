"""Offline command proofs at the live runner and GitHub seams."""
import tests

from contextlib import redirect_stdout
from copy import deepcopy
import io
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from foreman.cli import main
from foreman.github import BOT_LOGIN, ForgeError, LiveForge
from foreman.smoke_command import FIXTURE_CHECK, SMOKE_REPO, SMOKE_TOKEN_CAP, SMOKE_CLAUDE_USD, run_smoke, smoke_command
from tests.test_smoke import passing


class SmokeCommandTest(unittest.TestCase):
    def test_cli_routes_to_smoke(self):
        with patch('foreman.smoke_command.smoke_command', return_value=78) as command:
            self.assertEqual(main(['smoke']), 78)
            command.assert_called_once_with()

    def test_live_runner_and_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (patch('foreman.smoke_command.credentials.environment', return_value={'PATH': ''}),
                  patch('foreman.smoke_command.LiveForge') as forge,
                  patch('foreman.smoke_command.tempfile.mkdtemp', return_value=directory),
                  patch('foreman.smoke_command.subprocess.run') as process,
                  patch('foreman.smoke_command.subprocess.check_output', return_value='base\n'),
                  patch('foreman.smoke_command.run_live_delivery', return_value=passing()) as runner,
                  patch.dict(os.environ), redirect_stdout(io.StringIO())):
                forge.return_value.create_issue.return_value = 17
                forge.return_value.smoke_identity.return_value = ''
                self.assertEqual(run_smoke(), 0)
                kwargs = runner.call_args.kwargs
                self.assertEqual((kwargs['repo'], kwargs['issue']), (SMOKE_REPO, 17))
                self.assertIn('smoke_17.txt', kwargs['request'])
                self.assertEqual(kwargs['max_tokens'], SMOKE_TOKEN_CAP)
                self.assertEqual(kwargs['max_budget_usd'], SMOKE_CLAUDE_USD)
                self.assertEqual(kwargs['run'].workspace.root, root / 'checkout')
                self.assertIs(kwargs['run'].forge, forge.return_value)
                self.assertTrue(os.environ['PATH'].startswith(str(root / 'python/bin')))
                self.assertIn('python -m unittest', kwargs['check'])
                self.assertFalse('watch_repo' in kwargs)
                forge.return_value.smoke_identity.assert_called_once_with(SMOKE_REPO, 2, 'abc', 1)
                self.assertEqual(process.call_args_list[1].args[0][:3], ['python3.12', '-m', 'venv'])
                self.assertTrue((root / 'acceptance.py').is_file())
                for state, expected in ((passing(), 1), ({**passing(), 'attempts': [{**passing()['attempts'][0], 'outcome': 'quota'}]}, 78)):
                    runner.return_value = state
                    forge.return_value.smoke_identity.return_value = 'identity.commit' if expected == 1 else ''
                    self.assertEqual(run_smoke(), expected)
                forge.return_value.smoke_identity.return_value = 'identity.commit'
                self.assertEqual(run_smoke(), 1)
                forge.return_value.smoke_identity.side_effect = ForgeError('missing receipt')
                runner.return_value = passing()
                self.assertEqual(run_smoke(), 1)

    def test_prerequisites_fail_before_issue_and_runner(self):
        for env, failure in ((None, None), ({'PATH': ''}, ForgeError('scope denied')), ({'PATH': ''}, OSError('no Python'))):
            with (patch('foreman.smoke_command.credentials.environment', return_value=env),
                  patch('foreman.smoke_command.LiveForge') as forge,
                  patch('foreman.smoke_command.subprocess.run', side_effect=failure),
                  patch('foreman.smoke_command.run_live_delivery') as runner,
                  patch('foreman.smoke_command.tempfile.mkdtemp', return_value=tempfile.gettempdir()),
                  redirect_stdout(io.StringIO())):
                if isinstance(failure, ForgeError):
                    forge.return_value.default_branch.side_effect = failure
                self.assertEqual(run_smoke(), 78)
                forge.return_value.create_issue.assert_not_called()
                runner.assert_not_called()

    def test_repeatable_fixture_rejects_noop_and_unrelated_change(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def git(*args):
                return subprocess.check_output(['git', *args], cwd=root, stderr=subprocess.DEVNULL).decode().strip()
            git('init')
            git('-c', 'user.name=test', '-c', 'user.email=test@example.com', 'commit', '--allow-empty', '-m', 'base')
            script = root.parent / (root.name + '-acceptance.py')
            script.write_text(FIXTURE_CHECK)
            for number in ('17', '18'):
                base = git('rev-parse', 'HEAD')
                command = ['python', str(script), number, base]
                self.assertNotEqual(subprocess.run(command, cwd=root, capture_output=True).returncode, 0)
                (root / f'smoke_{number}.txt').write_text(f'smoke {number}\n')
                git('add', '.')
                git('-c', 'user.name=test', '-c', 'user.email=test@example.com', 'commit', '-m', number)
                self.assertEqual(subprocess.run(command, cwd=root, capture_output=True).returncode, 0)
                (root / 'other').write_text('unrelated ' + number)
                git('add', '.')
                git('-c', 'user.name=test', '-c', 'user.email=test@example.com', 'commit', '-m', 'unrelated')
                self.assertNotEqual(subprocess.run(command, cwd=root, capture_output=True).returncode, 0)

    def test_real_check_uses_provisioned_path_without_host_python(self):
        from foreman.workspace import LiveWorkspace
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / 'checkout'
            checkout.mkdir()
            bin_dir = root / 'python/bin'
            bin_dir.mkdir(parents=True)
            (bin_dir / 'python').symlink_to(sys.executable)
            lines = []
            with patch.dict(os.environ, {'PATH': str(bin_dir)}):
                self.assertEqual(LiveWorkspace(checkout).run(
                    "python -c 'import sys; print(sys.version_info[:2])'", log=lines.append), 0)
            self.assertIn('(3, 12)', ''.join(lines))
            self.assertEqual(list(checkout.iterdir()), [])

    def test_deadline_kills_hung_child_process(self):
        with tempfile.TemporaryDirectory() as directory:
            receipt = Path(directory) / 'pid'
            def hang():
                child = subprocess.Popen(['python', '-c', 'import time; time.sleep(60)'])
                receipt.write_text(str(child.pid))
                time.sleep(60)
                return 0
            with patch('foreman.smoke_command.run_smoke', side_effect=hang), patch('foreman.smoke_command.SMOKE_SECONDS', 0.5):
                self.assertEqual(smoke_command(), 1)
            pid = int(receipt.read_text())
            for _ in range(50):
                process = subprocess.run(['ps', '-p', str(pid), '-o', 'stat='], capture_output=True, text=True)
                if process.returncode or 'Z' in process.stdout:
                    break
                time.sleep(0.02)
            else:
                os.kill(pid, signal.SIGKILL)
                self.fail('owned child survived deadline')

    def test_observed_identity_receipts(self):
        user = {'login': BOT_LOGIN}
        actor = {'name': BOT_LOGIN, 'email': '359513816+' + BOT_LOGIN + '@users.noreply.github.com'}
        receipts = [{'user': user, 'head': {'sha': 'abc'}},
                    {'sha': 'abc', 'author': user, 'committer': user, 'commit': {'author': actor, 'committer': actor}},
                    {'user': user, 'state': 'COMMENTED', 'commit_id': 'abc'}]
        variants = [(None, '')]
        for index, field, value in ((0, 'user', {'login': 'personal'}), (1, 'committer', {'login': 'personal'}),
                                    (1, 'author', None), (2, 'commit_id', 'old'), (2, 'state', 'APPROVED')):
            changed = deepcopy(receipts)
            changed[index][field] = value
            variants.append((changed, ('identity.pr', 'identity.commit', 'identity.review')[index]))
        changed = deepcopy(receipts)
        changed[1]['commit']['committer']['email'] = 'personal@example.com'
        variants.append((changed, 'identity.commit'))
        changed = deepcopy(receipts)
        changed[2]['user'] = {'login': 'personal'}
        variants.append((changed, 'identity.review'))
        for changed, expected in variants:
            forge = LiveForge()
            def read(repo, model, command):
                data = (changed or receipts)[0 if '/commits/' not in command[-1] and '/reviews/' not in command[-1] else 1 if '/commits/' in command[-1] else 2]
                return model.model_validate(data)
            with patch.object(forge, '_read', side_effect=read), patch.object(forge, 'bot_identity', return_value={'GIT_AUTHOR_EMAIL': actor['email']}):
                self.assertEqual(forge.smoke_identity(SMOKE_REPO, 2, 'abc', 1), expected)
