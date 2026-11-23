"""Worker output contracts shared by the CLI prompts and delivery graph."""

import json
from hashlib import sha256
from typing import Annotated, Generic, ClassVar, Literal, Self, TypeVar

from abc import ABC, abstractmethod
from dataclasses import dataclass

from typing_extensions import NotRequired, TypedDict, override

from pydantic import BaseModel, ConfigDict, JsonValue, Field, NonNegativeInt, PositiveInt, StringConstraints, TypeAdapter, ValidationError, ValidationInfo, with_config, field_validator, model_validator, computed_field

from pydantic_core import PydanticCustomError

from foreman.review_context import ReviewContext
from foreman.contracts import Contract as Contract, Text as Text
from foreman.config import ModelSpec
from foreman.adapters import NativeUsage, OutcomeKind
from foreman.usd import CostUsd
from foreman.github import PullRequest, PullRequestRecord, ReviewReceipt
from foreman.workspace import ReviewScopeRecord
from foreman.dispatch import InvalidOutput

PULL_REQUEST_LINES = 400

TrimmedText = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"\S")]
Output = TypeVar("Output", bound=BaseModel)
WorkKind = Literal["feature", "bug_fix", "refactoring", "perf_issue"]
Severity = Literal["P0", "P1", "P2", "P3", "P4"]
BlockingSeverity = Literal["P0", "P1"]
FindingKind = Literal["correctness", "security", "scope", "other"]
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


@with_config(ConfigDict(extra="forbid", strict=True))
class CitationRecord(TypedDict):
    side: Literal["head", "base"]
    path: TrimmedText
    line: PositiveInt | None


@with_config(ConfigDict(extra="forbid"))
class FindingRecord(TypedDict):
    id: str
    kind: FindingKind
    location: CitationRecord
    ruling_id: str | None
    impact: str
    evidence: str
    severity: Severity
    summary: str


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
    cache_read: NonNegativeInt | None
    cache_creation: NonNegativeInt | None
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
    prompt_bytes: NotRequired[NonNegativeInt | None]
    native_usage: NotRequired[NativeUsage | None]
    detail: NotRequired[str | None]
    provenance: NotRequired[ReviewSelectionRecord]


class AttemptUsage(RecordContract[AttemptUsageRecord]):
    @classmethod
    @override
    def record_adapter(cls) -> TypeAdapter[AttemptUsageRecord]:
        return TypeAdapter(AttemptUsageRecord)

    input: NonNegativeInt
    output: NonNegativeInt
    cache_read: NonNegativeInt | None
    cache_creation: NonNegativeInt | None
    cost_usd: CostUsd | None


class Attempt(RecordContract[AttemptRecord]):
    optional_record_fields: ClassVar[set[str]] = RecordContract.optional_record_fields | {"prompt_bytes", "native_usage", "detail"}

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
    prompt_bytes: NonNegativeInt | None = None
    native_usage: NativeUsage | None = None
    detail: str | None = None
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
    budget_usd: NotRequired[str | None]
    models: list[str]
    instruction: str
    methods: list[str]
    read_roots: NotRequired[list[str]]
    target: PullRequestRecord | None
    scope: ReviewScopeRecord | None


class DispatchPlan(RecordContract[DispatchPlanRecord]):
    @classmethod
    @override
    def record_adapter(cls) -> TypeAdapter[DispatchPlanRecord]:
        return TypeAdapter(DispatchPlanRecord)

    budget_usd: str | None = None
    models: list[str]
    instruction: str
    methods: list[str]
    read_roots: list[str] = Field(default_factory=list)
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
    builds: list[Seam] = Field(description="List only new seams this slice provides for other slices, with exists false and path null. A seam is an interface, module, or contract, not a file; a new seam may be implemented in an existing file. Pure file edits belong in uses and acceptance/spec, not builds.")
    uses: list[Seam] = Field(description="List seams this slice needs, including existing seams it edits with exists true and a workspace-relative path. Describe the edit in acceptance/spec. Edit-only example: builds: [], uses: [{name: items, exists: true, path: items.py}], acceptance: Change visible(items) to include closed items. Use an empty list when there are none.")
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
        if existing := [seam.name for seam in value if seam.exists]:
            raise PydanticCustomError("existing_seam_builder",
                "Existing seams {seams} cannot be in builds. For edits, move them to uses with exists true "
                "and describe the edit in acceptance/spec; for a genuinely new seam, use exists false and path null.",
                {"seams": ", ".join(existing)})
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

    def update(self, plan: "PlanState", *, single: bool) -> "PlanState":
        provenance: SliceProvenanceRecord = {"author": self.author.record(), "revision": self.revision}
        slices: list[SliceRecord] = [{**item, "provenance": provenance} for item in self.record()["slices"]]
        fields: dict[str, object] = {"spec_candidate": self.record(), "spec": self.spec, "work_kind": self.work_kind, "slices": slices}
        if single:
            fields["slice"] = slices[0]
        return plan.model_copy(update=fields)


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


