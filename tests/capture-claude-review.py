"""Live Claude review stream capture. Run outside unittest's offline policy: `uv run python tests/capture-claude-review.py`."""
from decimal import Decimal
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

from foreman.adapters import Call
from foreman.adapters.claude import ClaudeAdapter
from foreman.config import ModelSpec
from foreman.usd import BudgetUsd
from foreman.workspace import LiveWorkspace

root = Path(tempfile.mkdtemp(prefix='.scratch.review.', dir=Path.cwd())).resolve()
repo, attempt = root / 'repo', root / 'attempt'
repo.mkdir()
attempt.mkdir()


def git(*args: str) -> str:
    return subprocess.check_output(['git', '-c', 'user.name=Probe', '-c', 'user.email=probe@example.test', *args], cwd=repo, text=True).strip()


git('init', '-q', '-b', 'main')
(repo / 'package.json').write_text(json.dumps({'scripts': {'check': 'node check.js', 'test': 'node test.js'}}) + '\n')
(repo / 'app.js').write_text('export const total = (a, b) => a + b;\n')
git('add', '.')
git('commit', '-qm', 'Base')
base = git('rev-parse', 'HEAD')
(repo / 'app.js').write_text('export const total = (a, b) => a - b;\n')
git('commit', '-qam', 'Head')
head = git('rev-parse', 'HEAD')
git('remote', 'add', 'origin', str(repo))
scope = LiveWorkspace(repo).review_scope(base, head)
diff, = scope.required
# Exactly the extra command app #1376's reviewer ran. Claude Code 2.1.329 lets the required diff run piped through head,
# so the piped call reads a truncated diff without a denial.
extra = 'node -e \'const s=require("./package.json").scripts; for (const k of ["check","test"]) console.log(k, "=>", s[k])\''
prompts = {
    'read': f'Review probe. Use Bash once per command, exactly as written, in this order, including an expected denial:'
            f' {json.dumps([diff, extra])}. Then return done true.',
    'piped': f'Review probe. Never run this command as written: {diff}. Use Bash exactly once for this command:'
             f' {json.dumps(diff + " | head -5")}. Then read app.js with Read and return done true.',
}
adapter = ClaudeAdapter(ModelSpec.parse('claude-opus-5-5-low'))
schema = '{"type":"object","properties":{"done":{"type":"boolean"}},"required":["done"],"additionalProperties":false}'
calls: dict[str, object] = {}
for name, prompt in prompts.items():
    call = Call(prompt, schema, False, attempt, budget_usd=BudgetUsd(Decimal('0.40')), checks=scope.commands, review=True,
                required=scope.required)
    args = adapter.command(call)
    result = subprocess.run(args, cwd=repo, env={**os.environ, **adapter.env}, text=True, capture_output=True, stdin=subprocess.DEVNULL)
    # The system init event lists this host's skills, plugins and paths; the reader never uses it.
    events = [line for line in result.stdout.splitlines() if line.strip() and json.loads(line).get('type') != 'system']
    calls[name] = {'returncode': result.returncode, 'stdout': '\n'.join(events) + '\n', 'kind': adapter.read(result, call).kind}
version = subprocess.check_output(['claude', '--version'], text=True).split()[0]
text = json.dumps({'cli_version': version, 'scope': scope.command, 'calls': calls}, indent=1)
text = re.sub(r'[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}', '<uuid>', text.replace(str(root), '<probe>').replace(str(Path.home()), '<home>'))
(Path(__file__).parent / 'fixtures/claude/review-stream.json').write_text(text + '\n')
print({name: (c['returncode'], c['kind']) for name, c in calls.items() if isinstance(c, dict)})
