"""CLI subprocess contracts."""

from dataclasses import dataclass, field
from pathlib import Path
import re
from subprocess import CompletedProcess
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

QUOTA = re.compile(
    r"^(?:error: )?(?:api error:? (?:\(status )?429\b|http 429\b|"
    r"rate[ _-]limit(?:ed|_error| exceeded| reached)\b|too many requests\b|"
    r"(?:insufficient_quota|quota exceeded|resource_exhausted)\b|"
    r"you['’]ve (?:hit (?:your |the |your team['’]s API )?(?:rate )?limit|"
    r"reached your free Grok Build usage limit)\b)",
    re.IGNORECASE | re.MULTILINE,
)


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


class CliAdapter(Protocol):
    def command(self, call: Call) -> list[str]: ...
    def read(self, process: CompletedProcess[str], out_path: Path | None = None) -> Outcome: ...


class Envelope(BaseModel):
    model_config = ConfigDict(strict=True)
    output: dict[str, object] | None = None
    session_id: str | None = None
    model_usage: dict[str, object] | None = Field(default=None, alias="modelUsage")
    usage: Usage | None = None
    total_cost_usd: float | None = None
    is_error: bool = False
    type: str | None = None
    errors: list[str] = Field(default_factory=list)
    message: str = ""


def read_envelope(process: CompletedProcess[str], contract: type[Envelope], model: str) -> Outcome:
    try:
        envelope = contract.model_validate_json(process.stdout)
    except ValidationError as exc:
        detail = process.stderr.strip() or process.stdout.strip() or exc.errors(include_input=False)[0]["msg"]
        return Outcome("quota" if process.returncode and QUOTA.match(detail) else "failed", detail=detail)

    usage = envelope.usage
    if usage is not None and envelope.total_cost_usd is not None:
        usage = usage.model_copy(update={"total_cost_usd": envelope.total_cost_usd})
    session_id = envelope.session_id
    if envelope.model_usage and envelope.model_usage.keys() != {model}:
        detail = f"requested {model}, modelUsage reported {', '.join(envelope.model_usage)}"
        return Outcome("failed", usage=usage, session_id=session_id, detail=detail)
    if process.returncode or envelope.is_error or envelope.type == "error":
        detail = "\n".join(envelope.errors) or envelope.message or process.stderr.strip()
        kind = "quota" if QUOTA.match(detail) else "failed"
        return Outcome(kind, usage=usage, session_id=session_id, detail=detail or f"CLI exited {process.returncode}")
    if envelope.output is None:
        return Outcome("failed", usage=usage, session_id=session_id, detail=envelope.message or "missing structured output")
    return Outcome("ran", envelope.output, usage, session_id)
