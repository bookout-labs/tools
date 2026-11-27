"""Live capture for #446 K1 and T2-9: what each answer of a resumed Claude conversation reports, through ClaudeAdapter.command.

    uv run --python 3.12 python tests/capture-claude-resume-totals.py

On the configured Claude implement model: a parent, its fork, the fork's own fork (a chain of three), and a fork of the
parent that its spending cap stops. Each answer keeps only its numbers: the envelope's subtype, total_cost_usd and
usage, and its modelUsage rows by model name. No prompt, answer text, path or id is recorded.
"""
from decimal import Decimal
import json
import os
from pathlib import Path
import subprocess
import tempfile

from foreman.adapters import Call, Session
from foreman.adapters.claude import ClaudeAdapter
from foreman.config import load_config
from foreman.usd import BudgetUsd

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/claude/resume-totals.json"
SCHEMA = json.dumps({"type": "object", "properties": {"answer": {"type": "string", "enum": ["finished"]}},
                     "required": ["answer"], "additionalProperties": False})
TOKENS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")

model = next(spec for spec in load_config().nodes["implement"].models if spec.family == "claude")
adapter = ClaudeAdapter(model)
scratch = Path(tempfile.mkdtemp(prefix=".scratch.resume-totals.", dir=ROOT)).resolve()


def numbers(row: object) -> dict[str, object]:
    assert isinstance(row, dict)
    return {key: value for key, value in row.items() if isinstance(value, (int, float)) and not isinstance(value, bool)}


def ask(name: str, resume: Session | None, budget: str = "0.50") -> tuple[dict[str, object], Session | None]:
    attempt = scratch / name
    attempt.mkdir()
    call = Call("Return the answer finished.", SCHEMA, edit=False, temp_dir=attempt, budget_usd=BudgetUsd(Decimal(budget)), resume=resume)
    done = subprocess.run(adapter.command(call), cwd=scratch, env={**os.environ, **adapter.env, "TMPDIR": str(attempt)},
                          capture_output=True, text=True)
    envelope = json.loads(done.stdout)
    outcome = adapter.read(done, call)
    report = {"subtype": envelope["subtype"], "is_error": envelope["is_error"], "total_cost_usd": envelope["total_cost_usd"],
              "usage": {key: envelope["usage"].get(key) for key in TOKENS},
              "modelUsage": {name: numbers(row) for name, row in envelope["modelUsage"].items()},
              "adapter_outcome": outcome.kind}
    return report, outcome.session


parent, first = ask("parent", None)
fork, second = ask("fork", first)
third, _ = ask("fork_of_fork", second)
stopped, _ = ask("cap_stopped_fork", first, budget="0.0001")
cli = subprocess.run(["claude", "--version"], capture_output=True, text=True).stdout.strip()
FIXTURE.write_text(json.dumps({"cli": cli, "model": model.name, "parent": parent, "fork": fork, "fork_of_fork": third,
                               "cap_stopped_fork": stopped}, indent=2) + "\n")
print(FIXTURE)
