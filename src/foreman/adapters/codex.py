"""Legacy Codex command and result parsing, to be replaced in S3."""

from collections.abc import Mapping
from dataclasses import dataclass
import json
import re
from subprocess import CompletedProcess
from typing import ClassVar, cast, get_args

from pydantic import TypeAdapter, ValidationError

from foreman.adapters import Call, Outcome
from foreman.config import ModelSpec
from foreman.stage_output import Contract, TrimmedText

SECTIONS = TypeAdapter(dict[TrimmedText, TrimmedText])
QUOTA = re.compile(
    r"quota|rate.limit|usage limit|hit your limit|resource_exhausted|too many requests|\b429\b",
    re.IGNORECASE,
)


def fill_nullable_fields(contract: type[Contract], output: dict[str, object]) -> None:
    for name, field in contract.model_fields.items():
        annotation: object = field.annotation
        alternatives: tuple[object, ...] = get_args(annotation)
        if type(None) in alternatives:
            output.setdefault(name, None)
            annotation = next(arg for arg in alternatives if arg is not type(None))
        value = output.get(name)
        if isinstance(annotation, type) and issubclass(annotation, Contract) and isinstance(value, dict):
            fill_nullable_fields(annotation, cast(dict[str, object], value))


def normalize_output(contract: type[Contract], output: dict[str, object]) -> None:
    fill_nullable_fields(contract, output)
    if isinstance(sections := output.get("implementation"), dict):
        sections = SECTIONS.validate_python(sections, strict=True)
        output["implementation"] = "\n\n".join(f"{title}\n{body}" for title, body in sections.items())


@dataclass(frozen=True)
class CodexAdapter:
    model: ModelSpec
    contract: type[Contract]
    env: ClassVar[Mapping[str, str]] = {}

    def command(self, call: Call) -> list[str]:
        sandbox = "workspace-write" if call.edit else "read-only"
        effort = ["-c", f"model_reasoning_effort={self.model.effort}"] if self.model.effort else []
        return ["codex", "exec", "-m", self.model.model_id, *effort, "--color", "never", "-s", sandbox, call.prompt]

    def read(self, process: CompletedProcess[str]) -> Outcome:
        if process.returncode:
            if match := QUOTA.search(f"{process.stderr}\n{process.stdout}"):
                return Outcome("quota", detail=match[0])
            return Outcome("failed", detail=f"Codex exited {process.returncode}")

        reported = re.findall(r"^model:[ \t]*(\S+)[ \t]*$", process.stderr, re.MULTILINE)
        # Keep treating legacy Codex reroutes as quota until S3 replaces this path.
        if reported != [self.model.model_id] or re.search(r"^model rerouted:", process.stderr, re.MULTILINE):
            return Outcome("quota", detail="Codex did not confirm the requested model")
        try:
            value: object = json.loads(process.stdout)
        except ValueError:
            return Outcome("failed", detail="worker did not return a JSON object")
        if not isinstance(value, dict):
            return Outcome("failed", detail="worker did not return a JSON object")
        output = cast(dict[str, object], value)
        try:
            normalize_output(self.contract, output)
        except ValidationError as exc:
            return Outcome("failed", detail=f"implementation: {exc.errors(include_input=False)[0]['msg']}")
        return Outcome("ran", output)
