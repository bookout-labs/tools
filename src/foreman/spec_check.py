"""Pure candidate-bound spec acceptance: each round checks one exact candidate."""

from typing import Literal, Self

from pydantic import Field, PositiveInt, model_validator

from foreman.contracts import InvalidOutput
from foreman.stage_output import (
    Contract, PlanState, SettledRuling, SpecCandidate, SpecRound as SpecRound, SpecCheckOutput, SpecFinding,
)


class SpecHistory(Contract):
    rounds: list[SpecRound] = Field(default_factory=list[SpecRound])
    settled: list[SettledRuling] = Field(default_factory=list[SettledRuling])

    def clears(self, revision: str) -> bool:
        """Whether the latest check of this revision found no blocking gap."""
        checked = [entry for entry in self.rounds if entry.candidate["revision"] == revision]
        return bool(checked) and not checked[-1].findings

    @classmethod
    def cleared(cls, plan: PlanState) -> bool:
        """Whether the checker's saved verdict clears the plan's current candidate."""
        return plan.spec_candidate is not None and cls.model_validate(plan.spec_history).clears(plan.spec_candidate["revision"])


class SpecApproval(Contract):
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    round: PositiveInt

    def matches(self, candidate: SpecCandidate) -> bool:
        return self.revision == candidate.revision


class AcceptedSpecCheck(Contract):
    history: SpecHistory
    approval: SpecApproval | None

    @property
    def findings(self) -> list[SpecFinding]:
        return self.history.rounds[-1].findings if self.history.rounds else []

    @property
    def verdict(self) -> Literal["clear", "gaps"]:
        return "gaps" if self.findings else "clear"

    @model_validator(mode="after")
    def valid_approval(self) -> Self:
        expected = None
        if self.history.rounds and self.verdict == "clear":
            expected = SpecApproval(revision=self.history.rounds[-1].candidate["revision"], round=len(self.history.rounds))
        if self.approval != expected:
            raise ValueError("approval must match the accepted history")
        return self


def accept(past: SpecHistory, output: SpecCheckOutput, candidate: SpecCandidate) -> AcceptedSpecCheck:
    """Record one check of the exact candidate. Earlier rounds constrain nothing but settled rulings."""
    candidate = SpecCandidate.from_record(candidate.record())
    slices = {s.title for s in candidate.slices}
    reported = [*output.findings, *output.notes]
    if len({finding.id for finding in reported}) != len(reported):
        raise InvalidOutput("findings: IDs must be unique within a round, including notes")
    rulings = {ruling.id for ruling in past.settled}
    for finding in reported:
        if finding.slice not in slices:
            raise InvalidOutput("findings.slice: unknown candidate slice")
        if finding.ruling_id is not None and finding.ruling_id not in rulings:
            raise InvalidOutput("findings.ruling_id: unknown settled ruling")
    findings = [finding for finding in output.findings if finding.ruling_id is None]
    notes = [*output.notes, *(finding for finding in output.findings if finding.ruling_id is not None)]
    entry = SpecRound(candidate=candidate.record(), findings=findings, notes=notes)
    history = SpecHistory(rounds=[*past.rounds, entry], settled=past.settled)
    approval = None if findings else SpecApproval(revision=candidate.revision, round=len(history.rounds))
    return AcceptedSpecCheck(history=history, approval=approval)
