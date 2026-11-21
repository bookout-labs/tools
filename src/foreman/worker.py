"""Run a pinned worker and read its final stage result."""

from collections.abc import Mapping
import json
from pathlib import Path
import subprocess
from typing import Callable

from foreman.adapters import Call, CliAdapter, Outcome
from foreman.adapters.claude import ClaudeAdapter
from foreman.adapters.codex import CodexAdapter
from foreman.adapters.grok import GrokAdapter
from foreman.config import ModelSpec
from foreman.stage_output import STAGE_OUTPUTS, Contract

CommandRunner = Callable[[list[str], Path], subprocess.CompletedProcess[str]]

ADAPTERS: dict[str, Callable[[ModelSpec, type[Contract]], CliAdapter]] = {
    "claude": ClaudeAdapter, "gpt": CodexAdapter, "grok": GrokAdapter,
}


def run_worker_command(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, text=True)


class CliWorker:
    def __init__(self, cwd: Path, run: CommandRunner = run_worker_command):
        self.cwd = cwd
        self.run = run

    def __call__(self, node: str, model: ModelSpec | None, state: Mapping[str, object], instruction: str) -> Outcome:
        if model is None:
            return Outcome("quota")
        prompt = (
            f"{instruction}\n\nReturn exactly one JSON object that matches the output schema. "
            "Use null for inapplicable nullable fields. Do not arm a merge."
        )
        if node in {"triage", "spec"} and state.get("issue_kind") == "slice" and "slice" not in state:
            prompt += " For a ready slice, include slice. For needs_human, include human_question."
        contract = STAGE_OUTPUTS[node]
        schema = json.dumps(contract.model_json_schema())
        prompt += f"\n\nOutput schema:\n{schema}"
        prompt += f"\n\nDelivery state:\n{json.dumps(state)}"

        adapter = ADAPTERS[model.family](model, contract)
        args = adapter.command(Call(prompt, schema, edit=node == "implement"))
        try:
            process = self.run(args, self.cwd)
        except FileNotFoundError:
            return Outcome("cli_missing", detail=f"{args[0]} executable is unavailable")
        return adapter.read(process)
