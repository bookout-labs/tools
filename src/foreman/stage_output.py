"""Worker output contracts shared by the CLI prompts and delivery graph."""

import json
import re
from hashlib import sha256
from typing import Annotated, Generic, ClassVar, Literal, Self, TypeVar

from abc import ABC, abstractmethod
from dataclasses import dataclass

from typing_extensions import NotRequired, TypedDict, override

from pydantic_core import ErrorDetails
from pydantic import BaseModel, ConfigDict, JsonValue, Field, NonNegativeInt, PositiveInt, StringConstraints, TypeAdapter, ValidationError, ValidationInfo, with_config, field_validator, model_validator, computed_field

from foreman.review_context import ReviewContext
from foreman.contracts import Contract as Contract, Text as Text
from foreman.config import ModelSpec
from foreman.adapters import NativeUsage, OutcomeKind
from foreman.usd import CostUsd
from foreman.github import PullRequest, PullRequestRecord, ReviewReceipt
from foreman.workspace import ReviewScopeRecord
from foreman.contracts import InvalidOutput


TrimmedText = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"\S")]
Output = TypeVar("Output", bound=BaseModel)
WorkKind = Literal["feature", "bug_fix", "refactoring", "perf_issue"]
Severity = Literal["P0", "P1", "P2", "P3", "P4"]
BlockingSeverity = Literal["P0", "P1"]
FindingKind = Literal["correctness", "security", "scope", "other"]


@with_config(ConfigDict(extra="forbid"))
class SlicePropertyRecord(TypedDict):
    name: str
    constraint: str


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
    files: list[str]
    screen: str
    properties: list[SlicePropertyRecord]
    depends_on: NotRequired[list[str]]
    blocker_numbers: NotRequired[list[int]]
    provenance: NotRequired[SliceProvenanceRecord]
    repository: NotRequired[str | None]


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
    optional_record_fields: ClassVar[set[str]] = {"depends_on", "blocker_numbers", "provenance", "repository",
                                                  "resume_session", "repair", "attempt_id", "work_charge", "dollar_charge"}
    @classmethod
    @abstractmethod
    def record_adapter(cls) -> TypeAdapter[Record]:
        raise NotImplementedError

    def record(self) -> Record:
        excluded = {key for key in self.optional_record_fields if key not in self.model_fields_set}
        excluded.update(key for key in ("provenance", "repository") if getattr(self, key, None) is None)
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
    attempt_id: NotRequired[str]
    work_charge: NotRequired[NonNegativeInt]
    dollar_charge: NotRequired[str]


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
    attempt_id: str | None = None
    work_charge: NonNegativeInt | None = None
    dollar_charge: str | None = None


class Reservation(TypedDict):
    id: str
    model: str
    work_allowance: int
    dollar_allowance: str
    fingerprint: str


@with_config(ConfigDict(extra="forbid"))
class DispatchPlanRecord(TypedDict):
    budget_usd: NotRequired[str | None]
    resume_session: NotRequired[str | None]
    repair: NotRequired[str | None]
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
    resume_session: str | None = Field(default=None, pattern=r"^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$")
    # The exact validation error the repair turn sends as the whole prompt (item 1).
    repair: str | None = None
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


def _describe_slice(schema: dict[str, JsonValue]) -> None:
    schema["description"] = "One pull request's outcome and its proof obligations."
    required, properties = schema.get("required"), schema.get("properties")
    if isinstance(required, list) and isinstance(properties, dict) and isinstance(repository := properties.get("repository"), dict):
        repository.pop("default", None)
        required.append("repository")


class SliceContent(RecordContract[Record], Generic[Record]):
    model_config = ConfigDict(json_schema_extra=_describe_slice)

    title: TrimmedText = Field(description="Name the outcome.")
    repository: TrimmedText | None = Field(default=None, description="Explicit slice repository as OWNER/NAME, or null to inherit the admitted repository.")
    stateful: bool = Field(description="Set true when the slice changes behavior across states or transitions.")
    acceptance: TrimmedText = Field(description="State how to check the outcome, including response and other behavior checks.")
    files: list[TrimmedText] = Field(description=(
        "List the files you expect this slice to touch, as paths relative to the repository root. The list is advisory and "
        "does not limit the builder. A path that exists at the pinned base must be a tracked regular file. A path the slice "
        "adds may be absent there. Never give an absolute path, a path with '..', a directory or a symlink. Use an empty "
        "list only when the issue gives no way to tell."))
    depends_on: list[TrimmedText] = Field(description=(
        "List the titles of the other slices in this plan that must merge before this one, each exactly as that slice's "
        "title gives it. Use an empty list when there are none."))
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
    files: list[str]
    screen: str
    properties: list[SlicePropertyRecord]
    depends_on: list[str]
    blocker_numbers: list[int]
    repository: NotRequired[str | None]


