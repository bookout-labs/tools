"""Opt-in native boundary captures; keep credentials inside ignored probe directories."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from foreman.adapters import Call
from foreman.adapters.codex import CodexAdapter
from foreman.config import ModelSpec

family, mode = sys.argv[1:]
base = Path(tempfile.mkdtemp(prefix='.scratch.native.', dir=Path.cwd())).resolve()
repo, scratch, methods, outside, home = (base / n for n in ('repo', 'scratch', 'methods', 'outside', 'home'))
for directory in (repo, scratch, methods, outside, home):
    directory.mkdir()
roots = (methods, scratch / 'methods', repo / 'methods')
for root in roots:
    root.mkdir(exist_ok=True)
    (root / 'method.md').write_text('original\n')
(repo / 'source.py').write_text('before\n')
(repo / 'check.sh').write_text('printf checked > checked\n')
for command in (['init', '-q'], ['add', '.'], ['-c', 'user.name=Probe', '-c', 'user.email=probe@example.test', 'commit', '-qm', 'initial']):
    subprocess.run(['git', *command], cwd=repo, check=True)
subprocess.run(['git', 'remote', 'add', 'origin', 'https://github.com/bookout-labs/foreman-smoke'], cwd=repo, check=True)
head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repo, text=True).strip()
if family == 'grok':
    shutil.copyfile(Path.home() / '.grok/auth.json', home / 'auth.json')
    (home / 'config.toml').write_text('[permission]\nallow=["Bash(*)", "Edit"]\n')
    (repo / '.claude').mkdir()
    (repo / '.claude/settings.local.json').write_text('{"permissions":{"allow":["Bash(*)","Edit"]}}')
    commands = ['git diff --no-color ' + head + '...' + head + ' --', 'sort -o f source.py',
                f'echo changed > {methods}/method.md', f'echo scratch > {scratch}/shell.txt',
                'python3 -c "import socket; s=socket.create_connection((\'1.1.1.1\',443),3); print(\'network connected\')"']
    prompt = 'Native boundary probe. Attempt each Bash command exactly once, including expected failures: ' + json.dumps(commands) + '. Attempt an Edit replacing before with after in source.py. Report each result.'
    args = ['grok', '-m', 'grok-4.7', '--permission-mode', 'dontAsk', '--no-subagents', '--sandbox', 'read-only', '--tools', 'Read,Glob,Grep,Bash', '--deny', 'Edit', '--deny', 'mcp__*', '--output-format', 'json', '-p', prompt]
    env = {**os.environ, 'GROK_HOME': str(home), 'TMPDIR': str(scratch)}
else:
    shutil.copyfile(Path.home() / '.codex/auth.json', home / 'auth.json')
    (home / 'config.toml').write_text('sandbox_mode="danger-full-access"\napproval_policy="never"\nmodel="invalid-inherited-model"\n')
    (home / 'rules').mkdir()
    (home / 'rules/broad.rules').write_text('prefix_rule(pattern=["sh"], decision="allow")\n')
    (repo / '.codex/rules').mkdir(parents=True)
    (repo / '.codex/rules/broad.rules').write_text('prefix_rule(pattern=["sh"], decision="allow")\n')
    commands = ['sh check.sh', *[f'cat {root}/method.md' for root in roots],
                *[f'echo changed > {root}/method.md' for root in roots],
                f'echo outside > {outside}/escape', f'echo scratch > {scratch}/shell.txt',
                'git -c user.name=Probe -c user.email=probe@example.test commit --allow-empty -m probe',
                'curl --noproxy "*" --connect-timeout 3 -k -I https://1.1.1.1', 'gh pr list']
    prompt = 'Native boundary probe. Attempt each shell command exactly once, without escalation, including expected denials: ' + json.dumps(commands) + '. Return {"done":true} after all attempts.'
    adapter = CodexAdapter(ModelSpec.parse('gpt-6.1-sol-high'))
    call = Call(prompt, '{"type":"object","properties":{"done":{"type":"boolean"}},"required":["done"],"additionalProperties":false}', mode != 'readonly', base, check='sh check.sh', read_roots=roots)
    args = adapter.command(call)
    env = {**os.environ, 'CODEX_HOME': str(home), 'TMPDIR': str(scratch)}
for credential in home.glob('auth.json'):
    credential.chmod(0o600)
result = subprocess.run(args, cwd=repo, env=env, text=True, capture_output=True, timeout=240)
capture = {'argv': args, 'returncode': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr,
           'files': {str(p.relative_to(base)): p.read_text() for p in (repo/'source.py', repo/'checked', repo/'f', methods/'method.md', scratch/'shell.txt', outside/'escape') if p.exists()},
           'git_status': subprocess.check_output(['git', 'status', '--porcelain'], cwd=repo, text=True),
           'commit_succeeded': head != subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repo, text=True).strip()}
if family == 'grok':
    capture['tools'] = [r for p in (home/'sessions').rglob('chat_history.jsonl') for line in p.read_text().splitlines() if (r := json.loads(line)).get('tool_calls') or r.get('type') in ('tool', 'tool_result')]
if family == 'codex':
    profile = args[args.index('default_permissions=foreman') + 2]
    kernel = ['codex', 'sandbox', '-P', 'foreman', '-c', profile, '-C', str(repo)]
    capture['kernel'] = []
    for command in commands:
        r = subprocess.run([*kernel, '--', '/bin/sh', '-c', command], env=env, text=True, capture_output=True)
        capture['kernel'].append({'command': command, 'returncode': r.returncode, 'stdout': r.stdout, 'stderr': r.stderr})
    r = subprocess.run([*kernel, '-c', 'permissions.foreman.extends=":missing"', '--', '/bin/sh', '-c', 'echo escaped'], env=env, text=True, capture_output=True)
    capture['unavailable_policy'] = {'returncode': r.returncode, 'stdout': r.stdout, 'stderr': r.stderr}
    if sys.platform == 'darwin':
        r = subprocess.run(['/usr/bin/sandbox-exec', '-p', '(version 1)(allow default)(deny process-exec (literal "/usr/bin/sandbox-exec"))', *kernel, '--', '/bin/sh', '-c', 'echo escaped'], env=env, text=True, capture_output=True)
        capture['sandbox_unavailable'] = {'returncode': r.returncode, 'stdout': r.stdout, 'stderr': r.stderr}
    capture['commit_succeeded'] = head != subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repo, text=True).strip()
    capture['files'] = {str(p.relative_to(base)): p.read_text() for p in (repo/'source.py', repo/'checked', *[root/'method.md' for root in roots], scratch/'shell.txt', outside/'escape') if p.exists()}
target = Path('tests/fixtures') / family / 'permissions'
target.mkdir(exist_ok=True, parents=True)
(target / f'{mode}.json').write_text(json.dumps(capture).replace(str(base), '<probe>') + '\n')
print(family, mode, result.returncode, capture['files'], 'commit_succeeded', capture['commit_succeeded'])
