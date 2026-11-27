import tests

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import os
from pathlib import Path
import subprocess
import socket
import tempfile
import unittest
from unittest.mock import patch

from foreman.credentials import CredentialFailure, command_environment, GIT_AUTH, validate_remote
from foreman.worker_auth import environment, git_command, main, REPO, validate_config
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
                code, out, err = self.invoke(['gh', 'issue', 'list'])
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
            for command in ['alias', 'extension', 'custom-alias', '--help', 'api']:
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
        with patch('foreman.worker_auth.subprocess.run', side_effect=[
                subprocess.CompletedProcess([], 0, '', ''),
                subprocess.CompletedProcess([], 0, 'https://evil.invalid/repo', '')]):
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
        with patch('foreman.worker_auth.validate_remote', return_value=[f'https://github.com/{REPO}']), \
                patch('foreman.worker_auth.validate_config'):
            for ref, expected in [('HEAD:branch', 'HEAD:refs/heads/branch'), ('branch', 'refs/heads/branch:refs/heads/branch'),
                                  ('HEAD:refs/heads/topic/a', 'HEAD:refs/heads/topic/a')]:
                self.assertEqual(git_command(['push', 'origin', ref])[-1], expected)

    def test_repository_transport_config_is_refused_before_mint(self):
        good = f'https://github.com/{REPO}'
        probes = [
            [('core.fsmonitor', 'probe')],
            [('core.sshCommand', 'probe')],
            [('http.https://github.com/.proxy', 'http://127.0.0.1:9')],
            [('http.https://github.com/.sslVerify', 'false')],
            [('remote.origin.proxy', 'http://127.0.0.1:9')],
            [('http.sslCAInfo', '/synthetic/ca.pem'), ('http.curloptResolve', 'github.com:443:127.0.0.1')],
            [('http.curloptResolve', 'github.com:443:127.0.0.1')],
            [('remote.other.url', good + '-other')],
            [('remote.origin.url', good + '-other.git')],
            [('remote.origin.url', good + '.git.evil')],
            [('remote.origin.url', 'https://github.com/evil/tools.git')],
            [('remote.origin.pushurl', good + '.git')],
            [('url.http://127.0.0.1:9/.insteadOf', good)],
            [('url.http://127.0.0.1:9/.pushInsteadOf', good)],
        ]
        for mode in ['local', 'include', 'includeIf', 'worktree']:
            for settings in probes:
                with self.subTest(mode=mode, key=settings[0][0]), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory).resolve()
                    real_run = subprocess.run

                    def git(*args):
                        return real_run(['git', *args], cwd=root, check=True, capture_output=True, text=True)

                    git('init')
                    git('remote', 'add', 'origin', good)
                    config = root / '.git' / 'config'
                    rejected = settings[0][0]
                    if mode in {'include', 'includeIf'}:
                        config = root / 'included.config'
                        key = 'include.path' if mode == 'include' else f'includeIf.gitdir:{root}/.git.path'
                        git('config', key, str(config))
                        rejected = key
                    elif mode == 'worktree':
                        git('config', 'extensions.worktreeConfig', 'true')
                        rejected = 'extensions.worktreeconfig'
                        config = root / '.git' / 'config.worktree'

                    def run(command, **kwargs):
                        self.assertEqual(command[:2], ['git', 'config'])
                        self.assertNotIn('GH_TOKEN', kwargs['env'])
                        return real_run(command, cwd=root, **kwargs)

                    probe = root / 'probe'
                    marker = root / 'executed'
                    probe.write_text(f'#!/bin/sh\ntouch "{marker}"\n')
                    probe.chmod(0o755)
                    with patch('foreman.worker_auth.subprocess.run', side_effect=run), \
                            patch('foreman.worker_auth.environment', return_value=command_environment('synthetic')) as mint:
                        if mode == 'local':
                            validate_config(good)
                        for key, value in settings:
                            git('config', '--file', str(config), key, str(probe) if value == 'probe' else value)
                        code, out, err = self.invoke(['git', 'ls-remote', 'origin'])
                        self.assertEqual(code, 1)
                        self.assertEqual(out, '')
                        self.assertIn(rejected.lower(), err.lower())
                        self.assertFalse(marker.exists())
                        mint.assert_not_called()

    def test_fresh_clone_and_linked_worktree_pass_config_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            real_run = subprocess.run
            env = command_environment()
            env['GIT_ALLOW_PROTOCOL'] = 'file'

            def git(*args, cwd=root):
                return real_run(['git', *args], cwd=cwd, env=env, check=True, capture_output=True, text=True)

            git('init', '--initial-branch=main', 'source')
            git('-c', 'user.name=Synthetic', '-c', 'user.email=worker@example.invalid',
                'commit', '--allow-empty', '-m', 'synthetic', cwd=root / 'source')
            git('clone', str(root / 'source'), 'clone')
            good = f'https://github.com/{REPO}'
            git('remote', 'set-url', 'origin', good, cwd=root / 'clone')
            git('worktree', 'add', '-b', 'topic', str(root / 'linked'), cwd=root / 'clone')
            for key in ['gh-merge-base', 't3-merge-base', 't3-merge-base-ref']:
                git('config', f'branch.topic.{key}', 'main', cwd=root / 'linked')
            for suffix in ['', '.git']:
                git('remote', 'set-url', 'origin', good + suffix, cwd=root / 'clone')
                for checkout in [root / 'clone', root / 'linked']:
                    with self.subTest(checkout=checkout, suffix=suffix):
                        def run(command, **kwargs):
                            kwargs.setdefault('cwd', checkout)
                            return real_run(command, **kwargs)

                        with patch('foreman.worker_auth.subprocess.run', side_effect=run):
                            validate_config(good)
                            self.assertEqual(validate_remote(REPO, checkout), [good + suffix, good + suffix])

    def test_real_push_ignores_follow_tags_and_pre_push_hook(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, remote = root / 'repo', root / 'remote.git'
            repo.mkdir()

            def git(*args):
                return subprocess.run(['git', *args], cwd=repo, check=True, capture_output=True, text=True)

            git('init')
            git('init', '--bare', str(remote))
            git('config', 'user.name', 'Synthetic Worker')
            git('config', 'user.email', 'worker@example.invalid')
            git('-c', 'core.hooksPath=/dev/null', 'commit', '--allow-empty', '-m', 'synthetic')
            git('tag', '-a', 'must-stay-local', '-m', 'synthetic tag')
            git('remote', 'add', 'origin', str(remote))
            git('config', 'push.followTags', 'true')
            hooks = repo / 'hooks'
            hooks.mkdir()
            marker = root / 'hook-ran'
            hook = hooks / 'pre-push'
            hook.write_text(f'#!/bin/sh\ntouch "{marker}"\nexit 1\n')
            hook.chmod(0o755)
            git('config', 'core.hooksPath', str(hooks))
            env = command_environment('synthetic')
            env['GIT_ALLOW_PROTOCOL'] = 'file'
            real_run = subprocess.run

            def run(command, **kwargs):
                return real_run(command, cwd=repo, **kwargs)

            with patch('foreman.worker_auth.validate_config'), \
                    patch('foreman.worker_auth.validate_remote', return_value=[str(remote)]), \
                    patch('foreman.worker_auth.environment', return_value=env), \
                    patch('foreman.worker_auth.subprocess.run', side_effect=run):
                self.assertEqual(self.invoke(['git', 'push', 'origin', 'HEAD:worker-topic'])[0], 0)
            self.assertFalse(marker.exists())
            self.assertEqual(git('--git-dir', str(remote), 'tag', '--list').stdout, '')
            self.assertEqual(git('--git-dir', str(remote), 'rev-parse', 'refs/heads/worker-topic').stdout,
                             git('rev-parse', 'HEAD').stdout)

    def test_fetch_does_not_recurse_into_hostile_submodule_config(self):
        with tempfile.TemporaryDirectory() as directory, socket.socket() as listener:
            root = Path(directory).resolve()
            listener.bind(('127.0.0.1', 0))
            listener.listen()
            listener.setblocking(False)
            real_run = subprocess.run
            env = command_environment('synthetic')
            env['GIT_ALLOW_PROTOCOL'] = 'file:https'

            def git(*args, cwd=root, check=True):
                return real_run(['git', *args], cwd=cwd, env=env, check=check, capture_output=True, text=True)

            def commit(repo):
                git('-c', 'user.name=Synthetic', '-c', 'user.email=worker@example.invalid',
                    'commit', '--allow-empty', '-am', 'synthetic', cwd=repo)
                return git('rev-parse', 'HEAD', cwd=repo).stdout.strip()

            child, parent = root / 'child', root / 'parent'
            workers = [root / name for name in ['uploadpack-worker', 'proxy-worker']]
            git('init', '--initial-branch=main', str(child))
            commit(child)
            git('init', '--initial-branch=main', str(parent))
            git('submodule', 'add', str(child), 'sub', cwd=parent)
            commit(parent)
            for worker in workers:
                git('clone', str(parent), str(worker))
                git('submodule', 'update', '--init', cwd=worker)
            new_child = commit(child)
            git('fetch', 'origin', cwd=parent / 'sub')
            git('checkout', new_child, cwd=parent / 'sub')
            new_parent = commit(parent)
            marker = root / 'executed'
            uploadpack = root / 'uploadpack'
            uploadpack.write_text(f'#!/bin/sh\ntouch "{marker}"\nexit 1\n')
            uploadpack.chmod(0o755)
            for worker in workers:
                config = worker / '.git' / 'modules' / 'sub' / 'config'
                if worker == workers[0]:
                    git('config', '--file', str(config), 'remote.origin.uploadpack', str(uploadpack))
                else:
                    git('config', '--file', str(config), 'remote.origin.url', f'https://github.com/{REPO}')
                    git('config', '--file', str(config), 'http.https://github.com/.proxy',
                        f'http://127.0.0.1:{listener.getsockname()[1]}')

                def run(command, **kwargs):
                    return real_run(command, cwd=worker, **kwargs)

                with patch('foreman.worker_auth.validate_config'), \
                        patch('foreman.worker_auth.validate_remote', return_value=[str(parent)]), \
                        patch('foreman.worker_auth.environment', return_value=env), \
                        patch('foreman.worker_auth.subprocess.run', side_effect=run):
                    code, out, err = self.invoke(['git', 'fetch', 'origin'])
                self.assertEqual(code, 0, err)
                self.assertNotIn('synthetic', out + err)
                self.assertEqual(git('rev-parse', 'origin/main', cwd=worker).stdout.strip(), new_parent)
                self.assertNotEqual(git('cat-file', '-e', new_child, cwd=worker / 'sub', check=False).returncode, 0)
                self.assertFalse(marker.exists())
                with self.assertRaises(BlockingIOError):
                    listener.accept()

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
            for bad in [good + '.git.evil', 'https://github.com/evil/tools.git']:
                with self.subTest(url=bad):
                    git('remote', 'set-url', 'origin', bad)
                    with self.assertRaises(CredentialFailure):
                        validate_remote(REPO, Path(directory))
            git('remote', 'set-url', 'origin', good)
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
                    self.assertEqual(main(['gh', 'issue', 'create', '--title', 'synthetic', '--body', 'synthetic']), 0)
            self.assertEqual(self.http.call_count, 4)  # Each hourly mint looks up the installation, then mints.

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
                        self.assertEqual(main(['--studio', 'foreman-studio', 'gh', 'issue', 'create', '--title', 'synthetic', '--body', 'synthetic']), 0)
            self.assertEqual(self.http.call_count, 4)  # Each hourly mint looks up the installation, then mints.


class WorkerPromptTest(unittest.TestCase):
    def test_no_worker_is_told_its_check_may_not_reproduce_and_each_runs_its_commands_in_the_container(self):
        from foreman.state import ChecksState, RunState
        from foreman.worker import assignment
        state = {"run": RunState(repo="a/b", issue=8), "checks": ChecksState(check="npm test")}
        for node, kept in (("implement", None), ("verify", Path("/scratch/attempt")), ("review", None)):
            with self.subTest(node=node):
                prompt = assignment(node, state, "Do it.", kept)
                for gone in ("may not reproduce", "no network", "unavailable in Codex", "sandboxed"):
                    self.assertNotIn(gone, prompt)
                self.assertIn("Run each command with Foreman's exec tool, which runs it in the run's attempt container.", prompt)
                self.assertEqual("Start the app and any server" in prompt, node != "review")
        self.assertNotIn("exec tool", assignment("triage", state, "Read.", None))
