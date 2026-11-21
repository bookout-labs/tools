"""Worker output contracts shared by the CLI prompts and delivery graph."""

import re
from typing import Annotated, Literal, TypeVar, cast

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints, TypeAdapter, ValidationError, field_validator, model_validator
from pydantic.config import JsonDict

from foreman.dispatch import InvalidOutput

PULL_REQUEST_LINES = 400

Text = Annotated[str, StringConstraints(pattern=r"\S")]
TrimmedText = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"\S")]
Sections = TypeAdapter(dict[TrimmedText, TrimmedText])
Output = TypeVar("Output", bound=BaseModel)
WorkKind = Literal["feature", "bug_fix", "refactoring", "perf_issue"]


# CLIs without schema enforcement may omit nullable fields until S2/S3.
def _require_all_fields(schema: JsonDict) -> None:
    properties = cast(dict[str, JsonDict], schema["properties"])
    schema["required"] = list(properties)
    for field in properties.values():
        field.pop("default", None)


class Contract(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", json_schema_extra=_require_all_fields)


class SliceProperty(Contract):
    name: TrimmedText = Field(description="State the database rule in plain words.")
    constraint: TrimmedText = Field(description="Give the exact PostgreSQL constraint name, such as deals_external_id_key.")


def _describe_slice(schema: JsonDict) -> None:
    _require_all_fields(schema)
    schema["description"] = f"One pull request's outcome, about {PULL_REQUEST_LINES} changed lines or fewer, and its proof obligations."


class Slice(Contract):
    model_config = ConfigDict(json_schema_extra=_describe_slice)

    title: TrimmedText = Field(description="Name the outcome.")
    stateful: bool = Field(description="Set true when the slice changes behavior across states or transitions.")
    acceptance: TrimmedText = Field(description="State how to check the outcome, including response and other behavior checks.")
    depends_on: list[TrimmedText] = Field(
        description="List the titles of other slices from the same parent that must land first. Use an empty list if none must land first.",
    )
    screen: str = Field(
        description=(
            "When the change alters a screen described under docs/screens/, name that Markdown file directly under "
            "docs/screens/ without '..'. Use an empty string when no documented screen changes."
        ),
        pattern=r"^(docs/screens/[^/\\]+\.md)?$",
    )
    properties: list[SliceProperty] = Field(
        description="List only named PostgreSQL CHECK, UNIQUE, FOREIGN KEY, EXCLUSION, or NOT NULL constraints. Use an empty list when the slice adds no database rule.",
    )

    @field_validator("screen")
    @classmethod
    def screen_path(cls, value: str) -> str:
        if ".." in value or re.search(r"[\x00-\x1f]", value):
            raise ValueError("must be empty or a docs/screens/<file>.md path without '..'")
        return value


class HumanQuestionOutput(Contract):
    human_question: str | None = Field(
        default=None,
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
        default=None,
        description="For a parent issue that triages as implement, return its single slice. Use null otherwise, including for slice issues.",
    )


class StoredSlice(Slice):
    stateful: bool = False
    work_kind: WorkKind | None = Field(default=None, exclude=True)


class ResearchOutput(Contract):
    research: Text


class SpecOutput(HumanQuestionOutput):
    spec: Text
    spec_gate: Literal["ready", "needs_human"]
    slices: list[Slice] | None = Field(
        default=None,
        description="For a ready parent spec, return one entry per pull request. Use null otherwise.",
    )


class BrowserProof(Contract):
    """For a required screen, report only browser evidence you actually collected."""

    screen: str | None = None  # CLIs without schema enforcement may omit this.
    trace: str | None = None  # CLIs without schema enforcement may omit this.
    passed: bool | None = None  # CLIs without schema enforcement may omit this.


class ConstraintResult(Contract):
    constraint: Text
    rejected: bool


class FormalProof(Contract):
    """For required properties, report only formal evidence you actually collected."""

    ran: bool | None = None  # CLIs without schema enforcement may omit this.
    constraints: list[ConstraintResult] | None = None  # CLIs without schema enforcement may omit this.
    typecheck: str | bool | None = None  # CLIs without schema enforcement may omit this.


class ImplementOutput(Contract):
    implementation: Text
    # CLIs without schema enforcement may omit the head SHA.
    head_sha: Annotated[str | None, AfterValidator(lambda sha: (sha or "").strip() or None)] = None
    browser_proof: BrowserProof | None = None  # CLIs without schema enforcement may omit this.
    formal_proof: FormalProof | None = None  # CLIs without schema enforcement may omit this.

    # CLIs without schema enforcement can return sections until S2/S3.
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
    review_note: str | None = Field(
        default=None,
        description="Put the reviewer's requested changes here and keep the verdict in review. Use null when there is no note.",
    )

    # CLIs without schema enforcement can punctuate verdicts until S2/S3.
    @model_validator(mode="before")
    @classmethod
    def split_verdict(cls, value: object) -> object:
        if isinstance(value, dict) and isinstance(verdict := value.get("review"), str):
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


def parse_output(contract: type[Output], raw: object) -> Output:
    try:
        return contract.model_validate(raw)
    except ValidationError as exc:
        error = exc.errors(include_input=False)[0]
        path = ".".join(map(str, error["loc"])) or "$"
        raise InvalidOutput(f"{path}: {error['msg']}") from exc
