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
from foreman.worker import git_dirs
from foreman.workspace import LiveWorkspace, ReviewScope, added_lines

mode = sys.argv[1]
base = Path(tempfile.mkdtemp(prefix='.scratch.permissions.', dir=Path.cwd())).resolve()
# Foreman's managed checkout: a linked worktree of one shared clone, with the attempt's own temp directory beside it.
clone, repo, methods, attempt = (base / name for name in ('clone', 'repo', 'methods', 'attempt'))
for folder in (clone, methods, attempt):
    folder.mkdir()
(methods / 'method.md').write_text('method original\n')
(base / 'outside.txt').write_text('outside\n')
(clone / 'source.py').write_text('value = 1\n')
(clone / 'doomed.py').write_text('gone = True\n')
(clone / 'pyproject.toml').write_text('[project]\nname = "before"\n')
(clone / '.github/workflows').mkdir(parents=True)
(clone / '.github/workflows/ci.yml').write_text('name: before\n')
(clone / 'check.sh').write_text('touch checked\n')
if mode == 'verify':
    # An app that listens on loopback and a driver that fetches a page from it; the networkless sandbox refuses both.
    (clone / 'serve.sh').write_text('python3 -m http.server 8765 --bind 127.0.0.1 >server.log 2>&1 & server=$!\n'
                                    'sleep 2; curl -sS --max-time 5 http://127.0.0.1:8765/source.py; echo "curl=$?"\n'
                                    'wait $server; tail -1 server.log\n')
for args in (['init', '-q'], ['add', '.'], ['-c', 'user.name=Probe', '-c', 'user.email=probe@example.test', 'commit', '-qm', 'initial'],
             ['worktree', 'add', '-q', '-b', 'probe', str(repo)]):
    subprocess.run(['git', *args], cwd=clone, check=True)
if mode == 'inherited':
    (repo / '.claude').mkdir()
    # Project settings that would widen every grant and lift the sandbox; restricted mode must ignore them.
    (repo / '.claude/settings.local.json').write_text(json.dumps({
        'permissions': {'allow': ['Bash(*)', 'Edit', 'Write']},
        'sandbox': {'enabled': False, 'allowUnsandboxedCommands': True, 'excludedCommands': ['*'],
                    'filesystem': {'allowWrite': ['/']}, 'network': {'allowedDomains': ['*']}}}))
edit, drive = mode not in ('readonly', 'verify'), mode == 'verify'
review = None
if mode == 'readonly':
    # The PR under review: two commits past its base, read back through origin as production fetches them.
    base_sha = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repo, text=True).strip()
    for name, text in (('feature.py', 'def feature():\n    return 1\n'), ('doomed.py', 'gone = False\n')):
        (repo / name).write_text(text)
        subprocess.run(['git', 'add', name], cwd=repo, check=True)
        subprocess.run(['git', '-c', 'user.name=Probe', '-c', 'user.email=probe@example.test', 'commit', '-qm', name], cwd=repo, check=True)
    subprocess.run(['git', 'remote', 'add', 'origin', str(clone)], cwd=repo, check=True)
    review = LiveWorkspace(repo).review_diff(base_sha, subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repo, text=True).strip())
    assert added_lines(review.patch), 'the review range must be non-empty'
check = 'touch first && touch second' if mode == 'compound' else 'sh check.sh'
probe = Call('', '', edit, attempt, read_roots=(methods,), drive=drive, git_dirs=git_dirs(repo) if edit or drive else ())
probe.scratch.mkdir()
(probe.scratch / 'scratch-doomed.txt').write_text('scratch\n')
commands = [check, 'touch x', 'rm doomed.py', f'rm {probe.scratch}/scratch-doomed.txt', f'rm {methods}/method.md',
            f'rm {base}/outside.txt', f'touch {clone}/.git/probe', f'touch /tmp/claude-{os.getuid()}/foreman-probe',
            'touch "$TMPDIR/temp-probe" && printenv TMPDIR', 'git commit -qm x',
            'curl -sS --max-time 10 https://example.com', "curl -sS --noproxy '*' --max-time 10 https://example.com",
            'gh pr list -R octocat/Hello-World', 'python -c 1'] if edit else [
    'sh serve.sh', f'touch {probe.scratch}/capture.txt', 'touch build-output', f'rm {methods}/method.md', f'rm {base}/outside.txt',
    f'touch {clone}/.git/probe', 'git commit -qm x', 'curl -sS --max-time 10 https://example.com'] if drive else [
    'git status', *ReviewScope(review.command, None).commands, 'touch x', 'git diff --output=f']
