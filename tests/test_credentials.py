import tests

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from threading import Event
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
import jwt

from foreman.adapters import Outcome
from foreman.dispatch import DispatchRun
from foreman.credentials import AppCredentials, CredentialFailure, GIT_AUTH, TOKEN_MARGIN
from foreman.github import CommandFailed, LiveForge
from foreman.redaction import redact
from foreman.workspace import LiveWorkspace, WorktreeInspectionFailed


class CredentialFixture(unittest.TestCase):
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
        self.stack.enter_context(patch.dict(os.environ, FOREMAN_APP_ID='1',
                                           FOREMAN_APP_KEY_PATH=str(path), GH_TOKEN='personal'))
        self.clock = self.stack.enter_context(patch('foreman.credentials.time.time', return_value=1000))
        self.http = self.stack.enter_context(patch('foreman.credentials.urlopen'))
        def respond(request, timeout):
            # GitHub names the installation for a repository, then mints that installation's token.
            body = {'id': 2} if request.full_url.endswith('/installation') else {
                'token': 'ghs_' + 'a' * 36,
                'expires_at': datetime.fromtimestamp(self.clock.return_value + 3600, timezone.utc).isoformat()}
            response = MagicMock()
            response.__enter__.return_value.read.return_value = json.dumps(body).encode()
            return response
        self.http.side_effect = self.respond = respond
        self.owner = AppCredentials()


