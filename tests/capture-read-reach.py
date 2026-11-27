"""Read-reach probes (#438): what a worker's shell, and Claude's Read tool, can read outside its checkout and roots.

Run outside unittest's offline policy: `.venv/bin/python tests/capture-read-reach.py codex <readonly|edit>`, or
`claude <readonly|edit|verify> [adapter] [block-reads] [sandbox-block-reads]` (one model call per variant).
Host files are probed by byte count only; their contents never enter a capture.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Any, cast

from foreman.adapters import Call
from foreman.adapters.claude import ClaudeAdapter
from foreman.adapters.codex import CodexAdapter
from foreman.config import ModelSpec
from foreman.worker import git_dirs

family, mode = sys.argv[1:3]
# The host's per-user temp directory, read before the probe points any process's TMPDIR elsewhere.
host_temp = [str(Path(tempfile.gettempdir()).resolve()), tempfile.gettempdir()]
base = Path(tempfile.mkdtemp(prefix='.scratch.reach.', dir=Path.cwd())).resolve()
clone, repo, methods, outside, attempt = (base / n for n in ('clone', 'repo', 'methods', 'outside', 'attempt'))
for folder in (clone, methods, outside, attempt):
    folder.mkdir()
(methods / 'method.md').write_text('method original\n')
synthetic = outside / 'outside.txt'
synthetic.write_text('outside synthetic\n')
(clone / 'source.py').write_text('value = 1\n')
(clone / 'check.sh').write_text('touch checked\n')
for args in (['init', '-q'], ['add', '.'], ['-c', 'user.name=Probe', '-c', 'user.email=probe@example.test', 'commit', '-qm', 'initial'],
             ['worktree', 'add', '-q', '-b', 'probe', str(repo)]):
    subprocess.run(['git', *args], cwd=clone, check=True)
head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repo, text=True).strip()
diff = f'git diff --no-color --no-ext-diff --no-textconv --no-relative --src-prefix=a/ --dst-prefix=b/ --find-renames {head}...{head} --'
edit, drive = mode == 'edit', mode == 'verify'
home_files = [Path.home() / '.zshrc', Path.home() / '.codex/auth.json']
reads = {'outside': [f'wc -c {synthetic}', f'cat {synthetic} | wc -c'],
         'home': [form for path in home_files for form in (f'wc -c {path}', f'cat {path} | wc -c')],
         'system': ['wc -c /etc/hosts'],
         # The same home file named in ways a command parser may not resolve to a path.
         'indirect': ['wc -c "$HOME/.zshrc"', 'cd ~ && wc -c .zshrc', 'git diff --no-index --numstat /dev/null ~/.zshrc']}
work = [diff, 'command -v git', 'git status --porcelain', 'git log -1 --format=%H', f'cat {methods}/method.md', 'python3 -c "print(1)"',
        *(['sh check.sh'] if edit else [])]
call = Call('', '{"type":"object","properties":{"done":{"type":"boolean"}},"required":["done"],"additionalProperties":false}',
            edit, attempt, checks=('sh check.sh',) if edit else () if drive else (diff,), read_roots=(methods,), drive=drive,
            git_dirs=git_dirs(repo) if edit or drive else ())
call.scratch.mkdir()


def scrub(text: str) -> str:
    for temp in host_temp:
        text = text.replace(temp, '<tmp>')
    return text.replace(str(base), '<probe>').replace(str(Path.home()), '~').replace(str(Path.home()).lstrip('/'), '~')


def count(output: str) -> int | None:
    """The byte count a `wc -c` probe printed."""
    found = [int(m.group(1)) for line in output.splitlines() if (m := re.fullmatch(r'\s*(\d+)(?:\s+\S+)?\s*', line))]
    return found[0] if found else None


def kernel(profile: str, extra_env: dict[str, str], config: list[str]) -> list[dict[str, object]]:
    """Each probe under `codex sandbox` with the worker's exact profile: no model call."""
    env = {**os.environ, 'CODEX_HOME': str(base / 'kernel-home'), 'TMPDIR': str(call.scratch), **extra_env}
    (base / 'kernel-home').mkdir(exist_ok=True)
    rows: list[dict[str, object]] = []
    for kind, commands in (*reads.items(), ('work', work)):
        for command in commands:
            r = subprocess.run(['codex', 'sandbox', '-P', 'foreman', '-c', profile, *config, '-C', str(repo), '--', '/bin/sh', '-c', command],
                               env=env, text=True, capture_output=True)
            # Each read probe prints counts, names and errors only, never a host file's contents.
            rows.append({'kind': kind, 'command': command, 'returncode': r.returncode, 'bytes': count(r.stdout) if kind != 'work' else None,
                         'stdout': r.stdout[-600:], 'stderr': r.stderr[-400:]})
    return rows


