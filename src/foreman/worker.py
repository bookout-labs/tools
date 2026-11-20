"""Run a pinned worker and read its final stage result."""

import json
from pathlib import Path
import re
import subprocess
from typing import Callable

from foreman.config import EFFORTS, ROUTES, family
from foreman.dispatch import InvalidOutput, QuotaExhausted

CommandRunner = Callable[[list[str], Path], subprocess.CompletedProcess[str]]

REQUIRED = {
    "triage": ("triage",),
    "spec": ("spec", "spec_gate"),
    "research": ("research",),
    "implement": ("implementation",),
    "review": ("review", "risk"),
}
OPTIONAL = {
    "triage": ("slice", "human_question"),
    "spec": ("slice", "slices", "human_question"),
    "implement": ("head_sha", "browser_proof", "formal_proof"),
}
SLICE_PROMPT = (
    "For a ready slice, include slice with title, acceptance, depends_on (empty list), "
    "screen (docs/screens/<file>.md or empty), and properties (list of name and constraint). "
    "Preserve the proof obligations in the request. For needs_human, include human_question."
)
PROOF_PROMPT = (
    "For a required screen, report browser_proof with screen, trace, and passed. "
    "For required properties, report formal_proof with ran and constraints, a list of "
    "constraint and rejected results. Report evidence you actually collected."
)
def model_and_effort(model: str, requested: object = None) -> tuple[str, str | None]:
    slug, sep, effort = model.rpartition("-")
    if sep and slug and effort in EFFORTS:
        return slug, effort
    if isinstance(requested, str) and requested in EFFORTS:
        return model, requested
    return model, None


def _usage_matches(model: str, usage: object) -> bool:
    if not isinstance(usage, dict) or not usage:
        return False
    if set(usage) == {model}:
        return True
    # An alias such as opus comes back as the CLI's concrete slug.
    if model not in ROUTES:
        return False
    expected = family(model)
    return all(isinstance(key, str) and family(key) == expected and model in key.split("-") for key in usage)


QUOTA = re.compile(
    r"quota|rate.limit|usage limit|hit your limit|resource_exhausted|too many requests|\b429\b|"
    r"model[^\n]*(?:not (?:available|found|supported)|does not exist|unsupported)|"
    r"(?:unknown|unsupported|invalid) model",
    re.IGNORECASE,
)


def run_worker_command(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, text=True)


def json_object(text: str) -> dict:
    try:
        result = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise InvalidOutput("worker did not return a JSON object") from exc
    if not isinstance(result, dict):
        raise InvalidOutput("worker did not return a JSON object")
    return result


class CliWorker:
    def __init__(self, cwd: Path, run: CommandRunner = run_worker_command):
        self.cwd = cwd
        self.run = run

    def __call__(self, node: str, model: str | None, state: dict, instruction: str) -> dict:
        if model is None:
            raise QuotaExhausted(model)
        required = REQUIRED[node]
        optional = OPTIONAL.get(node, ())
        prompt = (
            f"{instruction}\n\nReturn exactly one JSON object without surrounding text. "
            f"Required keys: {', '.join(required)}. Optional keys: {', '.join(optional) or 'none'}. "
            "Write stage decisions as values of their corresponding keys. "
            "Do not arm a merge."
        )
        if node in {"triage", "spec"} and state.get("issue_kind") == "slice" and "slice" not in state:
            prompt += f" Use needs_human if you cannot determine the issue requirements. {SLICE_PROMPT}"
        if node == "implement":
            prompt += f" {PROOF_PROMPT}"
        prompt += f"\n\nDelivery state:\n{json.dumps(state)}"

        provider = family(model)
        slug, effort = model_and_effort(model, state.get("effort"))
        edit = node == "implement"
        if provider == "gpt":
            sandbox = "workspace-write" if edit else "read-only"
            effort_args = ["-c", f"model_reasoning_effort={effort}"] if effort else []
            args = ["codex", "exec", "-m", slug, *effort_args, "--color", "never", "-s", sandbox, prompt]
        else:
            mode = "acceptEdits" if edit else "plan"
            options = ["--output-format", "json", "--permission-mode", mode]
            if provider == "claude":
                effort_args = ["--effort", effort] if effort else []
                args = ["claude", "--model", slug, *effort_args, *options, "-p", prompt]
            else:
                effort_args = ["--reasoning-effort", effort] if effort else []
                args = ["grok", "-m", slug, *effort_args, *options, "--no-subagents", "-p", prompt]

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
            if reported != [slug] or re.search(r"^model rerouted:", process.stderr, re.MULTILINE):
                raise QuotaExhausted(model)
            body = process.stdout
        else:
            envelope = json_object(process.stdout)
            if envelope.get("is_error") or envelope.get("type") == "error":
                if QUOTA.search(process.stdout):
                    raise QuotaExhausted(model)
                raise InvalidOutput(f"{provider} worker reported an error")
            usage = envelope.get("modelUsage")
            if not _usage_matches(slug, usage):
                raise QuotaExhausted(model)
            body = envelope.get("result" if provider == "claude" else "text")

        result = json_object(body)
        if not set(required) <= result.keys() <= set(required + optional):
            raise InvalidOutput(f"{node} response keys do not match the stage")
        return result
