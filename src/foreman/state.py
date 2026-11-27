"""Delivery graph state channels and their reducers."""

from collections.abc import Mapping
import operator
from pathlib import Path
from typing import Annotated, Literal, NotRequired

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from pydantic import BaseModel, PlainValidator
from typing_extensions import TypedDict

from foreman.outcomes import Halt
from foreman.readiness import MergeState
from foreman.skill_rules import SkillFailureReason
from foreman.stage_output import (
    AttemptRecord, DispatchPlanRecord, SpecTarget,
    PlanState as PlanState, ReviewState as ReviewState, VerifyState as VerifyState,
)
from foreman.workspace import BaseMerge, PullRequestSize, RelatedCheckout, WorkState as WorkState


class RunState(BaseModel, frozen=True, extra="forbid"):
    repo: str = ""
    issue: int | None = None
    request: str = ""
    workspace: str = ""
    base_branch: str = ""
    base_sha: str = ""

    def target(self) -> SpecTarget:
        """The admitted repository, issue and pinned base that every candidate binds."""
        assert self.issue is not None
        return {"repo": self.repo.lower(), "issue": self.issue, "base": self.base_sha}


class StartSettings(BaseModel, frozen=True, extra="forbid"):
    """The human login and budget caps the run's last start read from the repository's variables.

    Answers and stops count only from this login, and dispatch's allowance is these caps minus what the run has spent.
    """

    human_login: str
    max_tokens: int
    max_budget_usd: str


class ChecksState(BaseModel, frozen=True, extra="forbid"):
    # Failed checks cannot be recovered after attempt observations are reset.
    verify_failures: int = 0
    check: str = ""
    # The contract's verify skill path, read at the pinned base.
    verify_skill: str = ""
    preflight: str = ""
    check_exit_code: int | None = None
    verify: Literal["", "pass", "fail"] = ""
    failure_output: str = ""
    checked_tree: str = ""
    verified_head: str = ""

    def for_attempt(self) -> "ChecksState":
        return ChecksState(check=self.check, verify_skill=self.verify_skill, preflight=self.preflight,
                           verify_failures=self.verify_failures,
                           verify="fail" if self.verify == "fail" else "",
                           check_exit_code=self.check_exit_code if self.verify == "fail" else None,
                           failure_output=self.failure_output if self.verify == "fail" else "")


SCHEMA_VERSION = 27


def checkpoint_serializer() -> JsonPlusSerializer:
    return JsonPlusSerializer(allowed_msgpack_modules=[Halt, MergeState, WorkState, BaseMerge, RelatedCheckout, RunState, StartSettings, ChecksState, PlanState, ReviewState, VerifyState])


class CheckpointVersionError(ValueError):
    """The checkpoint belongs to an incompatible delivery state schema."""

    def __init__(self, version: object, checkpoint: Path | None = None, thread: str | None = None):
        location = f" {checkpoint}" if checkpoint is not None else ""
        rendered_version = "missing" if version is None else repr(version)
        issue = f" for issue {thread}" if thread is not None else ""
        release = "an older" if version is None or (type(version) is int and version < SCHEMA_VERSION) else "an incompatible"
        super().__init__(
            f"Unsupported checkpoint{location}{issue} schema_version {rendered_version}; expected {SCHEMA_VERSION}. "
            f"This checkpoint was written by {release} release and cannot be resumed. "
            "Start a new run for this issue by rerunning without --answer and with XDG_STATE_HOME set to a new directory. "
            "The existing checkpoint file is left untouched, never deleted or migrated."
        )


def require_schema_version(values: Mapping[str, object], checkpoint: Path | None = None, thread: str | None = None) -> None:
    version = values.get("schema_version")
    if type(version) is not int or version != SCHEMA_VERSION:
        raise CheckpointVersionError(version, checkpoint, thread)


class WatchRecord(TypedDict):
    issue: int
    action: Literal["filed", "commented"]


def extend_attempts(current: list[AttemptRecord], update: list[AttemptRecord]) -> list[AttemptRecord]:
    """Append attempts. A nested stage returns the attempts it was given, then its own."""
    if current and update[:len(current)] == current:
        update = update[len(current):]
    return current + update


class BuilderSession(TypedDict):
    """The builder's one conversation for the PR (D5): the session its latest accepted answer returned, and that checkout."""
    model: str
    session: str
    fingerprint: str


class DeliveryFields(TypedDict, total=False):
    schema_version: int
    dispatch_plan: DispatchPlanRecord
    pstack_pin: dict[str, str]
    matt_skills_pin: dict[str, str]
    # The related repositories admission pinned. Every worker reads their checkouts; only the instructions' pins differ by procedure.
    related: list[RelatedCheckout]
    skill_reason: SkillFailureReason
    attempts: Annotated[list[AttemptRecord], extend_attempts]
    # The drawn steps the run has started, as stage.step in order. The run report names them and the last one.
    steps: list[str]
    research: str
    work: WorkState
    implementation: str
    merge: MergeState
    human_question: str
    human_answers: list[str]
    pr_size: PullRequestSize
    models_used: dict[str, str]
    implement_models: list[str]
    builder_session: BuilderSession | None
    verify: VerifyState
    watch: WatchRecord | None
    # Issues the run returns to admission as OWNER/NAME#N: open dependents after its merge, or the children it filed.
    released: list[str]
    halt: Annotated[Halt, PlainValidator(lambda code: Halt(code).value)] | None
    halt_detail: str


class DeliveryUpdate(DeliveryFields, total=False):
    review: ReviewState
    plan: PlanState
    checks: ChecksState
    run: RunState
    start: StartSettings
    stop: str
    path: Annotated[list[str], operator.add]


class DeliveryState(DeliveryFields):
    review: ReviewState
    plan: PlanState
    checks: ChecksState
    run: RunState
    start: StartSettings
    stop: NotRequired[str]
    path: Annotated[list[str], operator.add]


class DeliveryResult(DeliveryFields):
    review: ReviewState
    plan: PlanState
    checks: ChecksState
    run: RunState
    # A refused new run keeps nothing, so its result has no start.
    start: NotRequired[StartSettings]
    path: Annotated[list[str], operator.add]
    stop: str


def pull_request_number(state: DeliveryState) -> int | None:
    """The number of the pull request the run's review dispatched on, which is not the issue's number."""
    target = (state.get("dispatch_plan") or {}).get("target")
    return target["number"] if target else None