class CredentialsTest(CredentialFixture):
    def test_scoped_refresh_and_concurrent_callers(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            values = list(pool.map(self.owner.environment, ['org/repo'] * 20))
        self.assertEqual(self.http.call_count, 2)
        self.assertEqual(self.http.call_args_list[0].args[0].full_url, 'https://api.github.com/repos/org/repo/installation')
        self.assertEqual(self.http.call_args.args[0].full_url, 'https://api.github.com/app/installations/2/access_tokens')
        self.assertTrue(all(value['GH_TOKEN'] != 'personal' for value in values))
        request = self.http.call_args.args[0]
        self.assertEqual(json.loads(request.data), {'repositories': ['repo']})
        self.assertEqual(jwt.decode(request.headers['Authorization'].split()[1], self.key,
                                    algorithms=['RS256'], options={'verify_exp': False})['iss'], '1')
        self.clock.return_value = 4601 - TOKEN_MARGIN
        self.owner.environment('org/repo')
        self.owner.environment('org/other')
        self.assertEqual(self.http.call_count, 6)
        self.assertEqual(json.loads(self.http.call_args.args[0].data), {'repositories': ['other']})

    def test_fail_closed_and_sanitize_library_failures(self):
        for error, expected in [(TimeoutError('private material'), 'unavailable'),
                                (HTTPError('', 401, 'secret', {}, None), 'unauthorized'),
                                (HTTPError('', 403, 'secret', {}, None), 'permission'),
                                (HTTPError('', 404, 'secret', {}, None), 'not installed on org/repo'),
                                (HTTPError('', 422, 'secret', {}, None), 'scope'),
                                (HTTPError('', 503, 'secret', {}, None), 'unavailable'),
                                (HTTPError('', 403, 'secret', {'Retry-After': '30'}, None), 'rate limit')]:
            self.http.side_effect = error
            with self.assertRaisesRegex(CredentialFailure, expected) as caught:
                self.owner.environment('org/repo')
            self.assertNotIn('private material', str(caught.exception))
        with patch.dict(os.environ, FOREMAN_APP_KEY_PATH=str(Path(os.environ['FOREMAN_APP_KEY_PATH']).with_name('missing.pem'))):
            with self.assertRaisesRegex(CredentialFailure, 'unavailable'):
                self.owner.environment('org/repo')
        with patch.dict(os.environ, FOREMAN_APP_ID='app'):
            with self.assertRaisesRegex(CredentialFailure, 'FOREMAN_APP_ID'):
                self.owner.environment('org/repo')
        with patch.dict(os.environ, FOREMAN_APP_ID=''):
            self.assertIsNone(self.owner.environment('org/repo'))

    def test_gh_command_uses_app_environment_and_preserves_argv(self):
        with patch('foreman.github.credentials', self.owner), patch('foreman.github.subprocess.run') as run:
            run.return_value = subprocess.CompletedProcess([], 0, '{}', '')
            for command in [['gh', 'api', 'repos/org/repo/issues'], ['gh', 'api', 'graphql'],
                            ['gh', 'api', '--method', 'GET', '/repos/org/repo']]:
                LiveForge()._run_command('org/repo', command)
                self.assertEqual(run.call_args.args[0], command)
                self.assertTrue(run.call_args.kwargs['env']['GH_TOKEN'].startswith('ghs_'))
            run.return_value = subprocess.CompletedProcess([], 1, '', 'HTTP 401')
            with self.assertRaisesRegex(CommandFailed, 'unauthorized'):
                LiveForge()._run_command('org/repo', command)

    def test_git_rejects_other_transport_and_uses_run_repository(self):
        with patch('foreman.workspace.credentials', self.owner), patch('foreman.workspace.subprocess.run') as run:
            for url, push in [('git@github.com:org/repo', 'git@github.com:org/repo'),
                              ('https://user:secret@github.com/org/repo', 'https://github.com/org/repo'),
                              ('https://github.com/org/repo', 'https://github.com/org/wrong')]:
                run.side_effect = [subprocess.CompletedProcess([], 0, value.encode(), b'') for value in (url, push, url, push)]
                with self.assertRaises(WorktreeInspectionFailed):
                    LiveWorkspace(Path.cwd(), 'org/repo')._git('fetch', 'origin', 'main')
            run.side_effect = [subprocess.CompletedProcess([], 0, b'https://github.com/org/repo', b'')] * 4
            run.side_effect = [*run.side_effect, subprocess.CompletedProcess([], 0, b'head', b''),
                               subprocess.CompletedProcess([], 0, b'head', b'')]
            workspace = LiveWorkspace(Path.cwd(), 'org/repo')
            self.assertEqual(workspace._git('fetch', 'origin', 'main'), b'head')
            self.assertEqual(workspace._git('ls-remote', 'origin'), b'head')
            command = run.call_args.args[0]
            self.assertEqual(command[1:1 + len(GIT_AUTH)], list(GIT_AUTH))
            self.assertNotIn('ghs_', ' '.join(command))
            self.assertIn('protocol.allow=never', command)
            self.assertIn('protocol.https.allow=always', command)
            self.assertEqual(run.call_args.kwargs['env']['GIT_ALLOW_PROTOCOL'], 'https')
            self.assertEqual(json.loads(self.http.call_args.args[0].data), {'repositories': ['repo']})

    def test_git_error_classification_and_invalidation(self):
        with patch('foreman.workspace.credentials', self.owner), patch('foreman.workspace.subprocess.run') as run:
            workspace = LiveWorkspace(Path.cwd(), 'org/repo')
            workspace._urls = ['https://github.com/org/repo']
            with patch.object(self.owner, 'invalidate') as invalidate:
                for detail, expected in [('Authentication failed', 'unauthorized'), ('The requested URL returned error: 403', 'permission'),
                                         ('The requested URL returned error: 429', 'rate limit'), ('non-fast-forward', 'non-fast-forward')]:
                    invalidate.reset_mock()
                    run.return_value = subprocess.CompletedProcess([], 1, b'', detail.encode())
                    with self.assertRaisesRegex(WorktreeInspectionFailed, expected):
                        workspace._git('push', 'origin', 'main')
                    self.assertEqual(invalidate.call_count, int(expected == 'unauthorized'))

    def test_prepare_fetch_uses_app_transport_and_stops_on_auth_or_timeout(self):
        for error, detail in ((subprocess.CompletedProcess([], 1, b'', b'Authentication failed'), 'unauthorized'),
                              (subprocess.TimeoutExpired('git fetch', 30), 'timed out')):
            with self.subTest(detail=detail), patch('foreman.workspace.credentials', self.owner), \
                 patch('foreman.workspace.subprocess.run') as run:
                workspace = LiveWorkspace(Path.cwd(), 'org/repo')
                run.side_effect = [subprocess.CompletedProcess([], 0, b'', b''),
                                   *[subprocess.CompletedProcess([], 0, b'https://github.com/org/repo', b'')] * 4,
                                   error]
                with self.assertRaisesRegex(WorktreeInspectionFailed, detail):
                    workspace.create_branch('delivery', 'main')
                command = run.call_args.args[0]
                self.assertIn('credential.helper=', command)
                self.assertEqual(command[-6:], ['fetch', '--no-tags', '--no-recurse-submodules', '--refmap=',
                                                'origin', '+refs/heads/main:refs/remotes/origin/main'])
                self.assertTrue(run.call_args.kwargs['env']['GH_TOKEN'].startswith('ghs_'))
                self.assertFalse(any('switch' in call.args[0] for call in run.call_args_list))

    def test_redaction_removes_entire_app_secrets(self):
        secrets = ['ghs_' + 'a' * 36, 'ghs_123_eyJabc.eyJdef.signature_suffix',
                   'eyJabc.eyJdef.signature_suffix', 'Authorization: Basic c3ludGhldGljOnNlY3JldA==',
                   'Authorization: Bearer opaque_suffix', 'https://user:p%40ss@github.com/org/repo',
                   '-----BEGIN PRIVATE KEY-----\nsynthetic suffix\n-----END PRIVATE KEY-----']
        names = ['legacy installation', 'stateless installation', 'App JWT', 'basic header', 'bearer header', 'encoded HTTPS password', 'PEM']
        for name, secret in zip(names, secrets):
            with self.subTest(shape=name):
                outcome = DispatchRun.clean(Outcome("ran", output={"detail": secret}, detail=secret))
                self.assertNotIn(secret, json.dumps(outcome.output))
                self.assertNotIn(secret, outcome.detail)
                clean = redact(secret)
                self.assertNotIn('suffix', clean)
                self.assertNotIn('c3lud', clean)
                self.assertNotIn('p%40ss', clean)
                self.assertNotEqual(secret, clean)
                with patch('foreman.github.credentials', self.owner), patch('foreman.github.subprocess.run') as run:
                    run.return_value = subprocess.CompletedProcess([], 1, '', secret)
                    with self.assertRaises(CommandFailed) as caught:
                        LiveForge()._run_command('org/repo', ['gh', 'api', 'repos/org/repo/issues'])
                    self.assertNotIn(secret, str(caught.exception))

    def test_helper_overrides_cached_personal_credentials(self):
        result = subprocess.run(['git', '-c', 'credential.helper=!echo password=personal', *GIT_AUTH,
                                 'credential', 'fill'], input='protocol=https\nhost=github.com\n\n',
                                text=True, capture_output=True, env={**os.environ, 'GH_TOKEN': 'synthetic'})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('password=synthetic', result.stdout)
        self.assertNotIn('personal', result.stdout)

    def test_quoted_auth_header_and_nested_strings_preserve_structure(self):
        text = 'Run: curl -H "Authorization: Bearer abc123" https://api.github.com'
        output = {'spec': text, 'nested': [text, {'pem': '-----BEGIN PRIVATE KEY-----\nkey\n-----END PRIVATE KEY-----'}], 'number': 3}
        outcome = DispatchRun.clean(Outcome('ran', output=output))
        self.assertEqual(outcome.output['spec'], 'Run: curl -H "[redacted:Credential]" https://api.github.com')
        self.assertNotIn('abc123', str(outcome.output))
        self.assertNotIn('BEGIN PRIVATE KEY', str(outcome.output))
        self.assertEqual(outcome.output['number'], 3)

    def test_command_stderr_keeps_shas(self):
        detail = 'expected head 3f786850e387550fdab836ed7e6dc881de23001b got a94a8fe5ccb19ba61c4c0873d391e987982fbbd3'
        with patch.dict(os.environ, FOREMAN_APP_ID=''), patch('subprocess.run') as run:
            for operation in [lambda: LiveForge()._run_command('org/repo', ['gh', 'api', 'repos/org/repo']),
                              lambda: LiveWorkspace(Path.cwd())._git('ls-remote', 'origin')]:
                run.return_value = subprocess.CompletedProcess([], 1, '', detail)
                with self.assertRaisesRegex(RuntimeError, detail):
                    operation()

    def test_cached_repo_does_not_wait_for_another_repos_mint(self):
        self.owner.environment('org/repo')
        entered, release = Event(), Event()
        def blocked(*args, **kwargs):
            entered.set()
            release.wait(2)
            return self.respond(*args, **kwargs)
        with patch('foreman.credentials.urlopen', side_effect=blocked), ThreadPoolExecutor(max_workers=2) as pool:
            mint = pool.submit(self.owner.environment, 'org/other')
            self.assertTrue(entered.wait(1))
            try:
                self.assertIsNotNone(pool.submit(self.owner.environment, 'org/repo').result(timeout=1))
            finally:
                release.set()
            mint.result(timeout=1)
