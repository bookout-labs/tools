"""The npm target's fixture contract and prerequisites, and the order and totals of the smoke's targets."""
import tests

from contextlib import ExitStack, nullcontext, redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from foreman import smoke_command as smoke
from foreman.runner import SavedRun
from test_smoke import passing


class NpmSmokeTest(unittest.TestCase):
    def exercise(self, mutation=''):
        """The npm target up to the saved run the deliveries left, with a fixture clone. Node and npm are the image's, so the
        smoke runs neither on the host."""
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            pins = {'node': '22.11.0', 'npm': '10.9.0'}
            real_run, real_output = subprocess.run, subprocess.check_output
            def process(args, **kwargs):
                if args[0] != 'git':
                    return real_run(args, **kwargs)
                if 'clone' in args:
                    checkout = Path(args[-1])
                    (checkout / 'tests').mkdir(parents=True)
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
                    if mutation == 'missing suite':
                        (checkout / 'tests/text.test.js').unlink()
                return subprocess.CompletedProcess(args, 0, b'', b'')
            def output(args, **kwargs):
                if args[0] in ('node', 'npm'):
                    raise AssertionError(f'the smoke ran the host {args[0]}')
                return 'abc' if args[0] == 'git' else real_output(args, **kwargs)
            forge = Mock()
            forge.smoke_issues.return_value, forge.smoke_pull_requests.return_value, forge.create_issue.return_value = [], [], 17
            forge.smoke_identity.return_value = (True, True, True)
            forge.variables.return_value = {'FOREMAN_HUMAN_LOGIN': 'ghost'}
            stack.enter_context(patch('foreman.smoke_command.SMOKE_TARGETS', (smoke.SMOKE_TARGETS[1],)))
            stack.enter_context(patch('foreman.smoke_command.credentials.environment', return_value={}))
            stack.enter_context(patch('foreman.smoke_command.LiveForge', return_value=forge))
            stack.enter_context(patch('foreman.smoke_command.smoke_lock'))
            stack.enter_context(patch('foreman.smoke_command.subprocess.run', side_effect=process))
            stack.enter_context(patch('foreman.smoke_command.subprocess.check_output', side_effect=output))
            stack.enter_context(patch('foreman.smoke_command.receiving', return_value=True))
            stack.enter_context(patch('foreman.smoke_command.human_session'))
            stack.enter_context(patch('foreman.smoke_command.release', return_value=passing()['run'].release))
            ready = stack.enter_context(patch('foreman.smoke_command.add_ready'))
            stack.enter_context(patch('foreman.smoke_command.published_file'))
            stack.enter_context(patch('foreman.smoke_command.shows', return_value=True))
            stack.enter_context(patch('foreman.smoke_command.run_end', return_value=SavedRun(passing(), False, True)))
            stack.enter_context(patch.dict('os.environ', {'XDG_STATE_HOME': str(root / 'state')}))
            output_text, errors = io.StringIO(), io.StringIO()
            with redirect_stdout(output_text), redirect_stderr(errors):
                code = smoke.run_smoke(root, time.monotonic() + 30)
            return code, output_text.getvalue() + errors.getvalue(), ready

    def test_baseline(self):
        code, output, ready = self.exercise()
        self.assertEqual(code, 0, output)
        ready.assert_called_once_with(smoke.SMOKE_TARGETS[1], 17)
        self.assertIn('aggregate work tokens', output)

    def test_contract_mutant_fails_before_the_issue(self):
        code, output, ready = self.exercise('missing suite')
        self.assertEqual(code, 1, output)
        self.assertIn('target.contract', output)
        ready.assert_not_called()

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
                if expected != 1:
                    self.assertIn('smoke aggregate work tokens 300; Claude $0.75', output.getvalue())
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
            self.assertIn(f'smoke {smoke.SMOKE_TARGETS[0]} exit 78 lock attempt prerequisite', output.getvalue())
