import tests

from dataclasses import replace
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest

from foreman.adapters import ExecRoute, Outcome
from foreman.adapters.claude import EXEC_TOOL, ClaudeAdapter
from foreman.config import ModelSpec, load_config
from foreman.dry_run import ScriptHandler, SCRIPTED_USAGE
from foreman.worker import CliWorker
from test_adapters import CALL, fixture_process
from test_delivery import walk

CHECK = 'python -m unittest discover -s tests'
DIFF = 'git --no-replace-objects diff --no-color a...b --'
MODEL = ModelSpec.parse('claude-opus-5-5-high')
ROUTE = ExecRoute('container-1', Path('.'), 1800)


def read(envelope, **call):
    shell = {'edit': True, 'checks': (CHECK,), 'route': ROUTE} if not call else call
    return ClaudeAdapter(MODEL).read(subprocess.CompletedProcess(['claude'], 0, json.dumps(envelope), ''), replace(CALL, **shell))


def denied(tool, command=CHECK):
    return {**json.loads(fixture_process(['claude'], 'success', 0).stdout),
            'permission_denials': [{'tool_name': tool, 'tool_input': {'command': command}}]}


class ClaudeCheckTest(unittest.TestCase):
    def test_a_denied_exec_tool_fails_every_call_that_runs_commands(self):
        for call in ({'edit': True, 'checks': (CHECK,), 'route': ROUTE}, {'drive': True, 'route': ROUTE},
                     {'checks': (DIFF,), 'route': replace(ROUTE, only=(DIFF,))}):
            with self.subTest(call=call):
                result = read(denied(EXEC_TOOL), **call)
                self.assertEqual(result.kind, 'failed')
                self.assertIn("Claude denied Foreman's granted exec tool", result.detail)
                self.assertIsNotNone(result.usage)
        # A denial of another tool, such as Edit on a method root, leaves the answer as it stands.
        self.assertEqual(read(denied('Edit')).kind, 'ran')
        # A call that runs no command has no exec tool to deny.
        self.assertEqual(read(denied(EXEC_TOOL), edit=False).kind, 'ran')
        for name, kind in (('quota', 'quota'), ('max_budget', 'budget_exhausted'), ('wrong_model', 'failed'), ('success', 'ran')):
            original = json.loads(fixture_process(['claude'], name, 0).stdout)
            with self.subTest(name=name):
                result = read({**original, 'permission_denials': denied(EXEC_TOOL)['permission_denials']})
                self.assertEqual(result.kind, 'failed' if name == 'success' else kind)
                self.assertIsNotNone(result.usage)
                if name == 'wrong_model':
                    self.assertIn('modelUsage reported', result.detail)

    def test_malformed_denial_evidence_fails(self):
        # A changed denial record must not hide a denial of the exec tool beside it.
        envelope = json.loads(fixture_process(['claude'], 'success', 0).stdout)
        grant = denied(EXEC_TOOL)['permission_denials']
        for denials in (None, {}, [{'tool_name': EXEC_TOOL, 'tool_input': None}], [*grant, {'tool_name': 'Bash', 'tool_input': None}]):
            with self.subTest(denials=denials):
                outcome = read({**envelope, 'permission_denials': denials})
                self.assertEqual(outcome.kind, 'failed')
                self.assertIn('denial evidence malformed', outcome.detail)
                self.assertIsNotNone(outcome.usage)

    def test_non_review_denial_halts(self):
        script = ScriptHandler()
        def handler(node, model, state, instruction):
            result = script(node, model, state, instruction)
            return replace(result, kind="failed") if node == "implement" else result
        result = walk(handler)
        self.assertEqual(result["halt"], "invalid_output")
        self.assertEqual([a["stage"] for a in result["attempts"]], ["triage", "spec_check", "implement"])

    def test_graph_halts_on_first_fix_and_fallback(self):
        for mode in ('first', 'fix', 'fallback'):
            with self.subTest(mode=mode):
                script, config, count = ScriptHandler(reviews=['changes', 'approve']), load_config(), 0
                if mode != 'fallback':
                    config.nodes['implement'] = replace(config.nodes['implement'], models=(MODEL,))
                def handler(node, model, state, instruction):
                    nonlocal count
                    if node == 'implement':
                        count += 1
                        if mode == 'fallback' and model.family != 'claude':
                            return Outcome('quota', detail='quota', usage=SCRIPTED_USAGE)
                        if mode != 'fix' or count == 2:
                            run = lambda *_: subprocess.CompletedProcess(['claude'], 0, json.dumps(denied(EXEC_TOOL)), '')
                            return CliWorker(Path('.'), run=run, scratch_root=Path(self.enterContext(TemporaryDirectory())),
                                             containers=lambda base: ('container-1', 1800))(node, model, state, instruction)
                    return script(node, model, state, instruction)
                result = walk(handler, check=CHECK, config=config)
                self.assertEqual((result['stop'], count), ('invalid_output', {'first': 1, 'fix': 2, 'fallback': 3}[mode]))
                self.assertIn(EXEC_TOOL, result['halt_detail'])
                self.assertEqual(result['attempts'][-1]['outcome'], 'failed')
                self.assertIsNotNone(result['attempts'][-1]['usage'])


if __name__ == '__main__':
    unittest.main()
