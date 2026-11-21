"""Typed GitHub policy and commit evidence for a single PR observation."""

from __future__ import annotations

from typing import Literal
from pydantic import BaseModel as PydanticModel, Field, RootModel, AliasChoices


MergeMethod = Literal["MERGE", "REBASE", "SQUASH"]
PullRequestState = Literal["OPEN", "CLOSED", "MERGED"]
MergeQueueEntryState = Literal["QUEUED", "AWAITING_CHECKS", "MERGEABLE", "UNMERGEABLE", "LOCKED"]


Outcome = Literal["ready", "conflict", "pending", "red", "policy", "head_changed", "closed", "retargeted", "queue_rejected"]


class StrictModel(PydanticModel, strict=True):
    pass


class Ref(StrictModel):
    sha: str
    ref: str


class PullSnapshot(StrictModel):
    number: int
    state: str
    draft: bool
    merged: bool = False
    head: Ref
    base: Ref
    mergeable: bool | None
    merge_commit_sha: str | None
    mergeable_state: str


class Requirement(StrictModel):
    context: str
    integration_id: int | None = Field(default=None, validation_alias=AliasChoices("integration_id", "app_id"))


class Parameters(StrictModel):
    required_status_checks: list[Requirement] = Field(default_factory=list[Requirement])
    merge_method: MergeMethod | None = None
    grouping_strategy: str | None = None


class QueuePolicy(StrictModel):
    merge_method: MergeMethod
    grouping_strategy: str | None = None


class AutoMergeRequest(StrictModel):
    mergeMethod: MergeMethod
    enabledAt: str | None = None


class Rule(StrictModel):
    type: str
    parameters: Parameters = Field(default_factory=Parameters)


class App(StrictModel):
    id: int


class CheckSuite(StrictModel):
    id: int


class Check(StrictModel):
    id: int
    name: str
    head_sha: str
    app: App
    check_suite: CheckSuite
    status: str
    conclusion: str | None


class CheckPage(StrictModel):
    check_runs: list[Check]


class Status(StrictModel):
    id: int
    context: str
    state: str


class Commit(StrictModel):
    parents: list[CommitParent]


class CommitParent(StrictModel):
    sha: str


class CheckEvidence(StrictModel):
    context: str
    app: int | None
    run: int | None
    source_commit: str
    suite: int | None = None
    tested_commit: str | None = None
    state: str


class Observation(StrictModel):
    repo: str
    pr: PullSnapshot
    checks: list[CheckEvidence]
    outcome: Outcome
    recovery: str
    queue_policy: QueuePolicy | None = None

    def detail(self) -> str:
        checks = ", ".join(f"{c.context}={c.state}" for c in self.checks) or "no check evidence"
        return f"PR #{self.pr.number} head={self.pr.head.sha} base={self.pr.base.ref}@{self.pr.base.sha}: {self.outcome}; {checks}. {self.recovery}"


class Rules(RootModel[list[Rule]]):
    pass


class StatusPages(RootModel[list[list[Status]]]):
    pass


class CheckPages(RootModel[list[CheckPage]]):
    pass


class LegacyChecks(StrictModel):
    checks: list[Requirement]
    contexts: list[str]


class Protection(StrictModel):
    required_status_checks: LegacyChecks


class BranchTip(StrictModel):
    sha: str


class BranchPolicy(StrictModel):
    commit: BranchTip
    protection: Protection


class QueueEntry(StrictModel):
    id: str
    state: MergeQueueEntryState


class QueueSnapshot(StrictModel):
    number: int
    state: PullRequestState
    headRefOid: str
    baseRefOid: str
    baseRefName: str
    isDraft: bool
    autoMergeRequest: AutoMergeRequest | None
    mergeQueueEntry: QueueEntry | None


class MergeReceipt(StrictModel):
    repo: str
    pr: int
    source_head: str
    base: str
    base_ref: str
    method: MergeMethod
    queue_policy: QueuePolicy
    identity: str
    status: Literal[MergeQueueEntryState, "AUTO_MERGE_ENABLED", "MERGED"]
