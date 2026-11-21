"""Worker output contracts shared by the CLI prompts and delivery graph."""

import re
from typing import Annotated, Literal, TypeVar

from pydantic import AfterValidator, BaseModel, ConfigDict, StringConstraints, TypeAdapter, ValidationError, field_validator, model_validator

from foreman.dispatch import InvalidOutput

Text = Annotated[str, StringConstraints(pattern=r"\S")]
TrimmedText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Sections = TypeAdapter(dict[TrimmedText, TrimmedText])
Output = TypeVar("Output", bound=BaseModel)


def _require_all_fields(schema: dict) -> None:
    properties = schema["properties"]
    schema["required"] = list(properties)
    for field in properties.values():
        field.pop("default", None)


class Contract(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", json_schema_extra=_require_all_fields)


class SliceProperty(Contract):
    name: TrimmedText
    constraint: TrimmedText


class Slice(Contract):
    title: TrimmedText
    acceptance: TrimmedText
    depends_on: list[TrimmedText]
    screen: str
    properties: list[SliceProperty]

    @field_validator("screen")
    @classmethod
    def screen_path(cls, value: str) -> str:
        if value and (".." in value or not re.fullmatch(r"docs/screens/[^/\\\x00-\x1f]+\.md", value)):
            raise ValueError("must be empty or a docs/screens/<file>.md path without '..'")
        return value


class SliceDecision(Contract):
    slice: Slice | None = None
    human_question: str | None = None


class TriageOutput(SliceDecision):
    triage: Literal["implement", "spec", "question", "needs_human"]


class ResearchOutput(Contract):
    research: Text


class SpecOutput(SliceDecision):
    spec: Text
    spec_gate: Literal["ready", "needs_human"]
    slices: list[Slice] | None = None


class BrowserProof(Contract):
    screen: str | None = None
    trace: str | None = None
    passed: bool | None = None


class ConstraintResult(Contract):
    constraint: Text
    rejected: bool


class FormalProof(Contract):
    ran: bool | None = None
    constraints: list[ConstraintResult] | None = None
    typecheck: str | bool | None = None


class ImplementOutput(Contract):
    implementation: Text
    head_sha: Annotated[str | None, AfterValidator(lambda sha: (sha or "").strip() or None)] = None
    browser_proof: BrowserProof | None = None
    formal_proof: FormalProof | None = None

    @field_validator("implementation", mode="before")
    @classmethod
    def join_sections(cls, value: object) -> object:
        if isinstance(value, dict):
            sections = Sections.validate_python(value, strict=True)
            return "\n\n".join(f"{title}\n{body}" for title, body in sections.items())
        return value


class MergeRisk(Contract):
    alignment: TrimmedText
    impact: TrimmedText
    verification: TrimmedText
    rollback: TrimmedText
    error_detection: TrimmedText
    level: Literal["low", "medium", "high"]


class ReviewOutput(Contract):
    review: Literal["approve", "changes"]
    risk: MergeRisk
    review_note: str | None = None

    @model_validator(mode="before")
    @classmethod
    def split_verdict(cls, value: object) -> object:
        if isinstance(value, dict) and isinstance(verdict := value.get("review"), str):
            match = re.fullmatch(r"(approve|changes)(?:[.,:;]\s*(.*))?", verdict.strip(), re.I | re.S)
            if match:
                note = value.get("review_note")
                return {**value, "review": match[1].lower(), "review_note": match[2] if note is None else note}
        return value


class VerifyOutput(Contract):
    verify: Literal["pass", "fail"]


STAGE_OUTPUTS: dict[str, type[Contract]] = {
    "triage": TriageOutput,
    "research": ResearchOutput,
    "spec": SpecOutput,
    "implement": ImplementOutput,
    "review": ReviewOutput,
    "verify": VerifyOutput,
}


def parse_output(contract: type[Output], raw: object) -> Output:
    try:
        return contract.model_validate(raw)
    except ValidationError as exc:
        error = exc.errors(include_input=False)[0]
        location = [str(part) for part in error["loc"]]
        path = ".".join(location) or "$"
        label = f" (merge {' '.join(location)})" if location and location[0] == "risk" else ""
        raise InvalidOutput(f"{path}{label}: {error['msg']}") from exc
