"""Delivery graph state channels and their reducers."""

from collections.abc import Mapping
import operator
from pathlib import Path
from typing import Annotated, Literal, NotRequired

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from pydantic import BaseModel, Field, PlainValidator
from typing_extensions import TypedDict

from foreman.outcomes import Halt
from foreman.readiness import MergeState
from foreman.skill_rules import SkillFailureReason
from foreman.stage_output import (
    AttemptRecord, BrowserProofRecord, DispatchPlanRecord,
    FormalProofRecord, PendingDispatchRecord,
    PlanState as PlanState, ReviewState as ReviewState,
)
from foreman.workspace import PullRequestSize, WorkState


class RunState(BaseModel, frozen=True, extra="forbid"):
    repo: str = ""
    issue: int | None = None
    request: str = ""
    workspace: str = ""
    token_cap: int | None = None
    max_budget_usd: str | None = None


class ChecksState(BaseModel, frozen=True, extra="forbid"):
    check: str = ""
    preflight: str = ""
    check_exit_code: int | None = None
    verify: Literal["", "pass", "fail"] = ""
    verified_head: str = ""
    browser_proof: BrowserProofRecord | None = None
    formal_proof: FormalProofRecord | None = None
    proof_labels: dict[str, str] = Field(default_factory=dict)


SCHEMA_VERSION = 8


def checkpoint_serializer() -> JsonPlusSerializer:
    return JsonPlusSerializer(allowed_msgpack_modules=[Halt, MergeState, WorkState, RunState, ChecksState, PlanState, ReviewState])


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


class DeliveryFields(TypedDict, total=False):
    schema_version: int
    pending_dispatch: PendingDispatchRecord | None
    dispatch_plan: DispatchPlanRecord
    pstack_pin: dict[str, str]
    skill_reason: SkillFailureReason
    attempts: Annotated[list[AttemptRecord], operator.add]
    research: str
    work: WorkState
    implementation: str
    merge: MergeState
    human_question: str
    human_answers: list[str]
    pr_size: PullRequestSize
    pr_size_override: PullRequestSize
    review_rounds: int
    verify_failures: int
    proof_failures: int
    spec_failures: int
    models_used: dict[str, str]
    implement_models: list[str]
    watch: WatchRecord | None
    halt: Annotated[Halt, PlainValidator(lambda code: Halt(code).value)] | None
    halt_detail: str


class DeliveryUpdate(DeliveryFields, total=False):
    review: ReviewState
    plan: PlanState
    checks: ChecksState
    run: RunState
    stop: str
    path: Annotated[list[str], operator.add]


class DeliveryState(DeliveryFields):
    review: ReviewState
    plan: PlanState
    checks: ChecksState
    run: RunState
    stop: NotRequired[str]
    path: Annotated[list[str], operator.add]


class DeliveryResult(DeliveryFields):
    review: ReviewState
    plan: PlanState
    checks: ChecksState
    run: RunState
    path: Annotated[list[str], operator.add]
    stop: str
