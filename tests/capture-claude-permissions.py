"""Live permission probes. Run outside unittest's offline policy."""
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile

from foreman.adapters import Call
from foreman.adapters.claude import ClaudeAdapter
from foreman.config import ModelSpec

mode = sys.argv[1]
base = Path(tempfile.mkdtemp(prefix='.scratch.permissions.', dir=Path.cwd())).resolve()
repo, methods, scratch = (base / name for name in ('repo', 'methods', 'scratch'))
for folder in (repo, methods, scratch):
    folder.mkdir()
(methods / 'method.md').write_text('method original\n')
(repo / 'source.py').write_text('value = 1\n')
(repo / 'pyproject.toml').write_text('[project]\nname = "before"\n')
(repo / '.github/workflows').mkdir(parents=True)
(repo / '.github/workflows/ci.yml').write_text('name: before\n')
(repo / 'check.sh').write_text('touch checked\n')
for args in (['init', '-q'], ['add', '.'], ['-c', 'user.name=Probe', '-c', 'user.email=probe@example.test', 'commit', '-qm', 'initial']):
    subprocess.run(['git', *args], cwd=repo, check=True)
if mode == 'inherited':
    (repo / '.claude').mkdir()
    (repo / '.claude/settings.local.json').write_text(json.dumps({'permissions': {'allow': ['Bash(*)', 'Edit', 'Write']}}))
head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repo, text=True).strip()
check = 'touch first && touch second' if mode == 'compound' else 'sh check.sh'
edit = mode != 'readonly'
commands = [check, 'touch x', 'git commit -m x', 'gh pr list', 'python -c 1'] if edit else [
    'git status', f'git diff --no-color --no-ext-diff --no-textconv --no-relative --src-prefix=a/ --dst-prefix=b/ --find-renames {head}...{head} --', 'touch x', 'git diff --output=f']
prompt = f'''Permission probe. Attempt EVERY operation exactly once, including expected denials.
Do not substitute tools. Read {methods}/method.md with Read.
Read source.py then Edit it replacing value = 1 with value = 2.
Write {scratch}/scratch.txt with content scratch.
Edit {methods}/method.md replacing original with changed.
Read and Edit pyproject.toml and .github/workflows/ci.yml replacing before with after.
Use Bash once per command, exactly as written, without combining or changing them: {json.dumps(commands)}.
Return done after all attempts.'''
adapter = ClaudeAdapter(ModelSpec.parse('claude-opus-5-5-high'))
call = Call(prompt, '{"type":"object","properties":{"done":{"type":"boolean"}},"required":["done"],"additionalProperties":false}', edit, scratch, check=check if edit else commands[1], read_roots=(methods,))
args = adapter.command(call)
result = subprocess.run(args, cwd=repo, env={**os.environ, **adapter.env, 'TMPDIR': str(scratch)}, text=True, capture_output=True)
files = [repo / p for p in ('source.py', 'pyproject.toml', '.github/workflows/ci.yml', 'checked', 'x', 'f', 'first', 'second')]
files += [methods / 'method.md', scratch / 'scratch.txt']
evidence = {'argv': args, 'returncode': result.returncode, 'stdout': json.loads(result.stdout), 'stderr': result.stderr,
            'files': {str(p.relative_to(base)): p.read_text() for p in files if p.exists()}}
target = Path(__file__).parent / 'fixtures/claude/permissions' / f'{mode}.json'
transcript = Path.home() / '.claude/projects' / re.sub(r'[^a-zA-Z0-9-]', '-', str(repo)) / f"{evidence['stdout']['session_id']}.jsonl"
if transcript.exists():
    rows = [json.loads(line) for line in transcript.read_text().splitlines()]
    evidence['transcript'] = [{'type': row['type'], 'message': {'content': [c for c in row['message']['content'] if isinstance(c, dict) and c.get('type') in ('tool_use', 'tool_result')]}} for row in rows if row.get('type') in ('assistant', 'user')]
target.write_text(json.dumps(evidence).replace(str(base), '<probe>') + '\n')
print(mode, result.returncode, evidence['files'])
