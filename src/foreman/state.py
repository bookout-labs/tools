"""Delivery graph state channels and their reducers."""

from collections.abc import Mapping
import operator
import os
from pathlib import Path
from typing import Annotated, Literal, NotRequired

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from pydantic import BaseModel, PlainValidator
from typing_extensions import TypedDict

from foreman.adapters import Session
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
    # The commit of Foreman's code that started the run, saved at its first start and never replaced. Empty: that code ran no commit.
    release: str = ""

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
    check_exit_code: int | None = None
    verify: Literal["", "pass", "fail"] = ""
    failure_output: str = ""
    checked_tree: str = ""
    verified_head: str = ""

    def for_attempt(self) -> "ChecksState":
        return ChecksState(check=self.check, verify_skill=self.verify_skill, verify_failures=self.verify_failures,
                           verify="fail" if self.verify == "fail" else "",
                           check_exit_code=self.check_exit_code if self.verify == "fail" else None,
                           failure_output=self.failure_output if self.verify == "fail" else "")


SCHEMA_VERSION = 33


def checkpoint_serializer() -> JsonPlusSerializer:
    return JsonPlusSerializer(allowed_msgpack_modules=[Halt, MergeState, WorkState, BaseMerge, RelatedCheckout, RunState, StartSettings, ChecksState, PlanState, ReviewState, VerifyState])


class CheckpointVersionError(ValueError):
    """The issue's saved run was written under another delivery state schema, which this release cannot read."""

    def __init__(self, version: object, thread: str):
        found = "missing" if version is None else repr(version)
        release = "an older" if version is None or (type(version) is int and version < SCHEMA_VERSION) else "another"
        super().__init__(
            f"The run saved for {thread} was written by {release} Foreman release (schema_version {found}; "
            f"this release reads {SCHEMA_VERSION}). This release cannot read, wake or resume it, and it started nothing. "
            "To continue that run, use the release that saved it. "
            "This release cannot start a new run for this issue while that record is in its store. "
            "The record is left untouched, never deleted or migrated."
        )


def readable(values: Mapping[str, object]) -> bool:
    """Whether this release can read a saved record, by its schema_version alone."""
    version = values.get("schema_version")
    return type(version) is int and version == SCHEMA_VERSION


def require_schema_version(values: Mapping[str, object], thread: str) -> None:
    if not readable(values):
        raise CheckpointVersionError(values.get("schema_version"), thread)


def state_home() -> Path:
    """The host's XDG state home. Foreman keeps its runs, checkouts and checkpoint store in its foreman directory there."""
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")


def checkpoint_store() -> Path:
    """The store every run on this host saves in by default, the receiver's runs included."""
    return state_home() / "foreman" / "checkpoints.sqlite3"


def extend_attempts(current: list[AttemptRecord], update: list[AttemptRecord]) -> list[AttemptRecord]:
    """Append attempts. A nested stage returns the attempts it was given, then its own."""
    if current and update[:len(current)] == current:
        update = update[len(current):]
    return current + update


class BuilderSession(TypedDict):
    """The builder's one conversation for the PR (D5): the session its latest accepted answer returned, and that checkout."""
    model: str
    session: Session
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
    # Issues the run returns to admission as OWNER/NAME#N: open dependents after its merge, or the children it filed.
    released: list[str]
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
    """What a stage and its steps hold. The run's start is not among it: steps read the login and caps from the run context."""
    review: ReviewState
    plan: PlanState
    checks: ChecksState
    run: RunState
    stop: NotRequired[str]
    path: Annotated[list[str], operator.add]


class CutOffCall(TypedDict):
    """The report facts of the call a stop cut off after it was sent, from its saved reservation. Only the report reads them."""
    stage: str
    model: str
    methods: list[str]


def latest(current: StartSettings, update: StartSettings) -> StartSettings:
    """Each start supersedes the one before, however many reach one saved record."""
    return update


class DeliveryGraphState(DeliveryState):
    """The delivery graph's state. Its start is the one owner of the run's login and caps."""
    start: Annotated[StartSettings, latest]
    cut_off: NotRequired[CutOffCall]


class DeliveryGraphUpdate(DeliveryUpdate, total=False):
    start: StartSettings
    cut_off: CutOffCall


class DeliveryResult(DeliveryFields):
    review: ReviewState
    plan: PlanState
    checks: ChecksState
    run: RunState
    # A refused new run keeps nothing, so its result has no start.
    start: NotRequired[StartSettings]
    cut_off: NotRequired[CutOffCall]
    path: Annotated[list[str], operator.add]
    stop: str


def pull_request_number(state: DeliveryState) -> int | None:
    """The number of the pull request the run's review dispatched on, which is not the issue's number."""
    target = (state.get("dispatch_plan") or {}).get("target")
    return target["number"] if target else None
