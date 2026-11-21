import tests
from foreman.state import PlanState, ChecksState

from dataclasses import replace
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from foreman.adapters import Outcome
from foreman.adapters.claude import ClaudeAdapter
from foreman.config import ModelSpec, load_config
from foreman.dry_run import ScriptHandler
from foreman.worker import CliWorker
from test_adapters import CALL, FIXTURES, fixture_process
from test_delivery import walk

CHECK = 'python -m unittest discover -s tests'
COMPOUND = 'touch first && touch second'
MODEL = ModelSpec.parse('claude-opus-5-5-high')

def read(envelope, check=CHECK):
    return ClaudeAdapter(MODEL).read(subprocess.CompletedProcess(['claude'], 0, json.dumps(envelope), ''), replace(CALL, check=check))

def denied(command):
    return {**json.loads(fixture_process(['claude'], 'success', 0).stdout),
            'permission_denials': [{'tool_name': 'Bash', 'tool_input': {'command': command}}]}

class ClaudeCheckTest(unittest.TestCase):
    def test_denials_and_precedence(self):
        for command, check, kind in ((CHECK, CHECK, 'failed'), (None, CHECK, 'failed'), ('', CHECK, 'failed'),
                ('touch second', COMPOUND, 'failed'), ('git commit -m "fix bug"', CHECK, 'ran'),
                ('git log --format="%H %s"', 'git diff --no-color a...b --', 'ran')):
            with self.subTest(command=command):
                result = read(denied(command), check)
                self.assertEqual(result.kind, kind)
                self.assertIsNotNone(result.usage)
                if kind == 'failed':
                    self.assertIn(check, result.detail)
                    self.assertIn(repr(command), result.detail)
        envelope = denied(CHECK)
        envelope['permission_denials'][0]['tool_name'] = 'Edit'
        self.assertEqual(read(envelope).kind, 'ran')
        for name, kind in (('quota', 'quota'), ('max_budget', 'budget_exhausted'), ('wrong_model', 'failed'), ('success', 'ran')):
            original = json.loads(fixture_process(['claude'], name, 0).stdout)
            for denials in (None, [{'tool_name': 'Bash', 'tool_input': None}], denied(CHECK)['permission_denials']):
                with self.subTest(name=name, denials=denials):
                    result = read({**original, 'permission_denials': denials})
                    self.assertEqual(result.kind, 'failed' if name == 'success' else kind)
                    self.assertIsNotNone(result.usage)
                    if name == 'wrong_model':
                        self.assertIn('modelUsage reported', result.detail)

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
                            return Outcome('quota', detail='quota')
                        if mode != 'fix' or count == 2:
                            return CliWorker(Path('.'), run=lambda *_: subprocess.CompletedProcess(['claude'], 0, json.dumps(denied(CHECK)), ''))(node, model, state, instruction)
                    return script(node, model, state, instruction)
                result = walk(handler, check=CHECK, config=config)
                self.assertEqual((result['stop'], count), ('invalid_output', {'first': 1, 'fix': 2, 'fallback': 3}[mode]))
                self.assertIn(CHECK, result['halt_detail'])
                self.assertEqual(result['attempts'][-1]['outcome'], 'failed')
                self.assertIsNotNone(result['attempts'][-1]['usage'])

    def test_captures(self):
        capture = json.loads((FIXTURES / 'claude/t1_implement.json').read_text())
        self.assertEqual((capture['returncode'], capture['marker'], read(capture['envelope']).kind), (0, 'ran\n', 'ran'))
        self.assertEqual(capture['envelope']['permission_denials'], [])
        blocks = [c for r in capture['transcript'] for c in r['message']['content']]
        ids = [c['id'] for c in blocks if c.get('name') == 'Bash' and c['input']['command'] == CHECK]
        self.assertTrue(any(c.get('tool_use_id') in ids and not c.get('is_error') and 'OK' in c.get('content', '') for c in blocks))
        for name, command in (('denied-wrapper', f"bash -lc '{COMPOUND}'"), ('denied-segment', 'touch second')):
            capture = json.loads((FIXTURES / f'claude/{name}.json').read_text())
            envelope = capture['envelope']
            denial = next(d for d in envelope['permission_denials'] if d['tool_input'].get('command') == command)
            result = read({**envelope, 'permission_denials': [denial]}, COMPOUND)
            self.assertEqual(result.kind, 'failed')
            self.assertIn(command, result.detail)
            self.assertIsNotNone(result.usage)

    @unittest.skipUnless(os.environ.get('FOREMAN_LIVE_WORKER') == 'claude', 'opt-in paid Claude calls')
    def test_live(self):
        for check, commands, names in ((CHECK, [CHECK], ['t1_implement']), (COMPOUND, [f"bash -lc '{COMPOUND}'", 'touch second'], ['denied-wrapper', 'denied-segment'])):
            with TemporaryDirectory() as directory:
                repo, captured = Path(directory).resolve(), {}
                subprocess.run(['git', 'init', '-q', str(repo)], check=True)
                (repo / 'tests').mkdir()
                (repo / 'tests/test_marker.py').write_text("import unittest\nfrom pathlib import Path\nclass TestMarker(unittest.TestCase):\n def test_marker(self):\n  Path('marker').write_text('ran\\n')\n")
                def run(args, cwd, env):
                    live_env = {k: v for k, v in os.environ.items() if k.lower() not in ('http_proxy', 'https_proxy', 'all_proxy')}
                    live_env['PATH'] = str(Path(sys.executable).parent) + os.pathsep + live_env.get('PATH', '')
                    process = subprocess.run(args, cwd=cwd, env={**live_env, **env}, capture_output=True, text=True, stdin=subprocess.DEVNULL)
                    captured.update(argv=args, envelope=json.loads(process.stdout), returncode=process.returncode)
                    return process
                result = CliWorker(repo, run=run)('implement', MODEL, {"checks": ChecksState(check=check), "plan": PlanState()}, f'Use Bash separately for each command exactly once, including denials: {json.dumps(commands)}. Then return implementation "done", browser_proof null, formal_proof null.')
                self.assertEqual(result.kind, 'ran' if check == CHECK else 'failed', result.detail)
                transcript = Path.home() / '.claude/projects' / re.sub(r'[^a-zA-Z0-9-]', '-', str(repo)) / f"{captured['envelope']['session_id']}.jsonl"
                rows = [json.loads(line) for line in transcript.read_text().splitlines()]
                captured.update(marker=(repo / 'marker').read_text() if (repo / 'marker').exists() else None, transcript=[{'type': r['type'], 'message': {'content': [c for c in r['message']['content'] if isinstance(c, dict) and c.get('type') in ('tool_use', 'tool_result')]}} for r in rows if r.get('type') in ('assistant', 'user')])
                text = re.sub(r'[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}', '<uuid>', json.dumps(captured).replace(str(repo), '<repo>'))
                text = text.replace(captured['argv'][captured['argv'].index('--add-dir') + 1], '<worker>')
                text = re.sub(r'"credential_expires_at":\s*"[^"]*"', '"credential_expires_at": null', text)
                for name in names:
                    (FIXTURES / f'claude/{name}.json').write_text(text + '\n')
        self.test_captures()
