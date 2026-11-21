"""Worker output contracts shared by the CLI prompts and delivery graph."""

from typing import Annotated, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from pydantic.config import JsonDict

from foreman.dispatch import InvalidOutput

PULL_REQUEST_LINES = 400

Text = Annotated[str, StringConstraints(pattern=r"\S")]
TrimmedText = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"\S")]
Output = TypeVar("Output", bound=BaseModel)


class Contract(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class SliceProperty(Contract):
    name: TrimmedText = Field(description="State the database rule in plain words.")
    constraint: TrimmedText = Field(description="Give the exact PostgreSQL constraint name, such as deals_external_id_key.")


def _describe_slice(schema: JsonDict) -> None:
    schema["description"] = f"One pull request's outcome, about {PULL_REQUEST_LINES} changed lines or fewer, and its proof obligations."


class Slice(Contract):
    model_config = ConfigDict(json_schema_extra=_describe_slice)

    title: TrimmedText = Field(description="Name the outcome.")
    acceptance: TrimmedText = Field(description="State how to check the outcome, including response and other behavior checks.")
    depends_on: list[TrimmedText] = Field(
        description="List the titles of other slices from the same parent that must land first. Use an empty list if none must land first.",
    )
    screen: str = Field(
        description=(
            "When the change alters a screen described under docs/screens/, name that Markdown file directly under "
            "docs/screens/ without '..'. Use an empty string when no documented screen changes."
        ),
        pattern=r"^(docs/screens/(?:[^./\\\x00-\x1f]|\.[^./\\\x00-\x1f])+\.md)?$",
    )
    properties: list[SliceProperty] = Field(
        description="List only named PostgreSQL CHECK, UNIQUE, FOREIGN KEY, EXCLUSION, or NOT NULL constraints. Use an empty list when the slice adds no database rule.",
    )


class HumanQuestionOutput(Contract):
    human_question: str | None = Field(
        description="When this stage returns needs_human, put the question for the person here. Use null otherwise.",
    )


class TriageOutput(HumanQuestionOutput):
    triage: Literal["implement", "spec", "question", "needs_human"] = Field(
        description=(
            "Use implement when the behavior is decided and the change fits in one pull request "
            f"of about {PULL_REQUEST_LINES} changed lines or fewer. For other changes, use spec."
        ),
    )
    slice: Slice | None = Field(
        description="For a parent issue that triages as implement, return its single slice. Use null otherwise, including for slice issues.",
    )


class ResearchOutput(Contract):
    research: Text


class SpecOutput(HumanQuestionOutput):
    spec: Text
    spec_gate: Literal["ready", "needs_human"]
    slices: list[Slice] | None = Field(
        description="For a ready parent spec, return one entry per pull request. Use null otherwise.",
    )


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
