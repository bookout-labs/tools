"""Worker output contracts shared by the CLI prompts and delivery graph."""

import re
from typing import Annotated, Literal, TypeVar, get_args

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints, TypeAdapter, ValidationError, ValidationInfo, field_validator, model_validator

from foreman.dispatch import InvalidOutput

Text = Annotated[str, StringConstraints(pattern=r"\S")]
TrimmedText = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"\S")]
Sections = TypeAdapter(dict[TrimmedText, TrimmedText])
Output = TypeVar("Output", bound=BaseModel)


class Contract(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def legacy_nullable_fields(cls, value: object, info: ValidationInfo) -> object:
        if info.context == "legacy" and isinstance(value, dict):
            omitted = {name: None for name, field in cls.model_fields.items()
                       if name not in value and type(None) in get_args(field.annotation)}
            return {**omitted, **value}
        return value


class SliceProperty(Contract):
    name: TrimmedText
    constraint: TrimmedText


class Slice(Contract):
    """Preserve the proof obligations."""

    title: TrimmedText
    acceptance: TrimmedText
    depends_on: list[TrimmedText] = Field(
        description="Titles of other slices in this spec that must land first; empty for a single slice.",
    )
    screen: str = Field(
        description="Use docs/screens/<file>.md without '..', or an empty string.",
        pattern=r"^(docs/screens/(?:[^./\\\x00-\x1f]|\.[^./\\\x00-\x1f])+\.md)?$",
    )
    properties: list[SliceProperty]



class SlicePlanningOutput(Contract):
    slice: Slice | None
    human_question: str | None


class TriageOutput(SlicePlanningOutput):
    triage: Literal["implement", "spec", "question", "needs_human"]


class ResearchOutput(Contract):
    research: Text


class SpecOutput(SlicePlanningOutput):
    spec: Text
    spec_gate: Literal["ready", "needs_human"]
    slices: list[Slice] | None


class BrowserProof(Contract):
    """For a required screen, report only browser evidence you actually collected."""

    screen: str | None
    trace: str | None
    passed: bool | None


class ConstraintResult(Contract):
    constraint: Text
    rejected: bool


class FormalProof(Contract):
    """For required properties, report only formal evidence you actually collected."""

    ran: bool | None
    constraints: list[ConstraintResult] | None
    typecheck: str | bool | None


class ImplementOutput(Contract):
    implementation: Text
    head_sha: Annotated[str | None, AfterValidator(lambda sha: (sha or "").strip() or None)]
    browser_proof: BrowserProof | None
    formal_proof: FormalProof | None

    @field_validator("implementation", mode="before")
    @classmethod
    def join_sections(cls, value: object, info: ValidationInfo) -> object:
        if info.context == "legacy" and isinstance(value, dict):
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
    review_note: str | None = Field(
        description="Put the reviewer's requested changes here and keep the verdict in review. Use null when there is no note.",
    )

    @model_validator(mode="before")
    @classmethod
    def split_verdict(cls, value: object, info: ValidationInfo) -> object:
        if info.context == "legacy" and isinstance(value, dict) and isinstance(verdict := value.get("review"), str):
            verdict = verdict.strip()
            if verdict in {"approve", "approve."}:
                return {**value, "review": "approve"}
            match = re.fullmatch(r"changes(?:[.,:;]\s*(.*))?", verdict, re.S)
            if match:
                note = value.get("review_note")
                if match[1] and note is not None:
                    raise ValueError("inline change note conflicts with review_note")
                return {**value, "review": "changes", "review_note": match[1] or note}
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


def parse_output(contract: type[Output], raw: object, *, native: bool = False) -> Output:
    try:
        return contract.model_validate(raw, context=None if native else "legacy")
    except ValidationError as exc:
        error = exc.errors(include_input=False)[0]
        path = ".".join(map(str, error["loc"])) or "$"
        raise InvalidOutput(f"{path}: {error['msg']}") from exc
