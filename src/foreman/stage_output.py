"""Worker output contracts shared by the CLI prompts and delivery graph."""

from abc import ABC, abstractmethod
from typing import Annotated, Literal, Self, TYPE_CHECKING, TypeVar

from typing_extensions import NotRequired, TypedDict, override

from pydantic import BaseModel, ConfigDict, JsonValue, Field, PositiveInt, StringConstraints, ValidationError, field_validator, model_validator

from foreman.dispatch import InvalidOutput

if TYPE_CHECKING:
    from foreman.delivery import DeliveryState, DeliveryUpdate
    from foreman.workspace import Workspace

PULL_REQUEST_LINES = 400

Text = Annotated[str, StringConstraints(pattern=r"\S")]
TrimmedText = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"\S")]
Output = TypeVar("Output", bound=BaseModel)
WorkKind = Literal["feature", "bug_fix", "refactoring", "perf_issue"]


class SlicePropertyRecord(TypedDict):
    name: str
    constraint: str


class SeamRecord(TypedDict):
    name: str
    exists: bool
    path: str | None


class SliceRecord(TypedDict):
    title: str
    stateful: bool
    acceptance: str
    builds: list[SeamRecord]
    uses: list[SeamRecord]
    screen: str
    properties: list[SlicePropertyRecord]
    depends_on: NotRequired[list[str]]
    blocker_numbers: NotRequired[list[int] | None]


class BrowserProofRecord(TypedDict):
    screen: str | None
    trace: str | None
    passed: bool | None


class ConstraintResultRecord(TypedDict):
    constraint: str
    rejected: bool


class FormalProofRecord(TypedDict):
    ran: bool | None
    constraints: list[ConstraintResultRecord] | None
    typecheck: str | bool | None


class MergeRiskRecord(TypedDict):
    alignment: str
    impact: str
    verification: str
    rollback: str
    error_detection: str
    level: Literal["low", "medium", "high"]


class ToolFactRecord(TypedDict):
    claim: str
    fixture: str | None


class FindingRecord(TypedDict):
    file: str
    line: int
    severity: Literal["blocking", "nit"]
    summary: str


