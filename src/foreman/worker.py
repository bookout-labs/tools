"""Run a pinned worker and read its final stage result."""

from collections.abc import Mapping
import json
import os
from pathlib import Path
import subprocess
import tempfile
from tempfile import TemporaryDirectory
from typing import Callable

from foreman.adapters import Call, CliAdapter, Outcome
from foreman.adapters.claude import ClaudeAdapter
from foreman.adapters.codex import CodexAdapter
from foreman.adapters.grok import GrokAdapter
from foreman.config import ConfigError, ModelSpec
from foreman.stage_output import STAGE_OUTPUTS

CommandRunner = Callable[[list[str], Path, Mapping[str, str]], subprocess.CompletedProcess[str]]

ADAPTERS: dict[str, Callable[[ModelSpec], CliAdapter]] = {
    "claude": ClaudeAdapter,
    "gpt": CodexAdapter,
    "grok": GrokAdapter,
}


def run_worker_command(args: list[str], cwd: Path, adapter_env: Mapping[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args, cwd=cwd, env={**os.environ, **adapter_env}, stdin=subprocess.DEVNULL, capture_output=True, text=True,
    )


class CliWorker:
    def __init__(self, cwd: Path, run: CommandRunner = run_worker_command):
        self.cwd = cwd
        self.run = run

    def __call__(self, node: str, model: ModelSpec | None, state: Mapping[str, object], instruction: str) -> Outcome:
        if model is None:
            raise ConfigError(f"{node} has no model configured")
        prompt = (
            f"{instruction}\n\nReturn exactly one JSON object that matches the output schema. "
            "Use null for inapplicable nullable fields. Do not arm a merge."
            " Write disposable scratch only under $TMPDIR, outside the checkout."
            " Do not return the scratch path as evidence."
        )
        contract = STAGE_OUTPUTS[node]
        schema = json.dumps(contract.model_json_schema())
        prompt += f"\n\nDelivery state:\n{json.dumps(state)}"

        adapter = ADAPTERS[model.family](model)
        checkout = self.cwd.resolve()
        temp_root = Path(tempfile.gettempdir()).resolve()
        if temp_root.is_relative_to(checkout):
            temp_root = checkout.parent
        with TemporaryDirectory(prefix="foreman-worker-", dir=temp_root) as directory:
            scratch = Path(directory) / "scratch"
            scratch.mkdir()
            args = adapter.command(Call(prompt, schema, edit=node == "implement", temp_dir=Path(directory)))
            try:
                process = self.run(args, self.cwd, {**adapter.env, "TMPDIR": str(scratch)})
            except FileNotFoundError:
                return Outcome("cli_missing", detail=f"{args[0]} executable is unavailable")
            return adapter.read(process)
