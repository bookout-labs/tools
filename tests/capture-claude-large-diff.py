"""Live probe: what a Claude reviewer is handed for a review diff far past Bash's output limit. Run outside unittest's offline policy."""
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

from foreman.adapters import Call
from foreman.adapters.claude import ClaudeAdapter
from foreman.config import ModelSpec
from foreman.workspace import LiveWorkspace, ReviewScope

LINES = 8000
base = Path(tempfile.mkdtemp(prefix='.scratch.large-diff.', dir=Path.cwd())).resolve()
clone, repo, attempt = (base / name for name in ('clone', 'repo', 'attempt'))
for folder in (clone, attempt):
    folder.mkdir()
(clone / 'README.md').write_text('probe\n')
identity = ['-c', 'user.name=Probe', '-c', 'user.email=probe@example.test']
for args in (['init', '-q'], ['add', '.'], [*identity, 'commit', '-qm', 'initial'], ['worktree', 'add', '-q', '-b', 'probe', str(repo)]):
    subprocess.run(['git', *args], cwd=clone, check=True)
base_sha = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repo, text=True).strip()
# One large file, then a small one after it, so the diff's last line is a sentinel the model can only see whole.
(repo / 'bulk.txt').write_text(''.join(f'bulk line {n:05d} of the generated payload for the probe\n' for n in range(LINES)))
(repo / 'zz-last.txt').write_text('SENTINEL-END-OF-DIFF\n')
for name in ('bulk.txt', 'zz-last.txt'):
    subprocess.run(['git', 'add', name], cwd=repo, check=True)
    subprocess.run(['git', *identity, 'commit', '-qm', name], cwd=repo, check=True)
subprocess.run(['git', 'remote', 'add', 'origin', str(clone)], cwd=repo, check=True)
head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repo, text=True).strip()
review = LiveWorkspace(repo).review_diff(base_sha, head)
command = ReviewScope(review.command, None).commands[0]
schema = {'type': 'object', 'additionalProperties': False, 'required': ['handed_whole', 'sentinel_seen', 'reached_rest', 'how'],
          'properties': {'handed_whole': {'type': 'boolean'}, 'sentinel_seen': {'type': 'boolean'},
                         'reached_rest': {'type': 'boolean'}, 'how': {'type': 'string'}}}
prompt = f'''Diff size probe. Read the full PR diff with this one command in the checkout:
{review.command}

Run it with Bash exactly once, unchanged. Then report:
- handed_whole: whether the Bash result you were handed holds the entire diff output, or was cut or saved elsewhere.
- sentinel_seen: whether you have seen the line +SENTINEL-END-OF-DIFF, the last line of the diff.
- If you were not handed the whole diff, try to reach the rest with the tools you are allowed (Read, Glob, Grep). Do not run any other Bash command. Set reached_rest to whether you saw the rest, through bulk line {LINES - 1:05d} and the sentinel.
- how: exactly what the tool result said about size or truncation, any file path it named, and what each attempt to reach the rest returned.'''
adapter = ClaudeAdapter(ModelSpec.parse('claude-opus-5-5-high'))
call = Call(prompt, json.dumps(schema), False, attempt, checks=(command,))
(call.scratch).mkdir()
args = adapter.command(call)
result = subprocess.run(args, cwd=repo, env={**os.environ, **adapter.env, 'TMPDIR': str(call.scratch)}, text=True, capture_output=True)
stdout = json.loads(result.stdout)
evidence = {'argv': args, 'returncode': result.returncode, 'stdout': stdout, 'stderr': result.stderr,
            'settings': json.loads(Path(args[args.index('--settings') + 1]).read_text()),
            'review': {'command': review.command, 'patch_bytes': len(review.patch.encode()), 'patch_lines': review.patch.count('\n'),
                       'last_line': review.patch.rstrip('\n').rsplit('\n', 1)[-1]}}
project = re.sub(r'[^a-zA-Z0-9-]', '-', str(repo))
transcript = Path.home() / '.claude/projects' / project / f"{stdout['session_id']}.jsonl"
if transcript.exists():
    rows = [json.loads(line) for line in transcript.read_text().splitlines()]
    evidence['transcript'] = [{'type': row['type'], 'message': {'content': [c for c in row['message']['content'] if isinstance(c, dict) and c.get('type') in ('tool_use', 'tool_result')]}}
                              for row in rows if row.get('type') in ('assistant', 'user')]
    evidence['tool_use_results'] = [row['toolUseResult'] for row in rows if isinstance(row.get('toolUseResult'), dict)]
home = str(Path.home())
target = Path(__file__).parent / 'fixtures/claude/permissions/large-diff.json'
target.write_text(json.dumps(evidence).replace(project, '<project>').replace(str(base), '<probe>').replace(home, '<home>') + '\n')
print(result.returncode, stdout.get('structured_output'))
