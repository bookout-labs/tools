"""CLI subprocess contracts."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from subprocess import CompletedProcess
from typing import TYPE_CHECKING, ClassVar, Literal, Protocol

from pydantic import BaseModel, ConfigDict, ValidationError

from foreman.config import ModelSpec

if TYPE_CHECKING:
    from foreman.stage_output import Contract


class Usage(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True)
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    total_cost_usd: float | None = None


@dataclass(frozen=True)
class Outcome:
    kind: Literal["ran", "quota", "cli_missing", "failed"]
    output: dict[str, object] = field(default_factory=dict[str, object])
    usage: Usage | None = None
    session_id: str | None = None
    detail: str = ""


@dataclass(frozen=True)
class Call:
    prompt: str
    schema: str
    edit: bool

    @property
    def permission_mode(self) -> str:
        return "acceptEdits" if self.edit else "plan"


class CliAdapter(Protocol):
    def command(self, call: Call) -> list[str]: ...
    def read(self, process: CompletedProcess[str]) -> Outcome: ...


@dataclass(frozen=True)
class ModelAdapter:
    model: ModelSpec
    contract: "type[Contract]"


class NativeEnvelope(BaseModel, ABC):
    model_config = ConfigDict(strict=True)

    @abstractmethod
    def outcome(self, process: CompletedProcess[str], model_id: str) -> Outcome: ...


class NativeAdapter(ModelAdapter):
    envelope: ClassVar[type[NativeEnvelope]]

    def read(self, process: CompletedProcess[str]) -> Outcome:
        try:
            envelope = self.envelope.model_validate_json(process.stdout)
        except ValidationError as exc:
            detail = process.stderr.strip() or process.stdout.strip() or exc.errors(include_input=False)[0]["msg"]
            return Outcome("failed", detail=detail)
        return envelope.outcome(process, self.model.model_id)


def build_outcome(
    model_id: str, *, output: dict[str, object] | None, model_usage: dict[str, object] | None,
    usage: Usage | None, total_cost_usd: float | None, session_id: str | None, detail: str,
    failed: bool, quota: bool,
) -> Outcome:
    if usage is not None:
        usage = usage.model_copy(update={"total_cost_usd": total_cost_usd})
        if not any(usage.model_dump().values()):
            usage = None
    if quota:
        return Outcome("quota", usage=usage, session_id=session_id, detail=detail)
    if failed:
        return Outcome("failed", usage=usage, session_id=session_id, detail=detail)
    if model_usage and model_usage.keys() != {model_id}:
        detail = f"requested {model_id}, modelUsage reported {', '.join(model_usage)}"
        return Outcome("failed", usage=usage, session_id=session_id, detail=detail)
    if output is None:
        return Outcome("failed", usage=usage, session_id=session_id, detail=detail or "missing structured output")
    return Outcome("ran", output, usage, session_id)
