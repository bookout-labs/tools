"""Delivery graph state channels and their reducers."""

from collections.abc import Mapping
import operator
from pathlib import Path
from typing import Annotated, Literal, NotRequired

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from pydantic import PlainValidator
from typing_extensions import TypedDict

from foreman.github import ReviewReceipt
from foreman.outcomes import Halt
from foreman.readiness import MergeState
from foreman.skill_rules import SkillFailureReason
from foreman.stage_output import (
    AttemptRecord, BrowserProofRecord, DispatchPlanRecord, FindingRecord,
    FormalProofRecord, MergeRiskRecord, PendingDispatchRecord, PullRequestSplit,
    SliceRecord, SpecCandidateRecord, WorkKind,
)
from foreman.workspace import ImplementationCandidate, PullRequestSize


SCHEMA_VERSION = 2


def checkpoint_serializer() -> JsonPlusSerializer:
    return JsonPlusSerializer(allowed_msgpack_modules=[Halt, MergeState])


class CheckpointVersionError(ValueError):
    """The checkpoint belongs to an incompatible delivery state schema."""

    def __init__(self, version: object, checkpoint: Path | None = None):
        location = f" {checkpoint}" if checkpoint is not None else ""
        rendered_version = "missing" if version is None else repr(version)
        super().__init__(
            f"Unsupported checkpoint{location} schema_version {rendered_version}; expected {SCHEMA_VERSION}. "
            "Start a new run with a new checkpoint path."
        )


def require_schema_version(values: Mapping[str, object], checkpoint: Path | None = None) -> None:
    version = values.get("schema_version")
    if type(version) is not int or version != SCHEMA_VERSION:
        raise CheckpointVersionError(version, checkpoint)


class WatchRecord(TypedDict):
    issue: int
    action: Literal["filed", "commented"]


SpecRejectionKind = Literal["duplicate_title", "duplicate_builder", "dangling_seam", "dependency_cycle", "existing_seam_builder"]


class SpecRejection(TypedDict):
    kind: SpecRejectionKind
    detail: str


class DeliveryFields(TypedDict, total=False):
    schema_version: int
    max_budget_usd: str | None
    pending_dispatch: PendingDispatchRecord | None
    dispatch_plan: DispatchPlanRecord
    request: str
    issue: int
    workspace: str
    preflight: str
    check_exit_code: int
    triage: str
    spec: str
    spec_gate: str
    spec_candidate: SpecCandidateRecord
    spec_rejection: SpecRejection | None
    slices: list[SliceRecord]
    child_issue_numbers: list[int]
    slice: SliceRecord
    issue_kind: str
    work_kind: WorkKind
    pstack_pin: dict[str, str]
    skill_reason: SkillFailureReason
    attempts: Annotated[list[AttemptRecord], operator.add]
    research: str
    implementation: str
    delivery_branch: str
    delivery_base: str
    attempt_head: str
    implementation_candidate: ImplementationCandidate | None
    partial_edits: str
    head_sha: str | None
    verified_head: str
    repair_head: str | None
    merge: MergeState
    browser_proof: BrowserProofRecord | None
    formal_proof: FormalProofRecord | None
    proof_labels: dict[str, str]
    human_question: str
    human_answers: list[str]
    pr_size: PullRequestSize
    pr_size_override: PullRequestSize
    pr_split: PullRequestSplit
    review: str
    review_findings: list[dict[str, object]]
    reviewed_head: str
    settled_rulings: list[dict[str, object]]
    fact_record: str
    findings: list[FindingRecord]
    detail: str
    review_change_kind: Literal["blocking", "nits"] | None
    risk: MergeRiskRecord
    verify: str
    review_rounds: int
    verify_failures: int
    proof_failures: int
    spec_failures: int
    review_run_id: str
    review_receipt: ReviewReceipt | None
    models_used: dict[str, str]
    implement_models: list[str]
    watch: WatchRecord | None
    halt: Annotated[Halt, PlainValidator(lambda code: Halt(code).value)] | None
    halt_detail: str


class DeliveryUpdate(DeliveryFields, total=False):
    token_cap: int
    stop: str
    repo: str
    check: str
    path: Annotated[list[str], operator.add]


class DeliveryState(DeliveryFields):
    token_cap: int
    stop: NotRequired[str]
    repo: str
    check: str
    path: Annotated[list[str], operator.add]


class DeliveryResult(DeliveryFields):
    token_cap: NotRequired[int]
    repo: str
    check: str
    path: Annotated[list[str], operator.add]
    stop: str
