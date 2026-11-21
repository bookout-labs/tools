"""CLI subprocess contracts."""

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from subprocess import CompletedProcess
from typing import ClassVar, Literal, Protocol

from pydantic import BaseModel, ConfigDict, ValidationError

from foreman.config import ModelSpec


class Usage(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True)
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    total_cost_usd: float | None = None


OutcomeKind = Literal["ran", "quota", "cli_missing", "failed"]


@dataclass(frozen=True)
class Outcome:
    kind: OutcomeKind
    output: dict[str, object] = field(default_factory=dict[str, object])
    usage: Usage | None = None
    session_id: str | None = None
    detail: str = ""


@dataclass(frozen=True)
class Call:
    prompt: str
    schema: str
    edit: bool
    temp_dir: Path


class CliAdapter(Protocol):
    env: ClassVar[Mapping[str, str]]

    def command(self, call: Call) -> list[str]: ...
    def read(self, process: CompletedProcess[str]) -> Outcome: ...


class NativeEnvelope(BaseModel, ABC):
    model_config = ConfigDict(strict=True)

    @abstractmethod
    def outcome(self, process: CompletedProcess[str], model_id: str) -> Outcome: ...


@dataclass(frozen=True)
class NativeAdapter:
    model: ModelSpec
    envelope: ClassVar[type[NativeEnvelope]]
    env: ClassVar[Mapping[str, str]] = {}

    @staticmethod
    def permission_mode(call: Call) -> str:
        return "acceptEdits" if call.edit else "plan"

    def read(self, process: CompletedProcess[str]) -> Outcome:
        try:
            envelope = self.envelope.model_validate_json(process.stdout)
        except ValidationError as exc:
            detail = process.stderr.strip() or process.stdout.strip() or exc.errors(include_input=False)[0]["msg"]
            return Outcome("failed", detail=detail)
        return envelope.outcome(process, self.model.model_id)


def build_outcome(
    kind: OutcomeKind, *, output: dict[str, object] | None, usage: Usage | None,
    total_cost_usd: float | None, session_id: str | None, detail: str,
) -> Outcome:
    if usage is not None:
        usage = usage.model_copy(update={"total_cost_usd": total_cost_usd})
        if not any(usage.model_dump().values()):
            usage = None
    if kind == "ran" and output is not None:
        return Outcome(kind, output, usage, session_id)
    return Outcome(kind, usage=usage, session_id=session_id, detail=detail)
