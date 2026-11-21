"""Worker output contracts shared by the CLI prompts and delivery graph."""

from hashlib import sha256
from typing import Annotated, Generic, Literal, Self, TypeVar

from abc import abstractmethod

from typing_extensions import NotRequired, TypedDict, override

from pydantic import BaseModel, ConfigDict, JsonValue, Field, PositiveInt, StringConstraints, TypeAdapter, ValidationError, with_config, field_validator, model_validator

from pydantic.config import JsonDict

from foreman.dispatch import InvalidOutput

PULL_REQUEST_LINES = 400

Text = Annotated[str, StringConstraints(pattern=r"\S")]
TrimmedText = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"\S")]
Output = TypeVar("Output", bound=BaseModel)
WorkKind = Literal["feature", "bug_fix", "refactoring", "perf_issue"]
Severity = Literal["blocking", "nit"]
ResolutionStatus = Literal["resolved", "unresolved"]


@with_config(ConfigDict(extra="forbid"))
class SlicePropertyRecord(TypedDict):
    name: str
    constraint: str


@with_config(ConfigDict(extra="forbid"))
class SeamRecord(TypedDict):
    name: str
    exists: bool
    path: str | None


@with_config(ConfigDict(extra="forbid"))
class SliceRecord(TypedDict):
    title: str
    stateful: bool
    acceptance: str
    builds: list[SeamRecord]
    uses: list[SeamRecord]
    screen: str
    properties: list[SlicePropertyRecord]
    depends_on: NotRequired[list[str]]
    blocker_numbers: NotRequired[list[int]]


@with_config(ConfigDict(extra="forbid"))
class BrowserProofRecord(TypedDict):
    screen: str | None
    trace: str | None
    passed: bool | None


@with_config(ConfigDict(extra="forbid"))
class ConstraintResultRecord(TypedDict):
    constraint: str
    rejected: bool


@with_config(ConfigDict(extra="forbid"))
class FormalProofRecord(TypedDict):
    ran: bool | None
    constraints: list[ConstraintResultRecord] | None
    typecheck: str | bool | None


@with_config(ConfigDict(extra="forbid"))
class MergeRiskRecord(TypedDict):
    alignment: str
    impact: str
    verification: str
    rollback: str
    error_detection: str
    level: Literal["low", "medium", "high"]


@with_config(ConfigDict(extra="forbid"))
class GraphCheckRecord(TypedDict):
    head: str
    command: str
    exit_code: int


@with_config(ConfigDict(extra="forbid"))
class ToolFactRecord(TypedDict):
    graph_check: GraphCheckRecord | None
    claim: str
    fixture: str | None


@with_config(ConfigDict(extra="forbid"))
class FindingRecord(TypedDict):
    id: str
    kind: Literal["correctness", "contract-unenforced", "duplicate-owner", "hand-built-parsing", "untested-path", "docs"]
    fix: str
    ruling_id: str | None
    file: str
    line: int
    severity: Severity
    summary: str


