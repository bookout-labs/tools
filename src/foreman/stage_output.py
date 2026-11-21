"""Worker output contracts shared by the CLI prompts and delivery graph."""

from typing import Annotated, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from foreman.dispatch import InvalidOutput

Text = Annotated[str, StringConstraints(pattern=r"\S")]
TrimmedText = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"\S")]
Output = TypeVar("Output", bound=BaseModel)


class Contract(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


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
    head_sha: Annotated[str, StringConstraints(pattern=r"^[0-9a-fA-F]{40}$")] | None
    browser_proof: BrowserProof | None
    formal_proof: FormalProof | None


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
        path = ".".join(map(str, error["loc"])) or "$"
        raise InvalidOutput(f"{path}: {error['msg']}") from exc
