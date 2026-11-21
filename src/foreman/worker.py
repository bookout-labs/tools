"""Run a pinned worker and read its final stage result."""

from collections.abc import Mapping
import json
import os
from pathlib import Path
import subprocess
from typing import Callable

from foreman.adapters import Call, CliAdapter, Outcome
from foreman.adapters.claude import ClaudeAdapter
from foreman.adapters.codex import CodexAdapter
from foreman.adapters.grok import GrokAdapter
from foreman.config import ConfigError, ModelSpec
from foreman.stage_output import STAGE_OUTPUTS, Contract

CommandRunner = Callable[[list[str], Path, Mapping[str, str]], subprocess.CompletedProcess[str]]

ADAPTERS: dict[str, Callable[[ModelSpec, type[Contract]], CliAdapter]] = {
    "claude": lambda model, _: ClaudeAdapter(model),
    "gpt": CodexAdapter,
    "grok": lambda model, _: GrokAdapter(model),
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
        )
        contract = STAGE_OUTPUTS[node]
        schema = json.dumps(contract.model_json_schema())
        if model.family == "gpt":
            prompt += f"\n\nOutput schema:\n{schema}"
        prompt += f"\n\nDelivery state:\n{json.dumps(state)}"

        adapter = ADAPTERS[model.family](model, contract)
        args = adapter.command(Call(prompt, schema, edit=node == "implement"))
        try:
            process = self.run(args, self.cwd, adapter.env)
        except FileNotFoundError:
            return Outcome("cli_missing", detail=f"{args[0]} executable is unavailable")
        return adapter.read(process)