class Contract(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


Record = TypeVar("Record")


class RecordContract(Contract, Generic[Record]):
    @classmethod
    @abstractmethod
    def record_adapter(cls) -> TypeAdapter[Record]:
        raise NotImplementedError

    def record(self) -> Record:
        excluded = {key for key in ("depends_on", "blocker_numbers") if key not in self.model_fields_set}
        return self.record_adapter().validate_python(self.model_dump(exclude=excluded), strict=True)


class SliceProperty(RecordContract[SlicePropertyRecord]):
    @classmethod
    @override
    def record_adapter(cls) -> TypeAdapter[SlicePropertyRecord]:
        return TypeAdapter(SlicePropertyRecord)
    name: TrimmedText = Field(description="State the database rule in plain words.")
    constraint: TrimmedText = Field(description="Give the exact PostgreSQL constraint name, such as deals_external_id_key.")


class Seam(RecordContract[SeamRecord]):
    @classmethod
    @override
    def record_adapter(cls) -> TypeAdapter[SeamRecord]:
        return TypeAdapter(SeamRecord)
    name: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$", description="Use the same short identifier in every slice that builds or uses this seam.")
    exists: bool = Field(description="Set true for a seam that already exists in the checkout. Otherwise use false.")
    path: TrimmedText | None = Field(description="For an existing seam, give a workspace-relative file path. Otherwise use null.")

    @model_validator(mode="after")
    def existing_file(self) -> Self:
        if self.exists != (self.path is not None):
            raise ValueError("path is required exactly when exists is true")
        return self


def _describe_slice(schema: dict[str, JsonValue]) -> None:
    schema["description"] = f"One pull request's outcome, about {PULL_REQUEST_LINES} changed lines or fewer, and its proof obligations."


class Slice(RecordContract[SliceRecord]):
    @classmethod
    @override
    def record_adapter(cls) -> TypeAdapter[SliceRecord]:
        return TypeAdapter(SliceRecord)
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
    builds: list[Seam] = Field(default_factory=list[Seam])
    uses: list[Seam] = Field(default_factory=list[Seam])
    depends_on: list[TrimmedText] = Field(default_factory=list[str])
    blocker_numbers: list[PositiveInt] = Field(default_factory=list[int])


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


class BrowserProof(RecordContract[BrowserProofRecord]):
    """For a required screen, report only browser evidence you actually collected."""

    @classmethod
    @override
    def record_adapter(cls) -> TypeAdapter[BrowserProofRecord]:
        return TypeAdapter(BrowserProofRecord)

    screen: str | None
    trace: str | None
    passed: bool | None


class ConstraintResult(RecordContract[ConstraintResultRecord]):
    @classmethod
    @override
    def record_adapter(cls) -> TypeAdapter[ConstraintResultRecord]:
        return TypeAdapter(ConstraintResultRecord)
    constraint: Text
    rejected: bool


class FormalProof(RecordContract[FormalProofRecord]):
    """For required properties, report only formal evidence you actually collected."""

    @classmethod
    @override
    def record_adapter(cls) -> TypeAdapter[FormalProofRecord]:
        return TypeAdapter(FormalProofRecord)

    ran: bool | None
    constraints: list[ConstraintResult] | None
    typecheck: str | bool | None


class ImplementOutput(Contract):
    implementation: Text
    browser_proof: BrowserProof | None
    formal_proof: FormalProof | None


class MergeRisk(RecordContract[MergeRiskRecord]):
    @classmethod
    @override
    def record_adapter(cls) -> TypeAdapter[MergeRiskRecord]:
        return TypeAdapter(MergeRiskRecord)
    alignment: TrimmedText
    impact: TrimmedText
    verification: TrimmedText
    rollback: TrimmedText
    error_detection: TrimmedText
    level: Literal["low", "medium", "high"]


class GraphCheck(Contract):
    head: Text
    command: Text
    exit_code: int

    @property
    def claim(self) -> str:
        return f"Check {self.command!r} exited {self.exit_code} at {self.head}."


def _require_properties(schema: JsonDict) -> None:
    properties = schema["properties"]
    assert isinstance(properties, dict)
    schema["required"] = list(properties)


class ToolFact(RecordContract[ToolFactRecord]):
    @classmethod
    @override
    def record_adapter(cls) -> TypeAdapter[ToolFactRecord]:
        return TypeAdapter(ToolFactRecord)

    model_config = ConfigDict(json_schema_extra=_require_properties)
    claim: TrimmedText = Field(description="State the outside-tool behavior the diff relies on.")
    graph_check: GraphCheck | None = Field(default=None, description="Only for an exact graph check restatement. Copy its claim verbatim and its structured evidence; otherwise null.")
    fixture: TrimmedText | None = Field(
        description="Give the exact entry ID in the run state fact_record. Use null when the claim has no recorded evidence.",
    )


class StoredToolFact(ToolFact):
    claim_id: Text

    @classmethod
    def from_fact(cls, fact: ToolFact) -> "StoredToolFact":
        return cls(**fact.model_dump(), claim_id="tool-fact-" + sha256(fact.claim.encode()).hexdigest())


class Finding(RecordContract[FindingRecord]):
    @classmethod
    @override
    def record_adapter(cls) -> TypeAdapter[FindingRecord]:
        return TypeAdapter(FindingRecord)
    id: TrimmedText = Field(description="Unique finding ID. Never reuse an earlier finding ID.")
    kind: Literal["correctness", "contract-unenforced", "duplicate-owner", "hand-built-parsing", "untested-path", "docs"]
    fix: Text = Field(description="State the requested fix.")
    ruling_id: str | None = Field(description="If this contradicts a settled ruling, give its ID so it becomes a coordinator note. Otherwise null.")
    file: Text = Field(description="Give the path relative to the repository root.")
    line: int = Field(description="Give the line number in the reviewed commit.")
    severity: Severity
    summary: Text = Field(max_length=200)


class FindingResolution(Contract):
    id: TrimmedText
    status: ResolutionStatus


class SettledRuling(Contract):
    id: TrimmedText
    text: Text


class ReviewOutput(Contract):
    resolutions: list[FindingResolution] = Field(description="Mark every earlier unresolved reviewer finding once. The graph resolves tool-fact gate findings. Return an empty list in round 1.")
    risk: MergeRisk
    findings: list[Finding] = Field(
        description="List each new requested fix, including nits. Return an empty list when there are none.",
    )
    tool_facts: list[ToolFact] = Field(
        description="List every outside-tool fact the diff relies on, including unrecorded facts. Use an empty list if there are none.",
    )
    detail: Text = Field(description="Write the pull request review body, including any longer explanation.")


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
