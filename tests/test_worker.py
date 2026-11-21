import tests

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from foreman.credentials import CredentialFailure, command_environment, GIT_AUTH, validate_remote
from foreman.worker_auth import environment, git_command, main, REPO
from tests.test_credentials import CredentialFixture


class WorkerTest(unittest.TestCase):
    def invoke(self, args):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = main(args)
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_local_injects_only_environment_and_redacts_both_streams(self):
        token = 'ghs_123_eyJabc.eyJdef.signature_suffix'
        with patch('foreman.worker_auth.credentials.environment', return_value=command_environment(token)) as mint:
            with patch('foreman.worker_auth.subprocess.run') as run:
                run.return_value = subprocess.CompletedProcess([], 0, token, 'Authorization: Bearer other_suffix')
                code, out, err = self.invoke(['gh', 'api', f'repos/{REPO}'])
                self.assertEqual(code, 0)
                self.assertNotIn('suffix', out + err)
                self.assertEqual(run.call_args.kwargs['env']['GH_TOKEN'], token)
                self.assertNotIn(token, str(run.call_args.args))
                self.assertEqual(mint.call_count, 1)

    def test_remote_injects_only_environment_and_redacts_both_streams(self):
        token = 'synthetic_suffix'
        with patch('foreman.worker_auth.subprocess.run') as run:
            run.side_effect = [subprocess.CompletedProcess([], 0, f'password={token}\n\n', ''),
                               subprocess.CompletedProcess([], 0, token, token)]
            code, out, err = self.invoke(['--studio', 'foreman-studio', 'gh', 'pr', 'list'])
            self.assertEqual(code, 0)
            self.assertNotIn(token, out + err)
            self.assertTrue(all(token not in str(call.args) for call in run.call_args_list))
            self.assertIn('-oStrictHostKeyChecking=yes', run.call_args_list[0].args[0])
            self.assertEqual(run.call_args_list[0].args[0][-1], REPO)

    def test_error_messages_distinguish_auth_connection_usage_and_timeout(self):
        for error, message in [(CredentialFailure('App credentials: unauthorized.'), 'unauthorized'),
                               (subprocess.TimeoutExpired('gh', 30, output='secret_suffix'), 'gh pr timed out after 30'),
                               (UnicodeDecodeError('utf8', b'private', 0, 1, 'secret_suffix'), 'could not execute')]:
            with patch('foreman.worker_auth.environment', side_effect=error), patch('foreman.worker_auth.subprocess.run') as run:
                code, out, err = self.invoke(['gh', 'pr', 'create'])
                self.assertEqual(code, 1)
                self.assertEqual(out, '')
                self.assertIn(message, err)
                self.assertNotIn('suffix', err)
                run.assert_not_called()
        for status, detail, expected in [(255, 'ssh failed', 'connectivity'),
                                         (1, 'App credentials: permission denied.', 'permission denied')]:
            with patch('foreman.worker_auth.subprocess.run', return_value=subprocess.CompletedProcess([], status, '', detail)):
                with self.assertRaisesRegex(CredentialFailure, expected):
                    environment('foreman-studio')
        with patch('foreman.worker_auth.credentials.environment', return_value=command_environment('synthetic')):
            with patch('foreman.worker_auth.subprocess.run', side_effect=subprocess.TimeoutExpired('gh', 30)):
                self.assertIn('gh pr timed out after 30', self.invoke(['gh', 'pr', 'list'])[2])

    def test_forced_command_has_fixed_allowlist_and_no_shell_evaluation(self):
        with patch('foreman.worker_auth.credentials.environment', return_value={'GH_TOKEN': 'synthetic'}) as mint:
            for request in ['bookout-labs/other', REPO + '; id', '', REPO + ' extra']:
                with patch.dict(os.environ, SSH_ORIGINAL_COMMAND=request):
                    self.assertEqual(self.invoke(['--serve'])[0], 2)
            mint.assert_not_called()
            with patch.dict(os.environ, SSH_ORIGINAL_COMMAND=REPO):
                self.assertEqual(self.invoke(['--serve']), (0, 'password=synthetic\n\n', ''))

    def test_rejects_local_commands_aliases_extensions_and_options_before_mint(self):
        with patch('foreman.worker_auth.environment') as mint:
            for command in ['status', 'diff', 'log', 'show', 'add', 'commit', 'switch', 'branch', 'rev-parse', 'merge', 'cherry-pick', 'rebase']:
                self.assertEqual(self.invoke(['git', command])[0], 2)
            for command in ['alias', 'extension', 'custom-alias', '--help']:
                self.assertEqual(self.invoke(['gh', command])[0], 2)
            mint.assert_not_called()

    def test_clone_rejects_options_foreign_hosts_and_rewrites_before_mint(self):
        good = f'https://github.com/{REPO}'
        with patch('foreman.worker_auth.environment') as mint:
            for args in [['clone', '-q', 'https://example.invalid/other', 'dir'],
                         ['clone', '--upload-pack=evil', good], ['clone', '-oevil', good],
                         ['clone', 'https://example.invalid/other'], ['clone', good, '-dir']]:
                self.assertEqual(self.invoke(['git', *args])[0], 2)
            mint.assert_not_called()
        with patch('foreman.worker_auth.subprocess.run', return_value=subprocess.CompletedProcess([], 0, 'https://evil.invalid/repo', '')):
            with self.assertRaisesRegex(CredentialFailure, 'Transport requires'):
                git_command(['clone', good])

    def test_push_refusals_and_fully_qualified_branches(self):
        with patch('foreman.worker_auth.environment') as mint:
            for ref in ['+HEAD:main', ':refs/heads/other', 'HEAD:refs/tags/v9', 'refs/tags/v9',
                        'HEAD:main', 'main', 'refs/heads/main', 'HEAD:refs/heads/main', '*',
                        'HEAD:', 'other:branch', 'HEAD:bad..name', 'HEAD:branch/', 'HEAD:bad.lock']:
                self.assertEqual(self.invoke(['git', 'push', 'origin', ref])[0], 2, ref)
            for extra in [['--force', 'HEAD:branch'], ['--delete', 'branch'], ['--tags'], [], ['a', 'b']]:
                self.assertEqual(self.invoke(['git', 'push', 'origin', *extra])[0], 2)
            mint.assert_not_called()
        with patch('foreman.worker_auth.validate_remote', return_value=[f'https://github.com/{REPO}']):
            for ref, expected in [('HEAD:branch', 'HEAD:refs/heads/branch'), ('branch', 'refs/heads/branch:refs/heads/branch'),
                                  ('HEAD:refs/heads/topic/a', 'HEAD:refs/heads/topic/a')]:
                self.assertEqual(git_command(['push', 'origin', ref])[-1], expected)

    def test_environment_drops_ambient_execution_and_transport_controls(self):
        dangerous = {key: 'evil' for key in ['GIT_SSL_NO_VERIFY', 'GIT_EXEC_PATH', 'GIT_ASKPASS', 'GIT_DIR',
                                           'GIT_WORK_TREE', 'HTTPS_PROXY', 'ALL_PROXY', 'GH_DEBUG', 'GIT_CONFIG_COUNT', 'PYTHONPATH']}
        with patch.dict(os.environ, dangerous):
            self.assertFalse(set(dangerous) & command_environment('synthetic').keys())

    def test_real_git_remote_validation_rejects_rewrites_and_all_wrong_push_urls(self):
        with tempfile.TemporaryDirectory() as directory:
            def git(*args):
                return subprocess.run(['git', *args], cwd=directory, check=True, capture_output=True, text=True)
            git('init')
            good = f'https://github.com/{REPO}'
            git('remote', 'add', 'origin', good)
            self.assertEqual(validate_remote(REPO, Path(directory)), [good, good])
            git('config', f'url.https://example.invalid/.pushInsteadOf', 'https://github.com/')
            with self.assertRaises(CredentialFailure):
                validate_remote(REPO, Path(directory))
            git('config', '--unset-all', f'url.https://example.invalid/.pushInsteadOf')
            git('remote', 'set-url', '--add', '--push', 'origin', good)
            git('remote', 'set-url', '--add', '--push', 'origin', good + '-other')
            with self.assertRaises(CredentialFailure):
                validate_remote(REPO, Path(directory))

    def test_foreign_host_gets_no_token_even_with_a_personal_helper(self):
        result = subprocess.run(['git', '-c', 'credential.helper=!echo password=personal', *GIT_AUTH,
                                 'credential', 'fill'], input='protocol=https\nhost=example.invalid\n\n',
                                text=True, capture_output=True, env=command_environment('synthetic'))
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('synthetic', result.stdout + result.stderr)
        self.assertNotIn('personal', result.stdout)


