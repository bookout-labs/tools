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
from unittest.mock import patch

from foreman.adapters import Outcome
from foreman.adapters.claude import ClaudeAdapter
from foreman.config import ModelSpec, load_config
from foreman.dry_run import ScriptHandler, SCRIPTED_USAGE
from foreman.worker import CliWorker
from foreman.workspace import ReviewScope
from test_adapters import CALL, FIXTURES, claude_review_stream, fixture_process
from test_delivery import walk

CHECK = 'python -m unittest discover -s tests'
COMPOUND = 'touch first && touch second'
DIFF = 'git --no-replace-objects diff --no-color a...b --'
MODEL = ModelSpec.parse('claude-opus-5-5-high')

def read(envelope, check=CHECK):
    return ClaudeAdapter(MODEL).read(subprocess.CompletedProcess(['claude'], 0, json.dumps(envelope), ''), replace(CALL, edit=True, checks=(check,) if check else ()))

def review(stdout, required=(DIFF,), stderr=''):
    return ClaudeAdapter(MODEL).read(subprocess.CompletedProcess(['claude'], 0, stdout, stderr),
                                     replace(CALL, checks=(*required, CHECK), review=True, required=required))

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

    def test_native_review_counts_only_the_full_diff_read(self):
        capture = json.loads((FIXTURES / "claude/review-stream.json").read_text())
        required = ReviewScope(capture["scope"], frozenset()).required
        for name, kind in (("read", "ran"), ("piped", "failed")):
            with self.subTest(name=name):
                captured = capture["calls"][name]
                self.assertEqual(captured["kind"], kind)
                outcome = review(captured["stdout"], required)
                self.assertEqual(outcome.kind, kind, outcome.detail)
                self.assertIsNotNone(outcome.usage)
                self.assertIsNotNone(outcome.native_usage)
                envelope = json.loads(captured["stdout"].splitlines()[-1])
                if kind == "ran":
                    # The extra command app #1376's reviewer ran is denied, and the review still counts.
                    self.assertEqual([d["tool_input"]["command"][:8] for d in envelope["permission_denials"]], ["node -e "])
                else:
                    # Claude Code lets the diff run piped through head, so a truncated read leaves no denial.
                    self.assertEqual(envelope["permission_denials"], [])
                    self.assertEqual(outcome.output, {})
                    self.assertEqual(outcome.detail, f"Claude reviewer did not complete required read: {required[0]}")
        # Earlier capture, transcript blocks only: a denied chained diff never reads it, standalone exact grants do.
        capture = json.loads((FIXTURES / "claude/review-denial.json").read_text())
        required = ReviewScope(capture["scope"], frozenset()).required
        first, retry = capture["calls"]
        for captured, kind in ((first, "failed"), (retry, "ran")):
            stdout = "".join(json.dumps({"type": "assistant" if block["type"] == "tool_use" else "user", "parent_tool_use_id": None,
                                         "message": {"content": [block]}}) + "\n" for block in captured["blocks"])
            self.assertEqual(review(stdout + json.dumps(captured["envelope"]), required).kind, kind)
        self.assertLess(sum(c["envelope"]["total_cost_usd"] for c in capture["calls"]), 1)
        denied_id = first["envelope"]["permission_denials"][0]["tool_use_id"]
        self.assertTrue(any(b.get("tool_use_id") == denied_id and b.get("is_error") for b in first["blocks"]))
        # Claude's native matcher runs each exact standalone grant: every tree and blob read returned its content.
        calls = {b["id"]: b["input"]["command"] for b in retry["blocks"] if b.get("name") == "Bash"}
        successful = {calls[b["tool_use_id"]]: b["content"] for b in retry["blocks"]
                      if b.get("tool_use_id") in calls and not b.get("is_error")}
        self.assertEqual(set(successful), {line for line in capture["scope"].splitlines() if line.startswith("git ")})
        for content in ("base deletion\n", "unchanged dependency\n", "verified head\n", "previous only\n", "odd path\n"):
            self.assertIn(content.strip(), successful.values())

    def test_review_stream_edges(self):
        envelope = json.loads(fixture_process(['claude'], 'success', 0).stdout)
        for denials in (None, {}, [{"tool_name": "Bash", "tool_input": None}], denied(DIFF + " | od -c")["permission_denials"]):
            with self.subTest(denials=denials):
                self.assertEqual(review(claude_review_stream({**envelope, "permission_denials": denials}, (DIFF,))).kind, "ran")
        listed = claude_review_stream(envelope, (DIFF,)).replace('"content": "output"', '"content": [{"type": "text", "text": "diff"}]')
        nested = claude_review_stream(envelope, (DIFF,)).replace('"parent_tool_use_id": null', '"parent_tool_use_id": "toolu_parent"')
        background = claude_review_stream(envelope, (DIFF,)).replace('"input": {', '"input": {"run_in_background": true, ', 1)
        # A JSON string may hold U+2028 raw; only "\n" ends an NDJSON line.
        separator = claude_review_stream(envelope, (DIFF,)).replace('"content": "output"', '"content": "a\u2028b\u0085c"')
        for name, stdout, kind in (
            ("list content", listed, "ran"),
            ("line separator in output", separator, "ran"),
            ("backgrounded read", background, "failed"),
            ("errored read", claude_review_stream(envelope, (), (DIFF,)), "failed"),
            ("subagent read", nested, "failed"),
            ("other reads only", claude_review_stream(envelope, (CHECK,)), "failed"),
            ("invalid line", "not json\n" + claude_review_stream(envelope, (DIFF,)), "failed"),
        ):
            with self.subTest(name=name):
                outcome = review(stdout)
                self.assertEqual(outcome.kind, kind, outcome.detail)
                self.assertIsNotNone(outcome.usage)
        # A stop the envelope reports wins over a missing read, and keeps its usage.
        for name, kind in (("quota", "quota"), ("max_budget", "budget_exhausted"), ("wrong_model", "failed")):
            with self.subTest(name=name):
                outcome = review(claude_review_stream(json.loads(fixture_process(['claude'], name, 0).stdout)))
                self.assertEqual(outcome.kind, kind)
                self.assertIsNotNone(outcome.usage)
                if name == "wrong_model":
                    self.assertIn("modelUsage reported", outcome.detail)
        self.assertEqual(review("", stderr="Claude did not start").detail, "Claude did not start")
        # A stream cut before its result envelope reports its last line, not the whole stream.
        cut = claude_review_stream(envelope, (DIFF,)).rstrip("\n").rpartition("\n")[0]
        self.assertEqual(review(cut).detail, cut.rpartition("\n")[2])

    def test_review_without_a_required_read_never_launches(self):
        for scope in (None, {"command": "", "inline": []}, {"command": "# base only\n# head only", "inline": []}):
            with self.subTest(scope=scope):
                launched = []
                plan = {"models": [], "instruction": "", "methods": [], "target": None, "scope": scope}
                outcome = CliWorker(Path("."), run=lambda *args: launched.append(args))(
                    "review", MODEL, {"dispatch_plan": plan}, "Review.")
                self.assertEqual((outcome.kind, outcome.detail), ("failed", "Review scope names no full PR diff read"))
                self.assertEqual(launched, [])

    def test_non_review_denial_halts(self):
        script = ScriptHandler()
        def handler(node, model, state, instruction):
            result = script(node, model, state, instruction)
            return replace(result, kind="failed") if node == "implement" else result
        result = walk(handler)
        self.assertEqual(result["halt"], "invalid_output")
        self.assertEqual([a["stage"] for a in result["attempts"]], ["triage", "spec_check", "implement"])

    @patch("foreman.adapters.claude.sandboxed")
    def test_graph_halts_on_first_fix_and_fallback(self, _confinable):
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
                result = CliWorker(repo, run=run)('implement', MODEL, {"checks": ChecksState(check=check), "plan": PlanState()}, f'Use Bash separately for each command exactly once, including denials: {json.dumps(commands)}. Then return implementation "done".')
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