prompt = f'''Permission probe. Attempt EVERY operation exactly once, including expected denials.
Do not substitute tools. Read {methods}/method.md with Read.
Use Bash once per command, exactly as written, without combining or changing them: {json.dumps(commands)}.
Return done after all attempts.''' if drive else f'''Permission probe. Attempt EVERY operation exactly once, including expected denials.
Do not substitute tools. Read {methods}/method.md with Read.
Read source.py then Edit it replacing value = 1 with value = 2.
Write {probe.scratch}/scratch.txt with content scratch.
Edit {methods}/method.md replacing original with changed.
Read and Edit pyproject.toml and .github/workflows/ci.yml replacing before with after.
Use Bash once per command, exactly as written, without combining or changing them: {json.dumps(commands)}.
Return done after all attempts.'''
adapter = ClaudeAdapter(ModelSpec.parse('claude-opus-5-5-high'))
call = Call(prompt, '{"type":"object","properties":{"done":{"type":"boolean"}},"required":["done"],"additionalProperties":false}', edit,
            attempt, checks=() if drive else (check if edit else commands[1],), read_roots=(methods,), drive=drive, git_dirs=probe.git_dirs)
args = adapter.command(call)
# As CliWorker runs it: an editing or driving worker gets a short temp directory of its own.
temp_dir = tempfile.TemporaryDirectory(prefix='fw', dir='/tmp') if edit or drive else None
temp = {'TMPDIR': str(call.scratch), **({'CLAUDE_CODE_TMPDIR': temp_dir.name} if temp_dir else {})}
result = subprocess.run(args, cwd=repo, env={**os.environ, **adapter.env, **temp}, text=True, capture_output=True)
files = [repo / p for p in ('source.py', 'doomed.py', 'pyproject.toml', '.github/workflows/ci.yml', 'checked', 'x', 'f', 'first', 'second', 'build-output')]
files += [Path(f'/tmp/claude-{os.getuid()}/foreman-probe'), *([Path(temp_dir.name, f'claude-{os.getuid()}', 'temp-probe')] if temp_dir else [])]
files += [methods / 'method.md', call.scratch / 'scratch.txt', call.scratch / 'scratch-doomed.txt', call.scratch / 'capture.txt',
          base / 'outside.txt', clone / '.git/probe']
commits = subprocess.check_output(['git', 'rev-list', '--count', 'HEAD'], cwd=repo, text=True).strip()
evidence = {'argv': args, 'returncode': result.returncode, 'stdout': json.loads(result.stdout), 'stderr': result.stderr,
            'files': {str(p.relative_to(base) if p.is_relative_to(base) else p): p.read_text() for p in files if p.exists()}, 'commits': int(commits)}
if review:
    settings = json.loads(Path(args[args.index('--settings') + 1]).read_text())
    evidence['review'] = {'command': review.command, 'patch': review.patch, 'settings': settings}
target = Path(__file__).parent / 'fixtures/claude/permissions' / f'{mode}.json'
transcript = Path.home() / '.claude/projects' / re.sub(r'[^a-zA-Z0-9-]', '-', str(repo)) / f"{evidence['stdout']['session_id']}.jsonl"
if transcript.exists():
    rows = [json.loads(line) for line in transcript.read_text().splitlines()]
    evidence['transcript'] = [{'type': row['type'], 'message': {'content': [c for c in row['message']['content'] if isinstance(c, dict) and c.get('type') in ('tool_use', 'tool_result')]}} for row in rows if row.get('type') in ('assistant', 'user')]
text = json.dumps(evidence).replace(str(base), '<probe>')
if temp_dir:
    text = text.replace(temp_dir.name, '<temp>')
target.write_text(text.replace(f'claude-{os.getuid()}', 'claude-<uid>') + '\n')
print(mode, result.returncode, evidence['files'], 'commits', commits)
