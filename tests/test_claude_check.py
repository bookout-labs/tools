import tests

from dataclasses import replace
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from foreman.adapters import Call, Outcome
from foreman.adapters.claude import ClaudeAdapter
from foreman.config import ModelSpec, load_config
from foreman.dry_run import ScriptHandler
from foreman.worker import CliWorker
from test_delivery import walk

FIXTURE = Path(__file__).with_name('fixtures') / 'claude' / 't1_implement.json'
CHECK = 'python -m unittest discover -s tests'
MODEL = ModelSpec.parse('claude-opus-5-5-high')


def denied(command):
    envelope = json.loads(FIXTURE.with_name('success.json').read_text())
    envelope['permission_denials'] = [{'tool_name': 'Bash', 'tool_input': {'command': command}}]
    return subprocess.CompletedProcess(['claude'], 0, json.dumps(envelope), '')


class ClaudeCheckTest(unittest.TestCase):
    def test_denied_check_and_wrapper_fail_with_usage(self):
        for command in (CHECK, f'bash -lc "{CHECK}"', 'bash -lc \'python -m unittest discover -s "tests"\'', None):
            result = ClaudeAdapter(MODEL).read(denied(command), Call('', '{}', True, Path('.'), check=CHECK))
            self.assertEqual(result.kind, 'failed')
            self.assertIn(CHECK, result.detail)
            self.assertIsNotNone(result.usage)
        result = ClaudeAdapter(MODEL).read(denied('git commit -m x'), Call('', '{}', True, Path('.'), check=CHECK))
        self.assertEqual(result.kind, 'ran')

    def test_denial_preserves_quota_and_budget(self):
        for changes, kind in (({'is_error': True, 'api_error_status': 429}, 'quota'),
                              ({'subtype': 'error_max_budget_usd'}, 'budget_exhausted')):
            process = denied(CHECK)
            process.stdout = json.dumps({**json.loads(process.stdout), **changes})
            result = ClaudeAdapter(MODEL).read(process, Call('', '{}', True, Path('.'), check=CHECK))
            self.assertEqual(result.kind, kind)
            self.assertIsNotNone(result.usage)

    def test_graph_halts_on_first_fix_and_fallback(self):
        for mode in ('first', 'fix', 'fallback'):
            script = ScriptHandler(reviews=['changes', 'approve'] if mode == 'fix' else None)
            config = load_config()
            if mode != 'fallback':
                config.nodes['implement'] = replace(config.nodes['implement'], models=(MODEL,))
            count = 0

            def handler(node, model, state, instruction):
                nonlocal count
                if node == 'implement':
                    count += 1
                    if mode == 'fallback' and model.family != 'claude':
                        return Outcome('quota', detail='quota')
                    if mode != 'fix' or count == 2:
                        return CliWorker(Path('.'), run=lambda *_: denied(CHECK))(node, model, state, instruction)
                return script(node, model, state, instruction)

            result = walk(handler, check=CHECK, config=config)
            self.assertEqual(result['stop'], 'invalid_output')
            self.assertIn(CHECK, result['halt_detail'])
            self.assertEqual(count, {'first': 1, 'fix': 2, 'fallback': 3}[mode])

    def test_recorded_t1(self):
        capture = json.loads(FIXTURE.read_text())
        envelope = capture['envelope']
        self.assertEqual(capture['marker'], 'ran\n')
        self.assertEqual(capture['returncode'], 0)
        blocks = [c for row in capture['transcript'] for c in row['message']['content']]
        calls = [c['id'] for c in blocks if c['type'] == 'tool_use' and c['name'] == 'Bash' and c['input']['command'] == CHECK]
        self.assertTrue(calls)
        results = [c for c in blocks if c['type'] == 'tool_result' and c['tool_use_id'] in calls]
        self.assertTrue(results)
        self.assertTrue(all(not c.get('is_error', False) for c in results))
        self.assertTrue(any('OK' in c['content'] for c in results))
        self.assertNotIn(CHECK, [d['tool_input'].get('command') for d in envelope['permission_denials']])
        self.assertIn(f"bash -lc '{CHECK}'", [d['tool_input'].get('command') for d in envelope['permission_denials']])
        self.assertEqual(ClaudeAdapter(MODEL).read(subprocess.CompletedProcess(['claude'], 0, json.dumps(envelope), ''),
                                               Call('', '{}', True, Path('.'), check=CHECK)).kind,
                         'failed')

    @unittest.skipUnless(os.environ.get('FOREMAN_LIVE_WORKER') == 'claude', 'opt-in paid Claude call')
    def test_live_t1(self):
        with TemporaryDirectory() as directory:
            repo = Path(directory).resolve()
            subprocess.run(['git', 'init', '-q', str(repo)], check=True)
            (repo / 'tests').mkdir()
            (repo / 'tests/test_marker.py').write_text("import unittest\nfrom pathlib import Path\nclass TestMarker(unittest.TestCase):\n def test_marker(self):\n  Path('marker').write_text('ran\\n')\n")
            captured = {}

            def run(args, cwd, env):
                live_env = {k: v for k, v in os.environ.items() if k.lower() not in ('http_proxy', 'https_proxy', 'all_proxy')}
                live_env['PATH'] = str(Path(sys.executable).parent) + os.pathsep + live_env['PATH']
                process = subprocess.run(args, cwd=cwd, env={**live_env, **env}, capture_output=True, text=True, stdin=subprocess.DEVNULL)
                captured.update(envelope=json.loads(process.stdout), returncode=process.returncode)
                return process

            result = CliWorker(repo, run=run)('implement', MODEL, {'check': CHECK},
                f'First attempt `bash -lc \'{CHECK}\'` using Bash. Then, regardless of that result, run exactly `{CHECK}` using Bash. Then return implementation "tests passed", browser_proof null, formal_proof null. Do not edit files.')
            self.assertEqual(result.kind, 'failed', result.detail)
            envelope = captured['envelope']
            transcript = Path.home() / '.claude/projects' / re.sub(r'[^a-zA-Z0-9-]', '-', str(repo)) / f"{envelope['session_id']}.jsonl"
            rows = [json.loads(line) for line in transcript.read_text().splitlines()]
            captured.update(check=CHECK, marker=(repo / 'marker').read_text(), transcript=[
                {'type': r['type'], 'message': {'content': [c for c in r['message']['content'] if isinstance(c, dict) and c.get('type') in ('tool_use', 'tool_result')]}}
                for r in rows if r.get('type') in ('assistant', 'user')])
            FIXTURE.write_text(json.dumps(captured) + '\n')
        self.test_recorded_t1()
