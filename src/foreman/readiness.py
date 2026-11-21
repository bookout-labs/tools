"""Typed GitHub policy and commit evidence for a single PR observation."""

from __future__ import annotations

from typing import Literal
from pydantic import BaseModel as PydanticModel, Field, RootModel, AliasChoices


class BaseModel(PydanticModel, strict=True):
    pass


class Ref(BaseModel):
    sha: str
    ref: str


class Readiness(BaseModel):
    number: int
    state: str
    draft: bool
    head: Ref
    base: Ref
    mergeable: bool | None
    merge_commit_sha: str | None
    mergeable_state: str


class Requirement(BaseModel):
    context: str
    integration_id: int | None = Field(default=None, validation_alias=AliasChoices("integration_id", "app_id"))


class Parameters(BaseModel):
    required_status_checks: list[Requirement] = Field(default_factory=list[Requirement])
    merge_method: str | None = None


class Rule(BaseModel):
    type: str
    parameters: Parameters = Field(default_factory=Parameters)


class App(BaseModel):
    id: int


class Check(BaseModel):
    id: int
    name: str
    head_sha: str
    app: App
    status: str
    conclusion: str | None


class CheckPage(BaseModel):
    check_runs: list[Check]


class Status(BaseModel):
    id: int
    context: str
    state: str


class Commit(BaseModel):
    parents: list[CommitParent]


class CommitParent(BaseModel):
    sha: str


class CheckEvidence(BaseModel):
    context: str
    app: int | None
    run: int | None
    tested_commit: str
    state: str


class Observation(BaseModel):
    repo: str
    pr: Readiness
    checks: list[CheckEvidence]
    outcome: Literal['ready', 'conflict', 'pending', 'red', 'policy']
    recovery: str


class Rules(RootModel[list[Rule]]):
    pass


class StatusPages(RootModel[list[list[Status]]]):
    pass


class CheckPages(RootModel[list[CheckPage]]):
    pass


class LegacyChecks(BaseModel):
    checks: list[Requirement]
    contexts: list[str]


class Protection(BaseModel):
    required_status_checks: LegacyChecks


class BranchPolicy(BaseModel):
    protection: Protection
