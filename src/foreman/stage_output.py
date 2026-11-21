"""Worker output contracts shared by the CLI prompts and delivery graph."""

import json
from hashlib import sha256
from typing import Annotated, Generic, ClassVar, Literal, Self, TypeVar

from abc import ABC, abstractmethod
from dataclasses import dataclass

from typing_extensions import NotRequired, TypedDict, override

from pydantic import BaseModel, ConfigDict, JsonValue, Field, NonNegativeInt, PositiveInt, StringConstraints, TypeAdapter, ValidationError, with_config, field_validator, model_validator, computed_field

from pydantic.config import JsonDict

from foreman.config import ModelSpec
from foreman.adapters import OutcomeKind
from foreman.usd import CostUsd
from foreman.github import PullRequest, PullRequestRecord
from foreman.workspace import ReviewScopeRecord
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
class SpecAuthorRecord(TypedDict):
    stage: Literal["triage", "spec"]
    model: str
    family: str


@with_config(ConfigDict(extra="forbid"))
class SliceProvenanceRecord(TypedDict):
    author: SpecAuthorRecord
    revision: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


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
    provenance: NotRequired[SliceProvenanceRecord]


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
    optional_record_fields: ClassVar[set[str]] = {"depends_on", "blocker_numbers", "provenance"}
    @classmethod
    @abstractmethod
    def record_adapter(cls) -> TypeAdapter[Record]:
        raise NotImplementedError

    def record(self) -> Record:
        excluded = {key for key in self.optional_record_fields if key not in self.model_fields_set}
        if getattr(self, "provenance", None) is None:
            excluded.add("provenance")
        return self.record_adapter().validate_python(self.model_dump(exclude=excluded), strict=True)


@with_config(ConfigDict(extra="forbid"))
class AttemptUsageRecord(TypedDict):
    input: NonNegativeInt
    output: NonNegativeInt
    cache_read: NonNegativeInt
    cache_creation: NonNegativeInt
    cost_usd: CostUsd | None


SelectionReason = Literal["no_other_family_configured", "preceding_candidates_unavailable"]


@with_config(ConfigDict(extra="forbid"))
class ReviewSelectionRecord(TypedDict):
    reviewer: str
    contributors: list[str]
    same_family: bool
    reason: SelectionReason | None


class ReviewSelection(RecordContract[ReviewSelectionRecord]):
    @classmethod
    @override
    def record_adapter(cls) -> TypeAdapter[ReviewSelectionRecord]:
        return TypeAdapter(ReviewSelectionRecord)

    reviewer: str
    contributors: list[str]
    same_family: bool
    reason: SelectionReason | None


@with_config(ConfigDict(extra="forbid"))
class AttemptRecord(TypedDict):
    stage: str
    model: str | None
    commit: str
    methods: list[str]
    outcome: OutcomeKind | Literal["interrupted"]
    usage: AttemptUsageRecord | None
    provenance: NotRequired[ReviewSelectionRecord]


class AttemptUsage(RecordContract[AttemptUsageRecord]):
    @classmethod
    @override
    def record_adapter(cls) -> TypeAdapter[AttemptUsageRecord]:
        return TypeAdapter(AttemptUsageRecord)

    input: NonNegativeInt
    output: NonNegativeInt
    cache_read: NonNegativeInt
    cache_creation: NonNegativeInt
    cost_usd: CostUsd | None


class Attempt(RecordContract[AttemptRecord]):
    @classmethod
    @override
    def record_adapter(cls) -> TypeAdapter[AttemptRecord]:
        return TypeAdapter(AttemptRecord)

    stage: str
    model: str | None
    commit: str
    methods: list[str]
    outcome: OutcomeKind | Literal["interrupted"]
    usage: AttemptUsage | None
    provenance: ReviewSelection | None = None


@with_config(ConfigDict(extra="forbid"))
class PendingDispatchRecord(TypedDict):
    stage: str
    index: NonNegativeInt
    quota_model: str | None


class PendingDispatch(RecordContract[PendingDispatchRecord]):
    @classmethod
    @override
    def record_adapter(cls) -> TypeAdapter[PendingDispatchRecord]:
        return TypeAdapter(PendingDispatchRecord)

    stage: str
    index: NonNegativeInt
    quota_model: str | None


@with_config(ConfigDict(extra="forbid"))
class DispatchPlanRecord(TypedDict):
    models: list[str]
    instruction: str
    methods: list[str]
    target: PullRequestRecord | None
    scope: ReviewScopeRecord | None


class DispatchPlan(RecordContract[DispatchPlanRecord]):
    @classmethod
    @override
    def record_adapter(cls) -> TypeAdapter[DispatchPlanRecord]:
        return TypeAdapter(DispatchPlanRecord)

    models: list[str]
    instruction: str
    methods: list[str]
    target: PullRequest | None
    scope: ReviewScopeRecord | None


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