def codex() -> dict[str, object]:
    args = CodexAdapter(ModelSpec.parse('gpt-6.2-sol-high')).command(call)
    profile = args[args.index('default_permissions=foreman') + 2]
    workspace = '' if edit else '":workspace_roots"={"."="read"},'
    # `:root` denied, then `:minimal` (the platform paths common tools need) and the workspace re-opened.
    confine = '":root"="deny",":minimal"="read",' + workspace
    homebrew = confine + '"/opt/homebrew"="read",'
    # The shared clone a linked worktree's .git points to, which Foreman already resolves for every checkout.
    shared = homebrew + ''.join(json.dumps(str(path)) + '="read",' for path in git_dirs(repo)[1:])
    no_global = {'GIT_CONFIG_GLOBAL': '/dev/null'}
    variants: dict[str, tuple[str, dict[str, str], list[str]]] = {
        'adapter': ('', {}, []),
        'root-deny-minimal': (confine, {}, []),
        # The Homebrew prefix the host's git and python3 come from.
        'root-deny-minimal-homebrew': (homebrew, {}, []),
        # Git treats an unreadable global config as fatal: re-open that one file.
        'root-deny-gitconfig': (homebrew + json.dumps(str(Path.home() / '.gitconfig')) + '="read",', {}, []),
        # Or keep it closed and point git at no global config.
        'root-deny-no-global-git-config': (homebrew, no_global, []),
        'root-deny-no-global-git-config-shared-clone': (shared, no_global, []),
        # The same variable set through Codex's own shell environment policy instead of the caller's environment.
        'root-deny-shell-environment-policy': (shared, {}, ['-c', 'shell_environment_policy.set.GIT_CONFIG_GLOBAL="/dev/null"']),
    }
    profiles = {}
    for name, (entries, extra_env, config) in variants.items():
        p = profile.replace('filesystem={', 'filesystem={' + entries)
        profiles[name] = {'profile': p, 'env': extra_env, 'config': config, 'probes': kernel(p, extra_env, config)}
    return {'version': subprocess.check_output(['codex', '--version'], text=True).strip(), 'profiles': profiles}