class CandidateSlice(SliceContent[CandidateSliceRecord]):
    @classmethod
    @override
    def record_adapter(cls) -> TypeAdapter[CandidateSliceRecord]:
        return TypeAdapter(CandidateSliceRecord)
    optional_record_fields: ClassVar[set[str]] = {"repository"}
    # A filed slice may leave depends_on out of its stored record.
    depends_on: list[TrimmedText] = Field(default_factory=list[str])
    blocker_numbers: list[PositiveInt] = Field(default_factory=list[int])


@with_config(ConfigDict(extra="forbid"))
class SpecTarget(TypedDict):
    repo: str
    issue: int
    base: str


@with_config(ConfigDict(extra="forbid"))
class SpecCandidateRecord(TypedDict):
    slices: list[CandidateSliceRecord]
    spec: str
    work_kind: WorkKind
    author: SpecAuthorRecord
    revision: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    target: NotRequired[SpecTarget | None]


class SpecCandidate(RecordContract[SpecCandidateRecord]):
    slices: list[CandidateSlice] = Field(min_length=1)
    spec: str
    work_kind: WorkKind
    author: SpecAuthor
    target: SpecTarget | None = None
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
        excluded = {"revision", "target"} if self.target is None else {"revision", "author"}
        return spec_digest(self.model_dump(exclude=excluded))

    @override
    def record(self) -> SpecCandidateRecord:
        record = self.model_dump(exclude={"target"} if self.target is None else set())
        record["slices"] = [item.record() for item in self.slices]
        return self.record_adapter().validate_python(record, strict=True)

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