class SliceContent(RecordContract[Record], Generic[Record]):
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


class Slice(SliceContent[SliceRecord]):
    @classmethod
    @override
    def record_adapter(cls) -> TypeAdapter[SliceRecord]:
        return TypeAdapter(SliceRecord)


class SpecAuthor(RecordContract[SpecAuthorRecord]):
    stage: Literal["triage", "spec"]
    model: TrimmedText
    family: TrimmedText

    @classmethod
    @override
    def record_adapter(cls) -> TypeAdapter[SpecAuthorRecord]:
        return TypeAdapter(SpecAuthorRecord)


class SliceProvenance(RecordContract[SliceProvenanceRecord]):
    author: SpecAuthor
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")

    @classmethod
    @override
    def record_adapter(cls) -> TypeAdapter[SliceProvenanceRecord]:
        return TypeAdapter(SliceProvenanceRecord)


@with_config(ConfigDict(extra="forbid"))
class CandidateSliceRecord(TypedDict):
    title: str
    stateful: bool
    acceptance: str
    builds: list[SeamRecord]
    uses: list[SeamRecord]
    screen: str
    properties: list[SlicePropertyRecord]
    depends_on: list[str]
    blocker_numbers: list[int]


class CandidateSlice(SliceContent[CandidateSliceRecord]):
    @classmethod
    @override
    def record_adapter(cls) -> TypeAdapter[CandidateSliceRecord]:
        return TypeAdapter(CandidateSliceRecord)
    optional_record_fields: ClassVar[set[str]] = set()
    depends_on: list[TrimmedText] = Field(default_factory=list[str])
    blocker_numbers: list[PositiveInt] = Field(default_factory=list[int])


