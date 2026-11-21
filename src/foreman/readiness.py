"""Typed GitHub policy and commit evidence for a single PR observation."""

from __future__ import annotations

from typing import Literal
from pydantic import BaseModel as PydanticModel, Field, RootModel, AliasChoices


Outcome = Literal["ready", "conflict", "pending", "red", "policy", "head_changed", "closed", "retargeted"]


class StrictModel(PydanticModel, strict=True):
    pass


class Ref(StrictModel):
    sha: str
    ref: str


class PullSnapshot(StrictModel):
    number: int
    state: str
    draft: bool
    head: Ref
    base: Ref
    mergeable: bool | None
    merge_commit_sha: str | None
    mergeable_state: str


class Requirement(StrictModel):
    context: str
    integration_id: int | None = Field(default=None, validation_alias=AliasChoices("integration_id", "app_id"))


class Parameters(StrictModel, extra="allow"):
    required_status_checks: list[Requirement] = Field(default_factory=list[Requirement])
    merge_method: Literal["MERGE", "REBASE", "SQUASH"] | None = None


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


class QueueCommit(StrictModel):
    oid: str


class QueueEntry(StrictModel):
    id: str
    state: str
    headCommit: QueueCommit


class AutoRequest(StrictModel):
    enabledAt: str
    mergeMethod: Literal["MERGE", "REBASE", "SQUASH"]


class QueueSnapshot(StrictModel):
    number: int
    state: str
    headRefOid: str
    baseRefOid: str
    baseRefName: str
    isDraft: bool
    autoMergeRequest: AutoRequest | None
    mergeQueueEntry: QueueEntry | None


class MergeReceipt(StrictModel):
    repo: str
    pr: int
    source_head: str
    base: str
    method: Literal["MERGE", "REBASE", "SQUASH"]
    queue_policy: Parameters
    identity: str
    status: str