SpecRejectionKind = Literal["duplicate_title", "duplicate_builder", "dangling_seam", "dependency_cycle", "existing_seam_builder"]


class SpecRejection(TypedDict):
    kind: SpecRejectionKind
    detail: str


class InvalidSlicePlan(InvalidOutput):
    def __init__(self, kind: SpecRejectionKind, detail: str):
        super().__init__(detail)
        self.reason: SpecRejection = {"kind": kind, "detail": detail}


@with_config(ConfigDict(extra="forbid"))
class HumanAnswer(TypedDict):
    stage: Literal["triage", "spec", "pr_size", "review"]
    question: str
    answer: str


class PlanState(BaseModel, frozen=True, extra="forbid"):
    # Rejected plans share dispatch receipts and paths with human questions.
    spec_failures: int = 0
    triage: Literal["", "implement", "spec", "question", "needs_human"] = ""
    spec: str = ""
    spec_gate: Literal["", "ready", "needs_human"] = ""
    human_qa: list[HumanAnswer] = Field(default_factory=list[HumanAnswer])
    spec_candidate: SpecCandidateRecord | None = None
    spec_rejection: SpecRejection | None = None
    slices: list[SliceRecord] = Field(default_factory=list[SliceRecord])
    child_issue_numbers: list[int] = Field(default_factory=list[int])
    slice: SliceRecord | None = None
    issue_kind: Literal["parent", "slice"] | None = None
    work_kind: WorkKind | None = None
    pr_split: PullRequestSplit | None = None

    @field_validator("slice", mode="before")
    @classmethod
    def stored_slice(cls, value: object) -> SliceRecord | None:
        return StoredSlice.model_validate(value).record() if value is not None else None


class ReviewState(BaseModel, frozen=True, extra="forbid"):
    review: Literal["", "approve", "changes"] = ""
    review_findings: list[dict[str, object]] = Field(default_factory=list[dict[str, object]])
    reviewed_head: str = ""
    settled_rulings: list[dict[str, object]] = Field(default_factory=list[dict[str, object]])
    findings: list[FindingRecord] = Field(default_factory=list[FindingRecord])
    detail: str = ""
    review_run_id: str = ""
    review_receipt: ReviewReceipt | None = None


class StageUpdate(TypedDict, total=False):
    plan: PlanState
    human_question: str
    research: str
    implementation: str
    review: ReviewState


@dataclass(frozen=True)
class UpdateContext:
    plan: PlanState
    review: ReviewState
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
        plan = context.plan
        current = plan.model_copy(update={"triage": self.triage, "work_kind": self.work_kind})
        if plan.issue_kind == "slice" and plan.work_kind is not None:
            current = current.model_copy(update={"work_kind": plan.work_kind})
        if plan.issue_kind == "parent" and self.triage == "implement":
            if self.slice is None:
                raise InvalidOutput("slice: required when a parent triages as implement")
            if context.model is not None:
                current = SpecCandidate(slices=[CandidateSlice.model_validate(self.slice.record())], spec="",
                    work_kind=self.work_kind, author=SpecAuthor(stage="triage", model=context.model.name,
                    family=context.model.family)).update(current, single=True)
            else:
                current = current.model_copy(update={"slice": self.slice.record()})
        return {"plan": current, "human_question": self.human_question or ""}


class ResearchOutput(StageOutput):
    research: Text

    @override
    def update(self, context: UpdateContext) -> StageUpdate:
        return {"research": self.research}


class SpecQuestion(Contract):
    question: TrimmedText
    why_code_unanswered: TrimmedText
    checked_paths: list[TrimmedText]


