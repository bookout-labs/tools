"""CLI subprocess contracts."""

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from subprocess import CompletedProcess
from typing import ClassVar, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, NonNegativeInt, TypeAdapter, ValidationError

from foreman.config import ModelSpec


class Usage(BaseModel):
    """Token counts with uncached input separate from cache reads and writes."""

    model_config = ConfigDict(strict=True, frozen=True)
    input_tokens: NonNegativeInt
    output_tokens: NonNegativeInt
    cache_read_input_tokens: NonNegativeInt | None = None
    cache_creation_input_tokens: NonNegativeInt | None = None
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


class ModelUsage(BaseModel):
    model_config = ConfigDict(strict=True)
    input: int = Field(alias="inputTokens", ge=0)
    output: int = Field(alias="outputTokens", ge=0)
    cache_read: int = Field(default=0, alias="cacheReadInputTokens", ge=0)
    cache_creation: int = Field(default=0, alias="cacheCreationInputTokens", ge=0)
    cost: float | None = Field(default=None, alias="costUSD")


class NativeEnvelope(BaseModel, ABC):
    model_config = ConfigDict(strict=True)

    model_usage: dict[str, object] | None = Field(default=None, alias="modelUsage")

    def read_usage(self) -> Usage | None:
        try:
            rows = list(TypeAdapter(dict[str, ModelUsage]).validate_python(self.model_usage or {}).values())
        except ValidationError:
            return None
        if not rows:
            return None
        costs = [row.cost for row in rows if row.cost is not None]
        return Usage(
            input_tokens=sum(row.input for row in rows), output_tokens=sum(row.output for row in rows),
            cache_read_input_tokens=sum(row.cache_read for row in rows),
            cache_creation_input_tokens=sum(row.cache_creation for row in rows),
            total_cost_usd=sum(costs) if len(costs) == len(rows) else None,
        )

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
    session_id: str | None, detail: str,
) -> Outcome:
    if usage is not None:
        if not any((usage.input_tokens, usage.output_tokens,
                    usage.cache_read_input_tokens, usage.cache_creation_input_tokens)):
            usage = None
    if kind == "ran" and output is not None:
        return Outcome(kind, output, usage, session_id)
    return Outcome(kind, usage=usage, session_id=session_id, detail=detail)