class Contract(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class SliceProperty(Contract):
    name: TrimmedText = Field(description="State the database rule in plain words.")
    constraint: TrimmedText = Field(description="Give the exact PostgreSQL constraint name, such as deals_external_id_key.")

    def record(self) -> SlicePropertyRecord:
        return {"name": self.name, "constraint": self.constraint}


class Seam(Contract):
    name: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$", description="Use the same short identifier in every slice that builds or uses this seam.")
    exists: bool = Field(description="Set true for a seam that already exists in the checkout. Otherwise use false.")
    path: TrimmedText | None = Field(description="For an existing seam, give a workspace-relative file path. Otherwise use null.")

    @model_validator(mode="after")
    def existing_file(self) -> Self:
        if self.exists != (self.path is not None):
            raise ValueError("path is required exactly when exists is true")
        return self

    def record(self) -> SeamRecord:
        return {"name": self.name, "exists": self.exists, "path": self.path}


def _describe_slice(schema: dict[str, JsonValue]) -> None:
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

    def record(self) -> SliceRecord:
        return {
            "title": self.title, "stateful": self.stateful, "acceptance": self.acceptance,
            "builds": [item.record() for item in self.builds], "uses": [item.record() for item in self.uses],
            "screen": self.screen, "properties": [item.record() for item in self.properties],
        }


class StoredSlice(Slice):
    stateful: bool = False
    work_kind: WorkKind | None = Field(default=None, exclude=True)
    builds: list[Seam] = Field(default_factory=lambda: list[Seam]())
    uses: list[Seam] = Field(default_factory=lambda: list[Seam]())
    depends_on: list[TrimmedText] = Field(default_factory=list)
    blocker_numbers: list[PositiveInt] | None = None

    @override
    def record(self) -> SliceRecord:
        record = super().record()
        if "depends_on" in self.model_fields_set:
            record["depends_on"] = self.depends_on
        if "blocker_numbers" in self.model_fields_set:
            record["blocker_numbers"] = self.blocker_numbers
        return record


class HumanPrompt(Contract):
    stage: str
    question: str


class StageOutput(Contract, ABC):
    @abstractmethod
    def update(self, state: "DeliveryState", workspace: "Workspace | None") -> "DeliveryUpdate":
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
    def update(self, state: "DeliveryState", workspace: "Workspace | None" = None) -> "DeliveryUpdate":
        update: DeliveryUpdate = {
            "triage": self.triage, "work_kind": self.work_kind, "human_question": self.human_question or "",
        }
        if state.get("issue_kind") == "slice" and "work_kind" in state:
            update["work_kind"] = state["work_kind"]
        if state.get("issue_kind") == "parent" and self.triage == "implement":
            if self.slice is None:
                raise InvalidOutput("slice: required when a parent triages as implement")
            update["slice"] = self.slice.record()
        return update


class ResearchOutput(StageOutput):
    research: Text

    @override
    def update(self, state: "DeliveryState", workspace: "Workspace | None" = None) -> "DeliveryUpdate":
        return {"research": self.research}


class SpecOutput(HumanQuestionOutput):
    spec: Text
    spec_gate: Literal["ready", "needs_human"]
    slices: list[Slice] | None = Field(
        description="For a ready parent spec, return one entry per pull request. Use null otherwise.",
    )

    @override
    def update(self, state: "DeliveryState", workspace: "Workspace | None" = None) -> "DeliveryUpdate":
        from foreman.delivery import clean_slices, oversized_pr

        slices = (
            clean_slices(self.slices, workspace)
            if state.get("issue_kind") == "parent" and self.spec_gate == "ready" else []
        )
        if oversized_pr(state):
            return {
                "pr_split": {"plan": self.spec, "gate": self.spec_gate, "question": self.human_question, "slices": slices},
                "spec_rejection": None,
            }
        update: DeliveryUpdate = {
            "spec": self.spec, "spec_gate": self.spec_gate,
            "human_question": self.human_question or "", "spec_rejection": None,
        }
        if state.get("issue_kind") == "parent" and self.spec_gate == "ready":
            update["slices"] = slices
        return update


class BrowserProof(Contract):
    """For a required screen, report only browser evidence you actually collected."""

    screen: str | None
    trace: str | None
    passed: bool | None

    def record(self) -> BrowserProofRecord:
        return {"screen": self.screen, "trace": self.trace, "passed": self.passed}


class ConstraintResult(Contract):
    constraint: Text
    rejected: bool

    def record(self) -> ConstraintResultRecord:
        return {"constraint": self.constraint, "rejected": self.rejected}


class FormalProof(Contract):
    """For required properties, report only formal evidence you actually collected."""

    ran: bool | None
    constraints: list[ConstraintResult] | None
    typecheck: str | bool | None

    def record(self) -> FormalProofRecord:
        return {"ran": self.ran, "constraints": [item.record() for item in self.constraints] if self.constraints is not None else None, "typecheck": self.typecheck}


class ImplementOutput(StageOutput):
    implementation: Text
    browser_proof: BrowserProof | None
    formal_proof: FormalProof | None

    @override
    def update(self, state: "DeliveryState", workspace: "Workspace | None" = None) -> "DeliveryUpdate":
        return {
            "implementation": self.implementation,
            "browser_proof": self.browser_proof.record() if self.browser_proof else None,
            "formal_proof": self.formal_proof.record() if self.formal_proof else None,
        }


class MergeRisk(Contract):
    alignment: TrimmedText
    impact: TrimmedText
    verification: TrimmedText
    rollback: TrimmedText
    error_detection: TrimmedText
    level: Literal["low", "medium", "high"]

    def record(self) -> MergeRiskRecord:
        return {"alignment": self.alignment, "impact": self.impact, "verification": self.verification, "rollback": self.rollback, "error_detection": self.error_detection, "level": self.level}


class ToolFact(Contract):
    claim: TrimmedText = Field(description="State the outside-tool behavior the diff relies on.")
    fixture: TrimmedText | None = Field(
        description="Give the exact entry ID in tests/fixtures/manifest.toml. Use null when the claim has no recorded evidence.",
    )

    def record(self) -> ToolFactRecord:
        return {"claim": self.claim, "fixture": self.fixture}


class Finding(Contract):
    file: Text = Field(description="Give the path relative to the repository root.")
    line: int = Field(description="Give the line number in the reviewed commit.")
    severity: Literal["blocking", "nit"]
    summary: Text = Field(max_length=200)

    def record(self) -> FindingRecord:
        return {"file": self.file, "line": self.line, "severity": self.severity, "summary": self.summary}


class ReviewOutput(StageOutput):
    review: Literal["approve", "changes"]
    risk: MergeRisk
    findings: list[Finding] = Field(
        description="List each requested fix, including nits. Return one or more findings for changes and an empty list for approve.",
    )
    tool_facts: list[ToolFact] = Field(
        description="List every outside-tool fact the diff relies on, including unrecorded facts. Use an empty list if there are none.",
    )
    detail: Text = Field(description="Write the pull request review body, including any longer explanation.")

    @model_validator(mode="after")
    def verdict_matches_findings(self) -> Self:
        if (self.review == "changes") != bool(self.findings):
            raise ValueError("changes requires at least one finding and approve requires an empty findings list")
        return self

    @override
    def update(self, state: "DeliveryState", workspace: "Workspace | None" = None) -> "DeliveryUpdate":
        change_kind = None
        if self.review == "changes":
            change_kind = "nits" if all(finding.severity == "nit" for finding in self.findings) else "blocking"
        return {
            "review": self.review, "risk": self.risk.record(), "detail": self.detail,
            "findings": [finding.record() for finding in self.findings],
            "tool_facts": [fact.record() for fact in self.tool_facts], "review_change_kind": change_kind,
        }


class VerifyOutput(StageOutput):
    verify: Literal["pass", "fail"]

    @override
    def update(self, state: "DeliveryState", workspace: "Workspace | None" = None) -> "DeliveryUpdate":
        return {"verify": self.verify}


STAGE_OUTPUTS: dict[str, type[StageOutput]] = {
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
