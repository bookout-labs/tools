"""Run a pinned worker and read its final stage result."""

from collections.abc import Mapping
import json
from pathlib import Path
import re
import subprocess
from typing import Callable

from foreman.adapters import QUOTA, Call, CliAdapter, Outcome
from foreman.adapters.claude import ClaudeAdapter
from foreman.adapters.grok import GrokAdapter
from foreman.config import family
from foreman.dispatch import InvalidOutput, QuotaExhausted
from foreman.stage_output import STAGE_OUTPUTS

CommandRunner = Callable[[list[str], Path], subprocess.CompletedProcess[str]]

ADAPTERS: dict[str, Callable[[str], CliAdapter]] = {
    "claude": ClaudeAdapter, "grok": GrokAdapter,
}


def run_worker_command(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, text=True)


def json_object(text: object) -> dict[str, object]:
    if not isinstance(text, (str, bytes, bytearray)):
        raise InvalidOutput("worker did not return a JSON object")
    try:
        result: object = json.loads(text)
    except ValueError as exc:
        raise InvalidOutput("worker did not return a JSON object") from exc
    if not isinstance(result, dict):
        raise InvalidOutput("worker did not return a JSON object")
    return result


class CliWorker:
    def __init__(self, cwd: Path, run: CommandRunner = run_worker_command):
        self.cwd = cwd
        self.run = run

    def __call__(self, node: str, model: str | None, state: Mapping[str, object], instruction: str) -> dict[str, object] | Outcome:
        if model is None:
            raise QuotaExhausted(model)
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

        provider = family(model)
        adapter = ADAPTERS[provider](model) if provider in ADAPTERS else None
        edit = node == "implement"
        if adapter is None:
            sandbox = "workspace-write" if edit else "read-only"
            args = ["codex", "exec", "-m", model, "--color", "never", "-s", sandbox, prompt]
        else:
            args = adapter.command(Call(prompt, schema, edit))

        try:
            process = self.run(args, self.cwd)
        except FileNotFoundError:
            return Outcome("cli_missing", detail=f"{args[0]} executable is unavailable")
        if adapter is not None:
            return adapter.read(process)
        if process.returncode != 0:
            if QUOTA.search(f"{process.stderr}\n{process.stdout}"):
                raise QuotaExhausted(model)
            raise InvalidOutput(f"{provider} worker exited {process.returncode}")

        reported = re.findall(r"^model:[ \t]*(\S+)[ \t]*$", process.stderr, re.MULTILINE)
        if reported != [model] or re.search(r"^model rerouted:", process.stderr, re.MULTILINE):
            raise QuotaExhausted(model)
        return json_object(process.stdout)
