"""Two-target smoke with real CLI, graph, reconcile, check commands and SQLite."""
import tests

from contextlib import ExitStack, redirect_stdout, redirect_stderr
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from foreman import cli, smoke_command as smoke
from foreman.delivery import LiveRun
from foreman.config import load_config
from foreman.state import CheckpointVersionError
from foreman.workspace import LiveWorkspace
from skill_fixture import fixture_skills, checker_reply
from test_runner import APPROVE, FakeGitHub, worker_reply


class NpmSmokeTest(unittest.TestCase):
    def exercise(self, mutation='', *, targets=None):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            fake = FakeGitHub()
            forge = Mock(wraps=fake)
            forge.create_issue = Mock(return_value=17)
            forge.smoke_identity = Mock(return_value=(True, True, True))
            forge.smoke_issues = Mock(return_value=[])
            forge.smoke_pull_requests = Mock(return_value=[])
            forge.ensure_smoke_label = Mock()
            real_run, real_main = subprocess.run, cli.main
            commands, reconciled = [], []
            pins = {name: subprocess.check_output([name, '--version'], text=True).strip().removeprefix('v') for name in ('node', 'npm')}
            def process(args, **kwargs):
                if 'clone' in args:
                    checkout = Path(args[-1])
                    checkout.mkdir()
                    (checkout / 'tests').mkdir()
                    (checkout / 'src').mkdir()
                    package = {'name': 'unlike', 'version': '1.0.0', 'type': 'module',
                               'scripts': {'check': 'node --test'}, 'engines': pins}
                    (checkout / 'package.json').write_text(json.dumps(package))
                    (checkout / 'package-lock.json').write_text(json.dumps({'name': 'unlike', 'lockfileVersion': 3,
                        'packages': {'': {'name': 'unlike', 'version': '1.0.0'}}}))
                    (checkout / '.nvmrc').write_text(pins['node'] + '\n')
                    (checkout / 'src/text.js').write_text('export const normalize = s => s.trim();\n')
                    (checkout / 'tests/text.test.js').write_text("import test from 'node:test';\nimport assert from 'node:assert/strict';\n"
                        "import { normalize } from '../src/text.js'; // unlike\n" +
                        ''.join(f"test('case {i}', () => assert.equal(normalize(' hi '), 'hi'));\n" for i in range(18)))
                    def git(*args):
                        return real_run(['git', *args], cwd=checkout, capture_output=True, check=True, text=True).stdout.strip()
                    git('init', '-b', 'main')
                    git('config', 'user.name', 'Synthetic')
                    git('config', 'user.email', 'test@example.invalid')
                    git('config', 'commit.gpgsign', 'false')
                    git('add', '.')
                    git('commit', '-m', 'Fixture')
                    git('remote', 'add', 'origin', '.')
                    git('update-ref', 'refs/remotes/origin/main', 'HEAD')
                    fake.base = git('rev-parse', 'HEAD')
                    if mutation == 'missing suite':
                        (checkout / 'tests/text.test.js').unlink()
                    return subprocess.CompletedProcess(args, 0, '', '')
                if mutation == 'npm missing' and args == ['npm', 'ci']:
                    raise FileNotFoundError('npm unavailable')
                if mutation == 'npm failed' and args == ['npm', 'ci']:
                    raise subprocess.CalledProcessError(1, args, stderr='npm install failed')
                commands.append(args)
                result = real_run(args, **kwargs)
                if mutation == 'npm dirty' and args == ['npm', 'ci']:
                    for name in ('package.json', 'package-lock.json'):
                        path = Path(kwargs['cwd']) / name
                        path.write_text(path.read_text() + '\n')
                return result
            def worker(args, cwd, env):
                if reply := checker_reply(args):
                    return worker_reply(args, reply)
                state = json.loads(args[-1].split('Delivery state:\n')[1])
                node = state['path'][-1] if state['path'] else ''
                # The worker is scripted; all workspace operations remain real.
                if args[0] == 'codex':
                    (cwd / f'smoke_{state["run"]["issue"]}.txt').write_text(f'smoke {state["run"]["issue"]}\n')
                    if mutation == 'broken suite':
                        (cwd / 'src/text.js').write_text('export const normalize = s => s;\n')
                    return worker_reply(args, {'implementation': 'added exact fixture'})
                if node:
                    return worker_reply(args, APPROVE)
                return worker_reply(args, {'triage': 'implement'})
            original_reconcile = LiveWorkspace.reconcile
            def reconcile(workspace, parent, branch, base):
                reconciled.append(parent)
                return original_reconcile(workspace, parent, branch, base)
            def boundary(command, **kwargs):
                kwargs['live_run'] = replace(kwargs['live_run'], skills=fixture_skills)
                kwargs.setdefault('worker_runner', worker)
                return real_main(command, **kwargs)
            stack.enter_context(patch('foreman.smoke_command.SMOKE_TARGETS', targets or (smoke.SMOKE_TARGETS[1],)))
            stack.enter_context(patch('foreman.smoke_command.credentials.environment', return_value=dict(os.environ)))
            stack.enter_context(patch('foreman.smoke_command.git_auth', return_value=[]))
            stack.enter_context(patch('foreman.smoke_command.LiveForge', return_value=forge))
            stack.enter_context(patch('foreman.smoke_command.smoke_lock'))
            stack.enter_context(patch('foreman.smoke_command.subprocess.run', side_effect=process))
            stack.enter_context(patch('foreman.smoke_command.main', side_effect=boundary))
            stack.enter_context(patch.object(LiveWorkspace, 'push'))
            stack.enter_context(patch.object(LiveWorkspace, 'validate_origin'))
            config = load_config()
            spec = config.nodes["spec"]
            config.nodes["spec"] = replace(spec, models=tuple(model for model in spec.models if model.family != "grok"))
            stack.enter_context(patch("foreman.runner.load_config", return_value=config))
            stack.enter_context(patch.object(LiveWorkspace, 'reconcile', reconcile))
            if mutation == 'bare refusal':
                stack.enter_context(patch.object(CheckpointVersionError, '__init__', lambda self, *args: ValueError.__init__(self, 'Unsupported checkpoint')))
            output, errors = io.StringIO(), io.StringIO()
            with redirect_stdout(output), redirect_stderr(errors):
                code = smoke.run_smoke(root, time.monotonic() + 30)
            if not mutation:
                from langgraph.checkpoint.sqlite import SqliteSaver
                from foreman.state import SCHEMA_VERSION, checkpoint_serializer
                home = root / 'foreman-smoke-npm'
                for state in ('old-state', 'fresh-state'):
                    with SqliteSaver.from_conn_string(str(home / state / 'foreman/checkpoints.sqlite3')) as saver:
                        saver.serde = checkpoint_serializer()
                        old = saver.get({'configurable': {'thread_id': 'bookout-labs/foreman-smoke-npm#18'}})
                        self.assertEqual(old['channel_values']['schema_version'], SCHEMA_VERSION - 1)
            return code, output.getvalue() + errors.getvalue(), commands, reconciled

    def test_baseline_and_checkpoint_guidance_mutant(self):
        for mutation in ('', 'bare refusal'):
            with self.subTest(mutation=mutation):
                code, output, commands, reconciled = self.exercise(mutation)
                self.assertEqual(code, 1 if mutation else 0, output)
                if mutation:
                    self.assertIn('checkpoint.refusal_guidance', output)
                else:
                    self.assertIn(['npm', 'ci'], commands)
                    self.assertIn('verify npm run check', output)
                    self.assertEqual(len(reconciled), 1)
                    self.assertIn('aggregate work tokens', output)

    def test_contract_mutants_and_broken_suite(self):
        for mutation in ('missing suite', 'broken suite'):
            with self.subTest(mutation=mutation):
                code, output, _, _ = self.exercise(mutation)
                self.assertEqual(code, 1, output)
                self.assertIn('target.contract' if mutation != 'broken suite' else 'check exit 1', output)

    def test_two_target_fail_fast_and_remaining_allowance(self):
        from decimal import Decimal
        for second in (0, 1, 78):
            with self.subTest(second=second), tempfile.TemporaryDirectory() as directory, \
                 patch('foreman.smoke_command._run_smoke', side_effect=[(0, 100, Decimal('.25')), (second, 200, Decimal('.5'))]) as run, \
                 patch('foreman.smoke_command.smoke_lock') as lock, redirect_stdout(io.StringIO()) as output:
                deadline = time.monotonic() + 30
                self.assertEqual(smoke.run_smoke(Path(directory), deadline), second)
                self.assertEqual([c.args[2] for c in run.call_args_list], list(smoke.SMOKE_TARGETS))
                args = run.call_args.args
                self.assertEqual(args[1], deadline)
                self.assertEqual(args[3], smoke.SMOKE_TOKEN_CAP - 100)
                self.assertEqual(args[4].amount, Decimal('1.75'))
                self.assertNotEqual(run.call_args_list[0].args[0], args[0])
                self.assertIn(smoke.SMOKE_TARGETS[1], output.getvalue())
                self.assertEqual(lock.call_count, 2)
        with tempfile.TemporaryDirectory() as directory, \
             patch('foreman.smoke_command._run_smoke', return_value=(78, 0, Decimal(0))) as run, \
             patch('foreman.smoke_command.smoke_lock'), redirect_stdout(io.StringIO()):
            self.assertEqual(smoke.run_smoke(Path(directory), time.monotonic() + 30), 78)
            self.assertEqual(run.call_count, 1)

    def test_missing_npm_is_unavailable_before_dispatch(self):
        code, output, _, reconciled = self.exercise('npm missing')
        self.assertEqual(code, 78, output)
        self.assertFalse(reconciled)
        self.assertIn('npm unavailable', output)

    def test_failed_npm_install_is_unavailable_before_dispatch(self):
        code, output, _, reconciled = self.exercise('npm failed')
        self.assertEqual(code, 78, output)
        self.assertFalse(reconciled)
        self.assertIn('npm install failed', output)

    def test_successful_npm_install_with_dirty_checkout_is_contract_failure(self):
        code, output, commands, reconciled = self.exercise('npm dirty')
        self.assertEqual(code, 1, output)
        self.assertIn(['npm', 'ci'], commands)
        self.assertFalse(reconciled)
        self.assertIn('target.contract', output)
        self.assertIn('target bookout-labs/foreman-smoke-npm', output)
        self.assertIn('package.json', output)
        self.assertIn('package-lock.json', output)
