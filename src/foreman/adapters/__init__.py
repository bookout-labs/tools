"""CLI subprocess contracts."""

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from decimal import Decimal
from math import isfinite
from pathlib import Path
from subprocess import CompletedProcess
from typing import Annotated, ClassVar, Literal, Protocol

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


@with_config(ConfigDict(extra="forbid"))
class Session(TypedDict):
    """A conversation a later call may resume, with the totals its CLI last reported for it, as reported (#446).
    It never exists without them: a call whose answer reported none leaves no session (K2)."""

    id: Annotated[str, Field(pattern=r"^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$")]
    totals: NativeUsage


OutcomeKind = Literal["ran", "quota", "cli_missing", "failed", "budget_exhausted"]


@dataclass(frozen=True)
class Outcome:
    kind: OutcomeKind
    output: dict[str, object] = field(default_factory=dict[str, object])
    usage: Usage | None = None
    session_id: str | None = None
    detail: str = ""
    prompt_bytes: int | None = None
    # The CLI's report as it came back, and the totals a session saves of it, which keep only figures the CLI corroborates.
    native_usage: NativeUsage | None = None
    totals: NativeUsage | None = None

    @property
    def session(self) -> Session | None:
        return Session(id=self.session_id, totals=self.totals) if self.session_id and self.totals else None


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
    resume: Session | None = None
    # The checkout's .git entry and the directories git resolves it to. Foreman commits, so a worker never writes them.
    git_dirs: tuple[Path, ...] = ()

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


def less(row: NativeUsageTokens, start: NativeUsageTokens) -> NativeUsageTokens | None:
    """One model's spend since `start`, in exact dollars. A figure either side lacks is unknown; a figure that fell ends it."""
    def minus(now: int | None, then: int | None) -> int | None:
        return None if now is None or then is None else now - then
    cost, paid = row["total_cost_usd"], start["total_cost_usd"]
    spent = None if cost is None or paid is None else Decimal(repr(cost)) - Decimal(repr(paid))
    own = NativeUsageTokens(
        input_tokens=row["input_tokens"] - start["input_tokens"], output_tokens=row["output_tokens"] - start["output_tokens"],
        cache_read_input_tokens=minus(row["cache_read_input_tokens"], start["cache_read_input_tokens"]),
        cache_creation_input_tokens=minus(row["cache_creation_input_tokens"], start["cache_creation_input_tokens"]),
        total_cost_usd=None if spent is None else float(spent),
    )
    figures = (own["input_tokens"], own["output_tokens"], own["cache_read_input_tokens"], own["cache_creation_input_tokens"], spent)
    return None if any(figure is not None and figure < 0 for figure in figures) else own


class NativeEnvelope(BaseModel, ABC):
    model_config = ConfigDict(strict=True)

    model_usage: dict[str, object] | None = Field(default=None, alias="modelUsage")

    def read_report(self) -> dict[str, NativeUsageTokens] | None:
        """The CLI's rows by model, as it reported them."""
        try:
            rows = TypeAdapter(dict[str, ModelUsage]).validate_python(self.model_usage or {})
        except ValidationError:
            return None
        return {
            model: NativeUsageTokens(
                input_tokens=row.input, output_tokens=row.output,
                cache_read_input_tokens=row.cache_read if "cache_read" in row.model_fields_set else None,
                cache_creation_input_tokens=row.cache_creation if "cache_creation" in row.model_fields_set else None,
                total_cost_usd=row.cost,
            )
            for model, row in rows.items()
        } or None

    def read_totals(self) -> dict[str, NativeUsageTokens] | None:
        """The rows a session saves and a charge reads: the report's, unless the CLI says which figures to doubt."""
        return self.read_report()

    def read_usage(self, since: Mapping[str, NativeUsageTokens] | None = None) -> Usage | None:
        """The usage the rows report less the rows `since`, model by model (#446 K1). A row `since` has that the rows lack is
        unknown, and so is a row with a figure that fell; an unknown row makes the whole usage unknown."""
        rows, since = self.read_totals(), since or {}
        if rows is None or any(model not in rows for model in since):
            return None
        own = [row if (start := since.get(model)) is None else less(row, start) for model, row in rows.items()]
        known = [row for row in own if row is not None]
        if len(known) < len(own):
            return None
        costs = [cost for row in known if (cost := row["total_cost_usd"]) is not None]
        reads = [value for row in known if (value := row["cache_read_input_tokens"]) is not None]
        writes = [value for row in known if (value := row["cache_creation_input_tokens"]) is not None]
        cost = sum(costs) if len(costs) == len(known) else None
        return Usage(
            input_tokens=sum(row["input_tokens"] for row in known), output_tokens=sum(row["output_tokens"] for row in known),
            cache_read_input_tokens=sum(reads) if len(reads) == len(known) else None,
            cache_creation_input_tokens=sum(writes) if len(writes) == len(known) else None,
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
        # The attempt keeps the CLI's own report whatever it is charged (#446 condition 4); the session saves its totals.
        if (rows := envelope.read_report()) is None or (totals := envelope.read_totals()) is None:
            return outcome
        family = self.model.family
        return replace(outcome, native_usage=NativeUsage(family=family, aggregation="cli_model_totals", entries=rows),
                       totals=NativeUsage(family=family, aggregation="cli_model_totals", entries=totals))


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
