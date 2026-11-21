"""Run a pinned worker and read its final stage result."""

from collections.abc import Mapping
import json
from pathlib import Path
import re
import subprocess
from typing import Callable

from foreman.config import family
from foreman.dispatch import InvalidOutput, QuotaExhausted
from foreman.stage_output import STAGE_OUTPUTS

CommandRunner = Callable[[list[str], Path], subprocess.CompletedProcess[str]]

QUOTA = re.compile(
    r"quota|rate.limit|usage limit|hit your limit|resource_exhausted|too many requests|\b429\b|"
    r"model[^\n]*(?:not (?:available|found|supported)|does not exist|unsupported)|"
    r"(?:unknown|unsupported|invalid) model",
    re.IGNORECASE,
)


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

    def __call__(self, node: str, model: str | None, state: Mapping[str, object], instruction: str) -> dict[str, object]:
        if model is None:
            raise QuotaExhausted(model)
        prompt = (
            f"{instruction}\n\nReturn exactly one JSON object that matches the output schema. "
            "Use null for inapplicable nullable fields. Do not arm a merge."
        )
        if node in {"triage", "spec"} and state.get("issue_kind") == "slice" and "slice" not in state:
            prompt += " For a ready slice, include slice. For needs_human, include human_question."
        prompt += f"\n\nOutput schema:\n{json.dumps(STAGE_OUTPUTS[node].model_json_schema())}"
        prompt += f"\n\nDelivery state:\n{json.dumps(state)}"

        provider = family(model)
        edit = node == "implement"
        if provider == "gpt":
            sandbox = "workspace-write" if edit else "read-only"
            args = ["codex", "exec", "-m", model, "--color", "never", "-s", sandbox, prompt]
        else:
            mode = "acceptEdits" if edit else "plan"
            options = ["--output-format", "json", "--permission-mode", mode]
            if provider == "claude":
                args = ["claude", "--model", model, *options, "-p", prompt]
            else:
                args = ["grok", "-m", model, *options, "--no-subagents", "-p", prompt]

        try:
            process = self.run(args, self.cwd)
        except FileNotFoundError as exc:
            raise QuotaExhausted(model) from exc
        if process.returncode != 0:
            if QUOTA.search(f"{process.stderr}\n{process.stdout}"):
                raise QuotaExhausted(model)
            raise InvalidOutput(f"{provider} worker exited {process.returncode}")

        if provider == "gpt":
            reported = re.findall(r"^model:[ \t]*(\S+)[ \t]*$", process.stderr, re.MULTILINE)
            if reported != [model] or re.search(r"^model rerouted:", process.stderr, re.MULTILINE):
                raise QuotaExhausted(model)
            body = process.stdout
        else:
            envelope = json_object(process.stdout)
            if envelope.get("is_error") or envelope.get("type") == "error":
                if QUOTA.search(process.stdout):
                    raise QuotaExhausted(model)
                raise InvalidOutput(f"{provider} worker reported an error")
            usage = envelope.get("modelUsage")
            if not isinstance(usage, dict) or usage.keys() != {model}:
                raise QuotaExhausted(model)
            body = envelope.get("result" if provider == "claude" else "text")

        return json_object(body)