def claude(variant: str) -> dict[str, object]:
    """One minimal model call; the permission and sandbox settings are the worker's, whatever the model."""
    absent = Path.home() / '.foreman-read-probe-absent'
    commands = [*(c for cs in reads.values() for c in cs), *work]
    prompt = f'''Permission probe. Attempt EVERY operation exactly once, including expected denials. Never retry, never substitute
another tool or command, never print file contents. Use Read on each of these paths: {json.dumps([str(synthetic), str(absent), f"{methods}/method.md", f"{repo}/source.py"])}.
Use Bash once per command, exactly as written, without combining or changing them: {json.dumps(commands)}.
Return done after all attempts.'''
    adapter = ClaudeAdapter(ModelSpec.parse('claude-haiku-5-5'))
    args = adapter.command(Call(prompt, call.schema, edit, attempt, checks=call.checks, read_roots=call.read_roots, drive=drive,
                                git_dirs=call.git_dirs))
    settings_path = Path(args[args.index('--settings') + 1])
    settings = json.loads(settings_path.read_text())
    if variant == 'block-reads':
        settings['permissions']['blockReadsOutsideWorkingDirectories'] = True
    elif variant == 'sandbox-block-reads':
        settings['permissions']['blockReadsOutsideWorkingDirectories'] = True
        settings['sandbox'] = {'enabled': True, 'autoAllowBashIfSandboxed': False, 'failIfUnavailable': True,
                               'allowUnsandboxedCommands': False, 'network': {'allowedDomains': [], 'strictAllowlist': True},
                               'filesystem': {'denyWrite': [str(repo), str(clone), str(methods)]}}
    settings_path.write_text(json.dumps(settings))
    # Claude falls back to its shared per-user temp directory: this probe writes nothing under /tmp itself.
    env = {**os.environ, **adapter.env, 'TMPDIR': str(call.scratch), 'CLAUDE_CODE_TMPDIR': str(attempt)}
    result = subprocess.run(args, cwd=repo, env=env, text=True, capture_output=True, stdin=subprocess.DEVNULL)
    envelope = json.loads(result.stdout)
    transcript = Path.home() / '.claude/projects' / re.sub(r'[^a-zA-Z0-9-]', '-', str(repo)) / f"{envelope['session_id']}.jsonl"
    # Transcript rows are Claude's untyped JSON; only tool calls and their results are read.
    uses: dict[str, Any] = {}
    results: dict[str, Any] = {}
    rows: list[Any] = [json.loads(line) for line in transcript.read_text().splitlines()]
    for row in rows:
        content: Any = row.get('message', {}).get('content') if row.get('type') in ('assistant', 'user') else None
        items = cast(list[Any], content) if isinstance(content, list) else []
        for item in items:
            if item.get('type') == 'tool_use':
                uses[item['id']] = item
            elif item.get('type') == 'tool_result':
                results[item['tool_use_id']] = item
    expected_reads = {str(synthetic), str(absent), f'{methods}/method.md', f'{repo}/source.py'}
    probes: list[dict[str, object]] = []
    for use_id, use in uses.items():
        tool, given = use['name'], use.get('input', {})
        target = given.get('command') if tool == 'Bash' else given.get('file_path') if tool == 'Read' else None
        outcome = results.get(use_id, {})
        raw: Any = outcome.get('content')
        blocks: list[Any] = [] if isinstance(raw, str) else raw or []
        text: str = raw if isinstance(raw, str) else ' '.join(str(block.get('text', '')) for block in blocks)
        kind = next((k for k, cs in (*reads.items(), ('work', work)) if target in cs), None) if tool == 'Bash' else \
            ('outside' if target in (str(synthetic), str(absent)) else 'work') if target in expected_reads else None
        if kind is None:
            probes.append({'tool': tool, 'target': '[unexpected call, redacted]', 'is_error': outcome.get('is_error')})
            continue
        # The exact probe commands print counts, names and errors only; any other call was redacted above.
        probes.append({'tool': tool, 'kind': kind, 'target': target, 'is_error': bool(outcome.get('is_error')),
                       'bytes': count(text) if tool == 'Bash' and kind != 'work' else None, 'result': text[:600]})
    return {'argv': args, 'settings': settings, 'returncode': result.returncode, 'stderr': result.stderr[-400:],
            'envelope': {k: envelope.get(k) for k in ('is_error', 'num_turns', 'total_cost_usd', 'structured_output')},
            'probes': probes}


if family == 'codex':
    capture = codex()
else:
    capture = {'version': subprocess.check_output(['claude', '--version'], text=True).strip(),
               'variants': {variant: claude(variant) for variant in sys.argv[3:] or ['adapter']}}
target = Path(__file__).parent / 'fixtures' / family / 'permissions' / f'read-reach-{mode}.json'
target.write_text(scrub(json.dumps(capture, indent=1)) + '\n')
print(target)