class SpecOutput(StageOutput):
    spec: Text
    spec_gate: Literal["ready", "needs_human"]
    questions: list[SpecQuestion]
    slices: list[Slice] | None = Field(
        description="For a ready parent spec, return one entry per pull request. Use null otherwise.",
    )

    @field_validator("questions")
    @classmethod
    def grounded_questions(cls, value: list[SpecQuestion], info: ValidationInfo) -> list[SpecQuestion]:
        gate = info.data.get("spec_gate")
        if gate is not None and bool(value) != (gate == "needs_human"):
            raise ValueError("questions must be non-empty on needs_human and empty on ready")
        return value

    @override
    def update(self, context: UpdateContext) -> StageUpdate:
        plan = context.plan
        slices = context.slices
        question = "\n\n".join(
            f"{item.question}\nWhy code cannot answer: {item.why_code_unanswered}\n"
            f"Checked paths: {', '.join(item.checked_paths) or '(none)'}" for item in self.questions
        )
        if context.oversized:
            return {"plan": plan.model_copy(update={"pr_split": {"plan": self.spec, "gate": self.spec_gate,
                "question": question or None, "slices": slices}, "spec_rejection": None})}
        current = plan.model_copy(update={"spec": self.spec, "spec_gate": self.spec_gate, "spec_rejection": None})
        if self.spec_gate == "needs_human":
            current = current.model_copy(update={"spec_candidate": None})
        if plan.issue_kind == "parent" and self.spec_gate == "ready":
            if context.model is not None and plan.work_kind is not None:
                current = SpecCandidate(slices=[CandidateSlice.model_validate(item) for item in slices],
                    spec=self.spec, work_kind=plan.work_kind, author=SpecAuthor(stage="spec",
                    model=context.model.name, family=context.model.family)).update(current, single=False)
            else:
                current = current.model_copy(update={"slices": slices})
        return {"plan": current, "human_question": question}


class ImplementOutput(StageOutput):
    implementation: Text

    @override
    def update(self, context: UpdateContext) -> StageUpdate:
        raise NotImplementedError("implement: use checks-bound stage_update")


class Finding(RecordContract[FindingRecord]):
    @classmethod
    @override
    def record_adapter(cls) -> TypeAdapter[FindingRecord]:
        return TypeAdapter(FindingRecord)
    id: TrimmedText
    kind: FindingKind
    ruling_id: str | None = Field(description="Give the applicable settled ruling ID, or null.")
    severity: Severity
    summary: Text = Field(max_length=200)
    location: CitationRecord
    impact: Text
    evidence: Text

    @property
    def blocking_severity(self) -> BlockingSeverity | None:
        if self.ruling_id is None and self.severity in ("P0", "P1"):
            return self.severity
        return None


class FindingResolution(Contract):
    id: TrimmedText
    status: ResolutionStatus


class SettledRuling(Contract):
    id: TrimmedText
    text: Text


class ReviewFinding(Finding):
    @field_validator("location")
    @classmethod
    def validate_location(cls, value: CitationRecord, info: ValidationInfo) -> CitationRecord:
        if not isinstance(info.context, ReviewContext):
            raise ValueError("trusted review context required")
        tree = getattr(info.context, value["side"])
        path, line = value["path"], value["line"]
        if path not in tree.files:
            raise ValueError(f"{value['side']} path {path!r} does not exist")
        if line is not None and line > tree.lines.get(path, 0):
            raise ValueError(f"{value['side']} {path}:{line} is not a text line")
        return value

    @model_validator(mode="after")
    def validate_priority(self) -> Self:
        if self.severity in ("P0", "P1") and self.kind == "other":
            raise ValueError("P0 and P1 require a correctness, security or scope defect.")
        return self


class ReviewOutput(HumanQuestionOutput):
    findings: list[ReviewFinding] = Field(description="List evidenced findings in the full current PR.")
    human_question: Text | None = Field(description="Ask only if unresolved intent changes the verdict, otherwise null.")

    @override
    def update(self, context: UpdateContext) -> StageUpdate:
        raise NotImplementedError("review: use exact-head stage acceptance")


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
        raise NotImplementedError("verify: use checks-bound stage_update")


def parse_output(contract: type[Output], raw: object, *, context: ReviewContext | None = None) -> Output:
    try:
        return contract.model_validate(raw, context=context)
    except ValidationError as exc:
        errors = exc.errors(include_input=False)
        if contract is SpecOutput and all(error["type"] == "existing_seam_builder" for error in errors):
            detail = "; ".join(f"{'.'.join(map(str, error['loc']))}: {error['msg']}" for error in errors)
            raise InvalidSlicePlan("existing_seam_builder", detail) from exc
        error = next((error for error in errors if error["type"] != "existing_seam_builder"), errors[0])
        path = ".".join(map(str, error["loc"])) or "$"
        raise InvalidOutput(f"{path}: {error['msg']}") from exc
