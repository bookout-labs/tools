"""Two-target smoke with real CLI, graph, reconcile, check commands and SQLite."""
import tests

from contextlib import ExitStack, nullcontext, redirect_stdout, redirect_stderr
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
from foreman.delivery import LiveRun, stage_text
from foreman.config import load_config
from foreman.state import CheckpointVersionError
from foreman.workspace import LiveWorkspace
from skill_fixture import fixture_skills, checker_reply
from test_runner import APPROVE, FakeGitHub, confine_host, worker_reply


class NpmSmokeTest(unittest.TestCase):
    def exercise(self, mutation='', *, targets=None):
        confine_host(self)
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            fake = FakeGitHub()
            # A queued merge stays armed until GitHub merges it, which a release smoke doesn't wait for.
            fake.queue_merges = mutation != 'queued'
            forge = Mock(wraps=fake)
            forge.create_issue = Mock(return_value=17)
            forge.smoke_identity = Mock(return_value=(True, True, True))
            forge.smoke_issues = Mock(return_value=[])
            forge.smoke_pull_requests = Mock(return_value=[])
            real_run, real_main = subprocess.run, cli.main
            commands, reconciled, checkouts = [], [], []
            pins = {name: subprocess.check_output([name, '--version'], text=True).strip().removeprefix('v') for name in ('node', 'npm')}
            def process(args, **kwargs):
                if 'clone' in args:
                    checkout = Path(args[-1])
                    checkout.mkdir()
                    checkouts.append(checkout)
                    (checkout / 'tests').mkdir()
                    (checkout / 'src').mkdir()
                    package = {'name': 'unlike', 'version': '1.0.0', 'type': 'module',
                               'scripts': {'check': 'node --test'}, 'engines': pins}
                    (checkout / 'package.json').write_text(json.dumps(package))
                    (checkout / 'package-lock.json').write_text(json.dumps({'name': 'unlike', 'lockfileVersion': 3,
                        'packages': {'': {'name': 'unlike', 'version': '1.0.0'}}}))
                    (checkout / '.nvmrc').write_text(pins['node'] + '\n')
                    # The target's own contract names its check and verify skill.
                    (checkout / '.foreman').mkdir()
                    (checkout / '.foreman/contract.yaml').write_text('check: npm run check\nverify_skill: .claude/skills/verify-text/SKILL.md\n')
                    (checkout / '.claude/skills/verify-text').mkdir(parents=True)
                    (checkout / '.claude/skills/verify-text/SKILL.md').write_text('# Verify the text module\n')
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
                if args[-1].startswith(stage_text('verify')):
                    return worker_reply(args, {'tested_head': state['work']['published_head'], 'criteria': [{'criterion': 'smoke file', 'result': 'passed',
                        'observation': 'The scripted drive read the file.', 'evidence': 'captures/smoke.txt'}]})
                # The worker is scripted; all workspace operations remain real.
                if args[0] == 'codex':
                    (cwd / f'smoke_{state["run"]["issue"]}.txt').write_text(f'smoke {state["run"]["issue"]}\n')
                    if mutation == 'broken suite':
                        (cwd / 'src/text.js').write_text('export const normalize = s => s;\n')
                    return worker_reply(args, {'implementation': 'added exact fixture'})
                # A stage subgraph sees only its own path, so triage's recorded route tells review from triage.
                if state['plan']['triage']:
                    return worker_reply(args, APPROVE)
                return worker_reply(args, {'triage': 'supplied_slice'})
            def pull_request_for(repo, branch):
                # Origin is the checkout itself, so the PR shows the branch head that publish observes there.
                listed = real_run(['git', 'ls-remote', '.', f'refs/heads/{branch}'], cwd=checkouts[-1],
                                  capture_output=True, check=True, text=True).stdout.split()
                if listed:
                    fake.branches[branch] = listed[0]
                return fake.pull_request_for(repo, branch)
            forge.pull_request_for = Mock(side_effect=pull_request_for)
            original_reconcile = LiveWorkspace.reconcile
            def reconcile(workspace, parent, branch, base):
                reconciled.append(parent)
                return original_reconcile(workspace, parent, branch, base)
            def boundary(command, **kwargs):
                kwargs['live_run'] = replace(kwargs['live_run'], skills=fixture_skills)
                kwargs.setdefault('worker_runner', worker)
                return real_main(command, **kwargs)
            # Smoke names its own fixture login; nothing comes from the suite's environment.
            stack.enter_context(patch.dict(os.environ, {name: value for name, value in os.environ.items() if name != "FOREMAN_HUMAN_LOGIN"}, clear=True))
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
                    self.assertIn('check npm run check', output)
                    self.assertEqual(len(reconciled), 1)
                    self.assertIn('aggregate work tokens', output)

    def test_armed_queue_entry_is_the_end_point(self):
        # The merge stage holds the armed merge in its own state while it waits on GitHub's queue.
        code, output, _, _ = self.exercise('queued')
        self.assertEqual(code, 0, output)
        self.assertIn("merge status='armed'", output)

    def test_contract_mutants_and_broken_suite(self):
        for mutation in ('missing suite', 'broken suite'):
            with self.subTest(mutation=mutation):
                code, output, _, _ = self.exercise(mutation)
                self.assertEqual(code, 1, output)
                self.assertIn('target.contract' if mutation != 'broken suite' else 'check exit 1', output)

    def test_every_target_runs_unless_one_fails_and_the_worst_exit_wins(self):
        # Joe, 2026-11-23: an unavailable target (78) doesn't skip the next one; only a failure (1) stops early.
        from decimal import Decimal
        for first, second, expected in ((0, 0, 0), (0, 1, 1), (0, 78, 78), (78, 0, 78), (78, 1, 1)):
            with self.subTest(first=first, second=second), tempfile.TemporaryDirectory() as directory, \
                 patch('foreman.smoke_command._run_smoke', side_effect=[(first, 100, Decimal('.25')), (second, 200, Decimal('.5'))]) as run, \
                 patch('foreman.smoke_command.smoke_lock') as lock, redirect_stdout(io.StringIO()) as output:
                deadline = time.monotonic() + 30
                self.assertEqual(smoke.run_smoke(Path(directory), deadline), expected)
                self.assertEqual([c.args[2] for c in run.call_args_list], list(smoke.SMOKE_TARGETS))
                args = run.call_args.args
                self.assertEqual(args[1], deadline)
                self.assertEqual(args[3], smoke.SMOKE_TOKEN_CAP - 100)
                self.assertEqual(args[4].amount, Decimal('1.75'))
                self.assertNotEqual(run.call_args_list[0].args[0], args[0])
                self.assertIn(smoke.SMOKE_TARGETS[1], output.getvalue())
                self.assertEqual(lock.call_count, 2)
        with tempfile.TemporaryDirectory() as directory, \
             patch('foreman.smoke_command._run_smoke', return_value=(1, 0, Decimal(0))) as run, \
             patch('foreman.smoke_command.smoke_lock'), redirect_stdout(io.StringIO()):
            self.assertEqual(smoke.run_smoke(Path(directory), time.monotonic() + 30), 1)
            self.assertEqual(run.call_count, 1)

    def test_an_unavailable_lock_still_runs_the_next_target(self):
        from decimal import Decimal
        with tempfile.TemporaryDirectory() as directory, \
             patch('foreman.smoke_command._run_smoke', return_value=(0, 100, Decimal('.25'))) as run, \
             patch('foreman.smoke_command.smoke_lock', side_effect=[OSError('lock denied'), nullcontext()]), \
             redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()):
            self.assertEqual(smoke.run_smoke(Path(directory), time.monotonic() + 30), 78)
            self.assertEqual([c.args[2] for c in run.call_args_list], [smoke.SMOKE_TARGETS[1]])
            self.assertEqual(run.call_args.args[3], smoke.SMOKE_TOKEN_CAP)
            self.assertIn(f'smoke {smoke.SMOKE_TARGETS[0]} exit 78 lock attempt prerequisite', output.getvalue())

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