@with_config(ConfigDict(extra="forbid"))
class SpecCandidateRecord(TypedDict):
    slices: list[CandidateSliceRecord]
    spec: str
    work_kind: WorkKind
    author: SpecAuthorRecord
    revision: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class SpecCandidate(RecordContract[SpecCandidateRecord]):
    slices: list[CandidateSlice] = Field(min_length=1)
    spec: str
    work_kind: WorkKind
    author: SpecAuthor
    model_config = ConfigDict(frozen=True)

    @classmethod
    @override
    def record_adapter(cls) -> TypeAdapter[SpecCandidateRecord]:
        return TypeAdapter(SpecCandidateRecord)

    @field_validator("slices")
    @classmethod
    def unique_titles(cls, value: list[CandidateSlice]) -> list[CandidateSlice]:
        if len({s.title for s in value}) != len(value):
            raise ValueError("candidate.slices: titles must be unique")
        return value

    @computed_field
    @property
    def revision(self) -> str:
        content = json.dumps(self.model_dump(exclude={"revision"}), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return sha256(content.encode("utf-8")).hexdigest()

    @classmethod
    def from_record(cls, raw: object) -> Self:
        record = cls.record_adapter().validate_python(raw, strict=True)
        candidate = cls.model_validate({k: v for k, v in record.items() if k != "revision"})
        if record["revision"] != candidate.revision:
            raise ValueError("candidate revision does not match its content")
        return candidate

    def update(self, *, single: bool) -> "StageUpdate":
        provenance: SliceProvenanceRecord = {"author": self.author.record(), "revision": self.revision}
        slices: list[SliceRecord] = [{**item, "provenance": provenance} for item in self.record()["slices"]]
        update: StageUpdate = {"spec_candidate": self.record(), "spec": self.spec, "work_kind": self.work_kind, "slices": slices}
        if single:
            update["slice"] = slices[0]
        return update


class StoredSlice(Slice):
    stateful: bool = False
    work_kind: WorkKind | None = Field(default=None, exclude=True)
    provenance: SliceProvenance | None = None
    builds: list[Seam] = Field(default_factory=list[Seam])
    uses: list[Seam] = Field(default_factory=list[Seam])
    depends_on: list[TrimmedText] = Field(default_factory=list[str])
    blocker_numbers: list[PositiveInt] = Field(default_factory=list[int])


class PullRequestSplit(TypedDict):
    plan: str
    gate: Literal["ready", "needs_human"]
    question: str | None
    slices: list[SliceRecord]


class StageUpdate(TypedDict, total=False):
    spec_candidate: SpecCandidateRecord
    triage: str
    work_kind: WorkKind
    human_question: str
    slice: SliceRecord
    research: str
    spec: str
    spec_gate: str
    spec_rejection: None
    slices: list[SliceRecord]
    pr_split: PullRequestSplit
    implementation: str
    browser_proof: BrowserProofRecord | None
    formal_proof: FormalProofRecord | None
    risk: MergeRiskRecord
    detail: str
    findings: list[FindingRecord]
    verify: str


@dataclass(frozen=True)
class UpdateContext:
    issue_kind: str | None
    work_kind: WorkKind | None
    oversized: bool
    slices: list[SliceRecord]
    model: ModelSpec | None = None


class StageOutput(Contract, ABC):
    @abstractmethod
    def update(self, context: UpdateContext) -> StageUpdate:
        raise NotImplementedError


class HumanQuestionOutput(StageOutput):
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

    @override
    def update(self, context: UpdateContext) -> StageUpdate:
        update: StageUpdate = {
            "triage": self.triage, "work_kind": self.work_kind, "human_question": self.human_question or "",
        }
        if context.issue_kind == "slice" and context.work_kind is not None:
            update["work_kind"] = context.work_kind
        if context.issue_kind == "parent" and self.triage == "implement":
            if self.slice is None:
                raise InvalidOutput("slice: required when a parent triages as implement")
            if context.model is not None:
                update.update(SpecCandidate(slices=[CandidateSlice.model_validate(self.slice.record())], spec="",
                    work_kind=self.work_kind, author=SpecAuthor(stage="triage", model=context.model.name,
                    family=context.model.family)).update(single=True))
            else:
                update["slice"] = self.slice.record()
        return update


class ResearchOutput(StageOutput):
    research: Text

    @override
    def update(self, context: UpdateContext) -> StageUpdate:
        return {"research": self.research}


class SpecOutput(HumanQuestionOutput):
    spec: Text
    spec_gate: Literal["ready", "needs_human"]
    slices: list[Slice] | None = Field(
        description="For a ready parent spec, return one entry per pull request. Use null otherwise.",
    )

    @override
    def update(self, context: UpdateContext) -> StageUpdate:
        slices = context.slices
        if context.oversized:
            return {
                "pr_split": {"plan": self.spec, "gate": self.spec_gate, "question": self.human_question, "slices": slices},
                "spec_rejection": None,
            }
        update: StageUpdate = {
            "spec": self.spec, "spec_gate": self.spec_gate,
            "human_question": self.human_question or "", "spec_rejection": None,
        }
        if context.issue_kind == "parent" and self.spec_gate == "ready":
            if context.model is not None and context.work_kind is not None:
                update.update(SpecCandidate(slices=[CandidateSlice.model_validate(item) for item in slices],
                    spec=self.spec, work_kind=context.work_kind, author=SpecAuthor(stage="spec",
                    model=context.model.name, family=context.model.family)).update(single=False))
            else:
                update["slices"] = slices
        return update


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


class ImplementOutput(StageOutput):
    implementation: Text
    browser_proof: BrowserProof | None
    formal_proof: FormalProof | None

    @override
    def update(self, context: UpdateContext) -> StageUpdate:
        return {
            "implementation": self.implementation,
            "browser_proof": self.browser_proof.record() if self.browser_proof else None,
            "formal_proof": self.formal_proof.record() if self.formal_proof else None,
        }


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


class ReviewOutput(StageOutput):
    resolutions: list[FindingResolution] = Field(description="Mark every earlier unresolved reviewer finding once. The graph resolves tool-fact gate findings. Return an empty list in round 1.")
    risk: MergeRisk
    findings: list[Finding] = Field(
        description="List each new requested fix, including nits. Return an empty list when there are none.",
    )
    tool_facts: list[ToolFact] = Field(
        description="List every outside-tool fact the diff relies on, including unrecorded facts. Use an empty list if there are none.",
    )
    detail: Text = Field(description="Write the pull request review body, including any longer explanation.")


    @override
    def update(self, context: UpdateContext) -> StageUpdate:
        return {"risk": self.risk.record(), "detail": self.detail,
                "findings": [finding.record() for finding in self.findings]}


class SpecFinding(Contract):
    id: TrimmedText
    kind: Literal["proof-obligation", "seam", "failure-path", "contract", "scope"]
    slice: TrimmedText
    section: Literal["slice", "spec", "work_kind"]
    summary: TrimmedText = Field(max_length=200)
    ruling_id: TrimmedText | None

    @property
    def severity(self) -> Literal["blocking"]:
        return "blocking"


class SpecCheckOutput(StageOutput):
    findings: list[SpecFinding] = Field(description="New blocking gaps only. Use resolutions for earlier IDs.")
    notes: list[SpecFinding] = Field(description="Nonblocking observations, separate from requested fixes.")
    resolutions: list[FindingResolution]

    @override
    def update(self, context: UpdateContext) -> StageUpdate:
        raise NotImplementedError("spec_check: use candidate-bound spec_check.accept")


class VerifyOutput(StageOutput):
    verify: Literal["pass", "fail"]

    @override
    def update(self, context: UpdateContext) -> StageUpdate:
        return {"verify": self.verify}


STAGE_OUTPUTS: dict[str, type[StageOutput]] = {
    "triage": TriageOutput,
    "research": ResearchOutput,
    "spec": SpecOutput,
    "spec_check": SpecCheckOutput,
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