class WorkerExpiryTest(CredentialFixture):
    def test_worker_fetches_current_token_after_one_hour(self):
        with patch('foreman.worker_auth.credentials', self.owner), patch('foreman.worker_auth.subprocess.run') as run:
            run.return_value = subprocess.CompletedProcess([], 0, 'write receipt', '')
            for now in [1000, 4601]:
                self.clock.return_value = now
                with redirect_stdout(StringIO()):
                    self.assertEqual(main(['gh', 'api', '--method', 'POST', f'repos/{REPO}/issues']), 0)
            self.assertEqual(self.http.call_count, 2)

    def test_remote_uses_same_owner_after_one_hour(self):
        with patch('foreman.worker_auth.credentials', self.owner):
            def ssh(args, **kwargs):
                if args[0] == 'gh':
                    return subprocess.CompletedProcess([], 0, 'write receipt', '')
                wire = StringIO()
                with patch.dict(os.environ, SSH_ORIGINAL_COMMAND=REPO), redirect_stdout(wire):
                    self.assertEqual(main(['--serve']), 0)
                return subprocess.CompletedProcess([], 0, wire.getvalue(), '')
            with patch('foreman.worker_auth.subprocess.run', side_effect=ssh):
                for now in [1000, 4601]:
                    self.clock.return_value = now
                    with redirect_stdout(StringIO()):
                        self.assertEqual(main(['--studio', 'foreman-studio', 'gh', 'api', '--method', 'POST', f'repos/{REPO}/issues']), 0)
            self.assertEqual(self.http.call_count, 2)