def spec_digest(content: object) -> str:
    return sha256(json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


class StoredSlice(Slice):
    stateful: bool = False
    work_kind: WorkKind | None = Field(default=None, exclude=True)
    provenance: SliceProvenance | None = None
    files: list[TrimmedText] = Field(default_factory=list[str])
    depends_on: list[TrimmedText] = Field(default_factory=list[str])
    blocker_numbers: list[PositiveInt] = Field(default_factory=list[int])


SLICE_FENCE = re.compile(r"^```json[ \t]*\r?\n((?:(?!^```)[^\n]*\n)*)^```[ \t]*\r?$", re.MULTILINE)


def load_stored_slice(body: str) -> StoredSlice:
    blocks = SLICE_FENCE.findall(body)
    if not blocks:
        raise InvalidOutput("slice issue needs a fenced JSON object")
    try:
        return StoredSlice.model_validate(json.loads(blocks[-1]))
    except json.JSONDecodeError as exc:
        raise InvalidOutput(str(exc)) from exc
    except ValidationError as exc:
        # The slice is read strictly in this release's shape, so the detail names every key the shape refuses.
        raise InvalidOutput("; ".join(f"{'.'.join(map(str, error['loc'])) or '$'}: {error['msg']}"
                                      for error in exc.errors(include_input=False))) from exc


SpecRejectionKind = Literal["seam_field", "duplicate_title", "unknown_dependency", "dependency_cycle", "file_path"]


class SpecRejection(TypedDict):
    kind: SpecRejectionKind
    detail: str


class InvalidSlicePlan(InvalidOutput):
    def __init__(self, kind: SpecRejectionKind, detail: str):
        super().__init__(detail)
        self.reason: SpecRejection = {"kind": kind, "detail": detail}


GateStage = Literal["triage", "research", "spec", "spec_check", "implement", "review"]
# Verify dispatches a worker but has no gates, so no human question or answer names it.
DispatchStage = GateStage | Literal["verify"]


@with_config(ConfigDict(extra="forbid"))
class HumanAnswer(TypedDict):
    stage: GateStage | Literal["merge"]
    question: str
    answer: str
    revision: NotRequired[str]
    candidate: NotRequired[str]


class GateReply(TypedDict):
    """The answer a gate's step resumes with. It names the question's interrupt, so a run cut off after it took the answer
    reads that question as closed. A step can ask again after an answer, and the library keeps the earlier answer pending
    beside the new question, so only the name tells which question an answer closed."""
    interrupt: str
    answer: str


class HumanPrompt(Contract):
    # Merge asks without a worker, at its head_unverified gate.
    stage: GateStage | Literal["merge"]
    question: str
    revision: str | None = None
    # The paused step's own attempts, path and models, which the state records only once the step finishes.
    attempts: list[AttemptRecord] = []
    path: list[str] = []
    models_used: dict[str, str] = {}

    @property
    def answer_marker(self) -> str:
        assert self.revision is not None
        return "foreman answer " + "-".join(self.revision[i:i + 8] for i in range(0, len(self.revision), 8))

    @property
    def reply(self) -> str:
        return f"Reply on the issue under the configured GitHub identity. Start with {self.answer_marker} and put the answer on the next line."

    def answer_in(self, comment: str) -> str | None:
        """The answer a comment gives when its first line is this question's reply marker, or None (glossary Reply marker)."""
        marker, _, answer = comment.strip().partition("\n")
        return (answer.strip() or None) if self.revision is not None and marker.strip() == self.answer_marker else None


class SettledRuling(Contract):
    id: TrimmedText
    text: Text


class _SpecFinding(Contract):
    id: TrimmedText
    kind: Literal["proof-obligation", "failure-path", "contract", "scope"]
    summary: TrimmedText = Field(max_length=200)
    ruling_id: TrimmedText | None

    @property
    def severity(self) -> Literal["blocking"]:
        return "blocking"


class SliceFinding(_SpecFinding):
    section: Literal["slice"] = Field(description="The finding concerns one candidate slice.")
    slice: TrimmedText = Field(description="The exact title of that candidate slice.")


class PlanFinding(_SpecFinding):
    section: Literal["spec", "work_kind"] = Field(description="The finding concerns the candidate's spec or work_kind, not one slice.")


# A finding about the whole plan names no slice, so the schema itself rules out an invented slice title.
SpecFinding = SliceFinding | PlanFinding


class SpecRound(Contract):
    candidate: SpecCandidateRecord
    findings: list[SpecFinding]
    notes: list[SpecFinding]

    @field_validator("candidate")
    @classmethod
    def valid_candidate(cls, value: SpecCandidateRecord) -> SpecCandidateRecord:
        return SpecCandidate.from_record(value).record()


@with_config(ConfigDict(extra="forbid"))
class SpecHistoryRecord(TypedDict, total=False):
    rounds: list[SpecRound]
    settled: list[SettledRuling]


class PlanState(BaseModel, frozen=True, extra="forbid"):
    triage: Literal["", "question", "plan", "supplied_slice", "needs_human"] = ""
    spec: str = ""
    spec_gate: Literal["", "ready", "needs_human"] = ""
    human_qa: list[HumanAnswer] = Field(default_factory=list[HumanAnswer])
    spec_candidate: SpecCandidateRecord | None = None
    spec_rejection: SpecRejection | None = None
    rejected_revisions: list[str] = Field(default_factory=list[str])
    spec_history: SpecHistoryRecord = Field(default_factory=SpecHistoryRecord)
    slices: list[SliceRecord] = Field(default_factory=list[SliceRecord])
    slice: SliceRecord | None = None
    issue_kind: Literal["parent", "slice"] | None = None
    work_kind: WorkKind | None = None

    @property
    def spec_failures(self) -> int:
        return len(set(self.rejected_revisions))

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


VerifyResult = Literal["passed", "failed", "unverified"]


@with_config(ConfigDict(extra="forbid"))
class VerifyCriterionRecord(TypedDict):
    criterion: str
    result: VerifyResult
    observation: str
    evidence: str


class VerifyState(BaseModel, frozen=True, extra="forbid"):
    """What Verify found for one published head: no skill, or the driven criteria and whether they posted."""

    outcome: Literal["", "no_skill", "driven", "posted"] = ""
    tested_head: str = ""
    criteria: list[VerifyCriterionRecord] = Field(default_factory=list[VerifyCriterionRecord])
    stale: bool = False


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
    triage: Literal["question", "plan", "supplied_slice", "needs_human"] = Field(
        description=(
            "Use supplied_slice when the behavior and acceptance are decided and the work ships as one pull request. "
            "Use plan when it needs a plan or splits at a real dependency. Use question when the issue asks for "
            "information and not a change. Use needs_human when the issue intent is missing."
        ),
    )
    slice: Slice | None = Field(
        description="For a parent issue that triages as supplied_slice, return its single slice. Use null otherwise, including for slice issues.",
    )

    @model_validator(mode="after")
    def asks_when_intent_is_missing(self) -> Self:
        if self.triage == "needs_human" and not self.human_question:
            raise ValueError("human_question: required when triage is needs_human")
        return self

    @override
    def update(self, context: UpdateContext) -> StageUpdate:
        plan = context.plan
        current = plan.model_copy(update={"triage": self.triage, "work_kind": self.work_kind})
        if plan.issue_kind == "slice" and plan.work_kind is not None:
            current = current.model_copy(update={"work_kind": plan.work_kind})
        if plan.issue_kind == "parent" and self.triage == "supplied_slice":
            if self.slice is None:
                raise InvalidOutput("slice: required when a parent triages as supplied_slice")
            current = current.model_copy(update={"slice": self.slice.record()})
        return {"plan": current, "human_question": self.human_question or ""}


class ResearchOutput(HumanQuestionOutput):
    research: Text | None = Field(
        description="The cited answer for the pinned base. Use null only when asking the human for missing evidence.",
    )

    @model_validator(mode="after")
    def answers_or_asks(self) -> Self:
        if (self.research is None) == (self.human_question is None):
            raise ValueError("research: return exactly one of research and human_question")
        return self

    @override
    def update(self, context: UpdateContext) -> StageUpdate:
        if self.research is None:
            return {"human_question": self.human_question or ""}
        return {"research": self.research, "human_question": ""}


class SpecQuestion(Contract):
    question: TrimmedText
    why_code_unanswered: TrimmedText
    checked_paths: list[TrimmedText]


class SpecOutput(StageOutput):
    spec: Text
    spec_gate: Literal["ready", "needs_human"]
    questions: list[SpecQuestion]
    slices: list[Slice] | None = Field(
        description="Return one entry per proposed pull request, including any tentative slices when asking a question. Use null when no slices are proposed.",
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
        current = plan.model_copy(update={"spec": self.spec, "spec_gate": self.spec_gate, "spec_rejection": None,
            "slices": slices, "spec_candidate": None})
        return {"plan": current, "human_question": question}


class ImplementOutput(HumanQuestionOutput):
    implementation: Text
    no_change: TrimmedText | None = Field(
        description="Only on a first attempt with no edits and no human_question: the checks showing the acceptance already "
                    "holds on the pinned base, and their results. Otherwise null.")
    human_question: TrimmedText | None = Field(
        description="Ask only when an unresolved acceptance choice blocks further editing, or when the code contradicts a "
                    "fact the plan states. Then cite the plan fact and the code, and build nothing. Otherwise null.")

    @model_validator(mode="after")
    def asks_or_reports_no_change(self) -> Self:
        if self.human_question and self.no_change:
            raise ValueError("no_change: return at most one of human_question and no_change")
        return self

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


class VerifyCriterion(RecordContract[VerifyCriterionRecord]):
    @classmethod
    @override
    def record_adapter(cls) -> TypeAdapter[VerifyCriterionRecord]:
        return TypeAdapter(VerifyCriterionRecord)

    criterion: TrimmedText = Field(description="One acceptance criterion, as the slice states it.")
    result: VerifyResult = Field(description="passed only on observed proof. Missing proof is unverified, never passed.")
    observation: TrimmedText = Field(description="What you did through the real user path and what you observed.")
    evidence: TrimmedText = Field(description="Where the capture or log is saved in your attempt scratch, or the blocker.")


class VerifyOutput(StageOutput):
    # Acceptance binds it to the exact published head.
    tested_head: TrimmedText = Field(description="The full SHA of the HEAD you drove.")
    criteria: list[VerifyCriterion] = Field(min_length=1, description="Every acceptance criterion with its observed result.")

    @override
    def update(self, context: UpdateContext) -> StageUpdate:
        raise NotImplementedError("verify: use exact-head stage acceptance")


class SpecCheckOutput(HumanQuestionOutput):
    human_question: TrimmedText | None = Field(description=(
        "Ask only when the issue's own text cannot be met by any candidate, citing the issue line. "
        "Leave findings empty and put candidate defects in notes."))
    findings: list[SpecFinding] = Field(description="All blocking gaps a revised candidate could fix.")
    notes: list[SpecFinding] = Field(description="Nonblocking observations, separate from requested fixes.")

    @model_validator(mode="after")
    def question_or_findings(self) -> Self:
        if self.human_question and self.findings:
            raise ValueError("Return either a human question or blocking findings.")
        return self

    @override
    def update(self, context: UpdateContext) -> StageUpdate:
        return {"human_question": self.human_question or ""}


def parse_output(contract: type[Output], raw: object, *, context: ReviewContext | None = None) -> Output:
    try:
        return contract.model_validate(raw, context=context)
    except ValidationError as exc:
        errors = exc.errors(include_input=False)
        if contract is SpecOutput and all(seam_field(error) for error in errors):
            # A slice has no builds or uses seams (SP3). The draft goes back as a plan defect, not to a repair turn.
            fields = ", ".join(".".join(map(str, error["loc"])) for error in errors)
            raise InvalidSlicePlan("seam_field", f"{fields}: a slice has no builds or uses seams.") from exc
        error = errors[0]
        path = ".".join(map(str, error["loc"])) or "$"
        raise InvalidOutput(f"{path}: {error['msg']}") from exc


def seam_field(error: ErrorDetails) -> bool:
    loc = error["loc"]
    return error["type"] == "extra_forbidden" and len(loc) == 3 and loc[0] == "slices" and loc[2] in ("builds", "uses")
