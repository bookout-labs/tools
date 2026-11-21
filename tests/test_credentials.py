import tests

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
import jwt

from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.sqlite import SqliteSaver

from foreman.adapters import Outcome
from foreman.dispatch import FailoverDispatcher
from foreman.credentials import AppCredentials, CredentialFailure, GIT_AUTH, github_repo
from foreman.github import CommandFailed, LiveForge
from foreman.redaction import redact
from foreman.workspace import LiveWorkspace, WorktreeInspectionFailed


class CredentialsTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.key = key.public_key()
        path = Path(directory) / 'key.pem'
        path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))
        path.chmod(0o600)
        self.stack.enter_context(patch.dict(os.environ, FOREMAN_APP_ID='1', FOREMAN_INSTALLATION_ID='2',
                                           FOREMAN_APP_KEY_PATH=str(path), GH_TOKEN='personal'))
        self.clock = self.stack.enter_context(patch('foreman.credentials.time.time', return_value=1000))
        self.http = self.stack.enter_context(patch('foreman.credentials.urlopen'))
        self.http.return_value.__enter__.return_value.read.side_effect = lambda: json.dumps({
            'token': 'ghs_' + 'a' * 36,
            'expires_at': datetime.fromtimestamp(self.clock.return_value + 3600, timezone.utc).isoformat(),
        }).encode()
        self.owner = AppCredentials()

    def test_scoped_refresh_and_concurrent_callers(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            values = list(pool.map(self.owner.environment, ['org/repo'] * 20))
        self.assertEqual(self.http.call_count, 1)
        self.assertTrue(all(value['GH_TOKEN'] != 'personal' for value in values))
        request = self.http.call_args.args[0]
        self.assertEqual(json.loads(request.data), {'repositories': ['repo']})
        self.assertEqual(jwt.decode(request.headers['Authorization'].split()[1], self.key,
                                    algorithms=['RS256'], options={'verify_exp': False})['iss'], '1')
        self.clock.return_value = 4541
        self.owner.environment('org/repo')
        self.owner.environment('org/other')
        self.assertEqual(self.http.call_count, 3)

    def test_fail_closed_and_sanitize_library_failures(self):
        for error, expected in [(TimeoutError('private material'), 'unavailable'),
                                (HTTPError('', 401, 'secret', {}, None), 'unauthorized'),
                                (HTTPError('', 403, 'secret', {}, None), 'permission'),
                                (HTTPError('', 403, 'secret', {'Retry-After': '30'}, None), 'rate limit')]:
            self.http.side_effect = error
            with self.assertRaisesRegex(CredentialFailure, expected) as caught:
                self.owner.environment('org/repo')
            self.assertNotIn('private material', str(caught.exception))
        with patch.dict(os.environ, FOREMAN_APP_KEY_PATH=str(Path(os.environ['FOREMAN_APP_KEY_PATH']).with_name('missing.pem'))):
            with self.assertRaisesRegex(CredentialFailure, 'unavailable'):
                self.owner.environment('org/repo')
        with patch.dict(os.environ, FOREMAN_APP_ID=''):
            with self.assertRaises(CredentialFailure):
                self.owner.environment('org/repo')
        with patch.dict(os.environ, FOREMAN_APP_ID='', FOREMAN_INSTALLATION_ID=''):
            self.assertIsNone(self.owner.environment('org/repo'))

    def test_gh_command_uses_app_environment_and_preserves_argv(self):
        with patch('foreman.github.credentials', self.owner), patch('foreman.github.subprocess.run') as run:
            run.return_value = subprocess.CompletedProcess([], 0, '{}', '')
            command = ['gh', 'api', 'repos/org/repo/issues']
            LiveForge()._run_command(command)
            self.assertEqual(run.call_args.args[0], command)
            self.assertTrue(run.call_args.kwargs['env']['GH_TOKEN'].startswith('ghs_'))
            run.return_value = subprocess.CompletedProcess([], 1, '', 'HTTP 401')
            with self.assertRaisesRegex(CommandFailed, 'unauthorized'):
                LiveForge()._run_command(command)

    def test_git_rejects_other_transport_and_uses_command_helper(self):
        workspace = LiveWorkspace(Path.cwd())
        for url in ['git@github.com:org/repo', 'https://user:secret@github.com/org/repo', 'https://evil/org/repo']:
            with self.assertRaises(CredentialFailure):
                github_repo(url)
        self.assertEqual(github_repo('https://github.com/org/repo.git'), 'org/repo')
        with patch('foreman.workspace.credentials', self.owner), patch('foreman.workspace.subprocess.run') as run:
            run.side_effect = [subprocess.CompletedProcess([], 0, b'https://github.com/org/repo\n', b''),
                               subprocess.CompletedProcess([], 0, b'https://github.com/org/repo\n', b''),
                               subprocess.CompletedProcess([], 0, b'head', b'')]
            self.assertEqual(workspace._git('fetch', 'origin', 'main'), b'head')
            command = run.call_args.args[0]
            self.assertEqual(command[1:1 + len(GIT_AUTH)], list(GIT_AUTH))
            self.assertIn('origin', command)
            self.assertNotIn('ghs_', ' '.join(command))
            run.side_effect = [subprocess.CompletedProcess([], 0, b'https://github.com/org/repo', b''),
                               subprocess.CompletedProcess([], 0, b'git@github.com:org/repo', b'')]
            with self.assertRaises(WorktreeInspectionFailed):
                workspace._git('push', 'origin', 'main')

    def test_redaction_removes_entire_app_secrets(self):
        secrets = ['ghs_' + 'a' * 36, 'ghs_123_eyJabc.eyJdef.signature_suffix',
                   'eyJabc.eyJdef.signature_suffix', 'Authorization: Basic c3ludGhldGljOnNlY3JldA==',
                   'Authorization: Bearer opaque_suffix', 'https://user:p%40ss@github.com/org/repo',
                   '-----BEGIN PRIVATE KEY-----\nsynthetic suffix\n-----END PRIVATE KEY-----']
        for secret in secrets:
            with self.subTest(secret=secret):
                outcome, _ = FailoverDispatcher(lambda *args: Outcome("ran", output={"detail": secret}, detail=secret)).run("review", None, {}, "")
                self.assertNotIn(secret, json.dumps(outcome.output))
                self.assertNotIn(secret, outcome.detail)
                with SqliteSaver.from_conn_string(":memory:") as saver:
                    checkpoint = empty_checkpoint()
                    checkpoint["channel_values"] = outcome.output
                    config = saver.put({"configurable": {"thread_id": "redaction", "checkpoint_ns": ""}}, checkpoint, {}, {})
                    self.assertNotIn(secret, str(saver.get(config)))
                clean = redact(secret)
                self.assertNotIn('suffix', clean)
                self.assertNotIn('c3lud', clean)
                self.assertNotIn('p%40ss', clean)
                self.assertNotEqual(secret, clean)
                with patch('foreman.github.credentials', self.owner), patch('foreman.github.subprocess.run') as run:
                    run.return_value = subprocess.CompletedProcess([], 1, '', secret)
                    with self.assertRaises(CommandFailed) as caught:
                        LiveForge()._run_command(['gh', 'api', 'repos/org/repo/issues'])
                    self.assertNotIn(secret, str(caught.exception))

    def test_helper_overrides_cached_personal_credentials(self):
        result = subprocess.run(['git', '-c', 'credential.helper=!echo password=personal', *GIT_AUTH,
                                 'credential', 'fill'], input='protocol=https\nhost=github.com\n\n',
                                text=True, capture_output=True, env={**os.environ, 'GH_TOKEN': 'synthetic'})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('password=synthetic', result.stdout)
        self.assertNotIn('personal', result.stdout)
