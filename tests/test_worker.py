import tests

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import os
import subprocess
import unittest
from unittest.mock import patch

from foreman.credentials import CredentialFailure, GIT_AUTH
from foreman.worker_auth import environment, git_command, main
from tests import test_credentials


class WorkerTest(unittest.TestCase):
    def invoke(self, args):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(args)
        return code, out.getvalue(), err.getvalue()

    def test_local_and_remote_inject_only_environment_and_redact_both_streams(self):
        token = 'ghs_123_eyJabc.eyJdef.signature_suffix'
        with patch('foreman.worker_auth.credentials.environment', return_value={**os.environ, 'GH_TOKEN': token}) as mint:
            with patch('foreman.worker_auth.subprocess.run') as run:
                run.return_value = subprocess.CompletedProcess([], 0, token, 'Authorization: Bearer other_suffix')
                code, out, err = self.invoke(['gh', 'api', 'repos/bookout-labs/tools'])
                self.assertEqual(code, 0)
                self.assertNotIn('suffix', out + err)
                self.assertEqual(run.call_args.kwargs['env']['GH_TOKEN'], token)
                self.assertNotIn(token, str(run.call_args.args))
                self.assertEqual(mint.call_count, 1)
                run.reset_mock()
                run.side_effect = [subprocess.CompletedProcess([], 0, f'password={token}\n\n', ''),
                                   subprocess.CompletedProcess([], 0, token, token)]
                code, out, err = self.invoke(['--studio', 'foreman-studio', 'gh', 'pr', 'list'])
                self.assertEqual(code, 0)
                self.assertNotIn(token, out + err)
                self.assertTrue(all(token not in str(call.args) for call in run.call_args_list))
                self.assertIn('-oStrictHostKeyChecking=yes', run.call_args_list[0].args[0])
                self.assertEqual(run.call_args_list[0].args[0][-1], 'bookout-labs/tools')

    def test_failed_mint_or_ssh_never_starts_worker_or_emits_details(self):
        for error in [CredentialFailure('secret_suffix'), subprocess.TimeoutExpired('ssh', 30, output='secret_suffix')]:
            with patch('foreman.worker_auth.environment', side_effect=error), patch('foreman.worker_auth.subprocess.run') as run:
                code, out, err = self.invoke(['gh', 'pr', 'create'])
                self.assertEqual(code, 1)
                self.assertEqual(out, '')
                self.assertNotIn('suffix', err)
                run.assert_not_called()
        with patch('foreman.worker_auth.subprocess.run', return_value=subprocess.CompletedProcess([], 1, 'secret_suffix', 'secret_suffix')):
            with self.assertRaises(CredentialFailure):
                environment('bookout-labs/tools', 'foreman-studio')
        with self.assertRaises(CredentialFailure):
            environment('bookout-labs/foreman-smoke', None)

    def test_forced_command_has_fixed_allowlist_and_no_shell_evaluation(self):
        with patch('foreman.worker_auth.credentials.environment', return_value={'GH_TOKEN': 'synthetic'}) as mint:
            for request in ['bookout-labs/other', 'bookout-labs/tools; id', '', 'bookout-labs/tools extra']:
                with patch.dict(os.environ, SSH_ORIGINAL_COMMAND=request):
                    self.assertEqual(self.invoke(['--serve'])[0], 1)
            mint.assert_not_called()
            with patch.dict(os.environ, SSH_ORIGINAL_COMMAND='bookout-labs/tools'):
                self.assertEqual(self.invoke(['--serve']), (0, 'password=synthetic\n\n', ''))

    def test_network_git_rejects_wrong_remotes_and_argument_overrides(self):
        repo = 'bookout-labs/tools'
        good = f'https://github.com/{repo}'
        for bad in ['git@github.com:bookout-labs/tools', good + '-other', 'https://user:password@github.com/' + repo]:
            with patch('foreman.worker_auth.subprocess.run') as run:
                run.return_value = subprocess.CompletedProcess([], 0, bad + '\n', '')
                with self.assertRaises(CredentialFailure):
                    git_command(repo, ['push', 'origin', 'HEAD:branch'], {})
        for args in [['push', good], ['push', 'origin', '--repo=other'], ['-c', 'credential.helper=other'], ['credential', 'fill']]:
            with self.assertRaises(CredentialFailure):
                git_command(repo, args, {})
        with patch('foreman.worker_auth.subprocess.run') as run:
            run.return_value = subprocess.CompletedProcess([], 0, good + '\n', '')
            command = git_command(repo, ['push', 'origin', 'HEAD:branch'], {})
            self.assertEqual(command[1:1 + len(GIT_AUTH)], list(GIT_AUTH))


class WorkerExpiryTest(unittest.TestCase):
    setUp = test_credentials.CredentialsTest.setUp

    def test_worker_fetches_current_token_after_one_hour(self):
        with patch('foreman.worker_auth.credentials', self.owner), patch('foreman.worker_auth.subprocess.run') as run:
            run.return_value = subprocess.CompletedProcess([], 0, 'write receipt', '')
            for now in [1000, 4601]:
                self.clock.return_value = now
                with redirect_stdout(StringIO()):
                    self.assertEqual(main(['gh', 'api', '--method', 'POST', 'repos/bookout-labs/tools/issues']), 0)
            self.assertEqual(self.http.call_count, 2)
            self.assertEqual(run.call_count, 2)

    def test_remote_uses_same_owner_after_one_hour(self):
        with patch('foreman.worker_auth.credentials', self.owner):
            def ssh(args, **kwargs):
                if args[0] == 'gh':
                    return subprocess.CompletedProcess([], 0, 'write receipt', '')
                wire = StringIO()
                with patch.dict(os.environ, SSH_ORIGINAL_COMMAND='bookout-labs/tools'), redirect_stdout(wire):
                    self.assertEqual(main(['--serve']), 0)
                return subprocess.CompletedProcess([], 0, wire.getvalue(), '')
            with patch('foreman.worker_auth.subprocess.run', side_effect=ssh):
                for now in [1000, 4601]:
                    self.clock.return_value = now
                    with redirect_stdout(StringIO()):
                        self.assertEqual(main(['--studio', 'foreman-studio', 'gh', 'api', '--method', 'POST',
                                               'repos/bookout-labs/tools/issues']), 0)
            self.assertEqual(self.http.call_count, 2)
