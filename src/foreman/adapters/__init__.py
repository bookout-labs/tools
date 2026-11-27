"""CLI subprocess contracts."""

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from math import isfinite
from pathlib import Path
from subprocess import CompletedProcess
from typing import ClassVar, Literal, Protocol

from typing_extensions import TypedDict

from pydantic import BaseModel, ConfigDict, Field, NonNegativeInt, TypeAdapter, ValidationError, field_validator, with_config

from foreman.config import ModelSpec
from foreman.usd import BudgetUsd, CostUsd


class Usage(BaseModel):
    """Token counts with uncached input separate from cache reads and writes."""

    model_config = ConfigDict(strict=True, frozen=True)
    input_tokens: NonNegativeInt
    output_tokens: NonNegativeInt
    cache_read_input_tokens: NonNegativeInt | None = None
    cache_creation_input_tokens: NonNegativeInt | None = None
    total_cost_usd: CostUsd | None = None


@with_config(ConfigDict(extra="forbid"))
class NativeUsageTokens(TypedDict):
    input_tokens: NonNegativeInt
    output_tokens: NonNegativeInt
    cache_read_input_tokens: NonNegativeInt | None
    cache_creation_input_tokens: NonNegativeInt | None
    total_cost_usd: CostUsd | None


@with_config(ConfigDict(extra="forbid"))
class NativeUsage(TypedDict):
    """Recorded CLI totals only; never an additional budget ledger."""

    family: str
    aggregation: Literal["cli_model_totals", "turn_total"]
    entries: dict[str, NativeUsageTokens]


OutcomeKind = Literal["ran", "quota", "cli_missing", "failed", "budget_exhausted"]


@dataclass(frozen=True)
class Outcome:
    kind: OutcomeKind
    output: dict[str, object] = field(default_factory=dict[str, object])
    usage: Usage | None = None
    session_id: str | None = None
    detail: str = ""
    prompt_bytes: int | None = None
    native_usage: NativeUsage | None = None


@dataclass(frozen=True)
class Call:
    prompt: str
    schema: str
    edit: bool
    temp_dir: Path
    budget_usd: BudgetUsd | None = None
    checks: tuple[str, ...] = ()
    read_roots: tuple[Path, ...] = ()
    # The verifier builds and drives the app: sandboxed Bash with no network, no Edit or Write.
    drive: bool = False
    resume_session: str | None = None
    # The checkout's .git entry and the directories git resolves it to. Foreman commits, so a worker never writes them.
    git_dirs: tuple[Path, ...] = ()
    # What git reads, read-only, for the checkout and each read root: see foreman.worker.git_reads.
    git_reads: tuple[Path, ...] = ()
    # Claude Code keeps a sandboxed shell's per-user temp directory under CLAUDE_CODE_TMPDIR only while the path fits its
    # AF_UNIX sockets; otherwise it falls back to the shared /tmp/claude-<uid>. So each Claude shell gets its own short one.
    shell_temp: Path | None = None

    @property
    def shell(self) -> bool:
        """Whether the worker has a shell: an editing or driving call, or one granted a check."""
        return self.edit or self.drive or bool(self.checks)

    @property
    def scratch(self) -> Path:
        """The attempt scratch an editing or driving worker writes besides its checkout."""
        return self.temp_dir / "scratch"


class CapabilityUnavailable(ValueError):
    """The native CLI cannot enforce the requested worker boundary."""


class CliAdapter(Protocol):
    env: ClassVar[Mapping[str, str]]

    def command(self, call: Call) -> list[str]: ...
    def read(self, process: CompletedProcess[str], call: Call) -> Outcome: ...


class ModelUsage(BaseModel):
    model_config = ConfigDict(strict=True)
    input: int = Field(alias="inputTokens", ge=0)
    output: int = Field(alias="outputTokens", ge=0)
    cache_read: int = Field(default=0, alias="cacheReadInputTokens", ge=0)
    cache_creation: int = Field(default=0, alias="cacheCreationInputTokens", ge=0)
    cost: CostUsd | None = Field(default=None, alias="costUSD")

    @field_validator("cost", mode="before")
    @classmethod
    def valid_cost(cls, value: object) -> float | None:
        try:
            return TypeAdapter(CostUsd).validate_python(value, strict=True)
        except ValidationError:
            return None


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
        cost = sum(costs) if len(costs) == len(rows) else None
        reads = [row.cache_read for row in rows if "cache_read" in row.model_fields_set]
        writes = [row.cache_creation for row in rows if "cache_creation" in row.model_fields_set]
        return Usage(
            input_tokens=sum(row.input for row in rows), output_tokens=sum(row.output for row in rows),
            cache_read_input_tokens=sum(reads) if len(reads) == len(rows) else None,
            cache_creation_input_tokens=sum(writes) if len(writes) == len(rows) else None,
            total_cost_usd=cost if cost is not None and isfinite(cost) else None,
        )

    @abstractmethod
    def outcome(self, process: CompletedProcess[str], model_id: str, call: Call) -> Outcome: ...


@dataclass(frozen=True)
class NativeAdapter:
    model: ModelSpec
    envelope: ClassVar[type[NativeEnvelope]]
    env: ClassVar[Mapping[str, str]] = {}

    def read(self, process: CompletedProcess[str], call: Call) -> Outcome:
        return self.read_envelope(process.stdout, process, call)

    def read_envelope(self, text: str, process: CompletedProcess[str], call: Call) -> Outcome:
        try:
            envelope = self.envelope.model_validate_json(text)
        except ValidationError as exc:
            detail = process.stderr.strip() or text.strip() or exc.errors(include_input=False)[0]["msg"]
            return Outcome("failed", detail=detail)
        outcome = envelope.outcome(process, self.model.model_id, call)
        if outcome.usage is None:
            return outcome
        try:
            rows = TypeAdapter(dict[str, ModelUsage]).validate_python(envelope.model_usage)
        except ValidationError:
            return outcome
        entries: dict[str, NativeUsageTokens] = {
            model: NativeUsageTokens(
                input_tokens=row.input, output_tokens=row.output,
                cache_read_input_tokens=row.cache_read if "cache_read" in row.model_fields_set else None,
                cache_creation_input_tokens=row.cache_creation if "cache_creation" in row.model_fields_set else None,
                total_cost_usd=row.cost,
            )
            for model, row in rows.items()
        }
        return replace(outcome, native_usage=NativeUsage(
            family=self.model.family, aggregation="cli_model_totals", entries=entries,
        ))


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
