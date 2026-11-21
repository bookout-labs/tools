"""Worker output contracts shared by the CLI prompts and delivery graph."""

from typing import Annotated, Literal, Self, TypeVar

from pydantic import BaseModel, ConfigDict, Field, PositiveInt, StringConstraints, ValidationError, field_validator, model_validator
from pydantic.config import JsonDict

from foreman.dispatch import InvalidOutput

PULL_REQUEST_LINES = 400

Text = Annotated[str, StringConstraints(pattern=r"\S")]
TrimmedText = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"\S")]
Output = TypeVar("Output", bound=BaseModel)
WorkKind = Literal["feature", "bug_fix", "refactoring", "perf_issue"]


class Contract(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class SliceProperty(Contract):
    name: TrimmedText = Field(description="State the database rule in plain words.")
    constraint: TrimmedText = Field(description="Give the exact PostgreSQL constraint name, such as deals_external_id_key.")


class Seam(Contract):
    name: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$", description="Use the same short identifier in every slice that builds or uses this seam.")
    exists: bool = Field(description="Set true for a seam that already exists in the checkout. Otherwise use false.")
    path: TrimmedText | None = Field(description="For an existing seam, give a workspace-relative file path. Otherwise use null.")

    @model_validator(mode="after")
    def existing_file(self) -> Self:
        if self.exists != (self.path is not None):
            raise ValueError("path is required exactly when exists is true")
        return self


def _describe_slice(schema: JsonDict) -> None:
    schema["description"] = f"One pull request's outcome, about {PULL_REQUEST_LINES} changed lines or fewer, and its proof obligations."


class Slice(Contract):
    model_config = ConfigDict(json_schema_extra=_describe_slice)

    title: TrimmedText = Field(description="Name the outcome.")
    stateful: bool = Field(description="Set true when the slice changes behavior across states or transitions.")
    acceptance: TrimmedText = Field(description="State how to check the outcome, including response and other behavior checks.")
    builds: list[Seam] = Field(description="List interfaces, modules, and contracts this slice builds for other slices. Use an empty list when there are none.")
    uses: list[Seam] = Field(description="List seams this slice needs. Use an empty list when there are none.")
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

    @field_validator("builds")
    @classmethod
    def new_seams(cls, value: list[Seam]) -> list[Seam]:
        if any(seam.exists for seam in value):
            raise ValueError("only uses can name existing seams")
        return value


class StoredSlice(Slice):
    stateful: bool = False
    work_kind: WorkKind | None = Field(default=None, exclude=True)
    builds: list[Seam] = Field(default_factory=list)
    uses: list[Seam] = Field(default_factory=list)
    depends_on: list[TrimmedText] = Field(default_factory=list)
    blocker_numbers: list[PositiveInt] | None = None


class HumanQuestionOutput(Contract):
    human_question: str | None = Field(
        description="When this stage returns needs_human, put the question for the person here. Use null otherwise.",
    )


class TriageOutput(HumanQuestionOutput):
    work_kind: WorkKind = Field(
        description="Classify the work. Keep the inherited work_kind when the delivery state already has one for a slice.",
    )
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


class Finding(Contract):
    file: Text = Field(description="Give the path relative to the repository root.")
    line: int = Field(description="Give the line number in the reviewed commit.")
    severity: Literal["blocking", "nit"]
    summary: Text = Field(max_length=200)


class ReviewOutput(Contract):
    review: Literal["approve", "changes"]
    risk: MergeRisk
    findings: list[Finding] = Field(
        description="List each requested fix, including nits. Return one or more findings for changes and an empty list for approve.",
    )
    detail: Text = Field(description="Write the pull request review body, including any longer explanation.")

    @model_validator(mode="after")
    def verdict_matches_findings(self) -> Self:
        if (self.review == "changes") != bool(self.findings):
            raise ValueError("changes requires at least one finding and approve requires an empty findings list")
        return self


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
