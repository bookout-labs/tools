"""Pure candidate-bound spec acceptance over the shared resolution ledger."""

from typing import Literal

from pydantic import Field, PositiveInt, field_validator

from foreman.dispatch import InvalidOutput
from foreman.resolution import ResolutionLedger
from foreman.stage_output import (
    Contract, ResolutionStatus, SettledRuling, SpecCandidate, SpecCandidateRecord,
    SpecCheckOutput, SpecFinding,
)


class SpecRound(Contract):
    candidate: SpecCandidateRecord
    findings: list[SpecFinding]
    notes: list[SpecFinding]
    resolutions: dict[str, ResolutionStatus]

    @field_validator("candidate")
    @classmethod
    def valid_candidate(cls, value: SpecCandidateRecord) -> SpecCandidateRecord:
        return SpecCandidate.from_record(value).record()


class SpecHistory(Contract):
    rounds: list[SpecRound] = Field(default_factory=list[SpecRound])
    settled: list[SettledRuling] = Field(default_factory=list[SettledRuling])

    def accounting(self) -> ResolutionLedger[SpecFinding]:
        return ResolutionLedger(self.rounds, self.settled)


class SpecApproval(Contract):
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    round: PositiveInt

    def matches(self, candidate: SpecCandidate) -> bool:
        return self.revision == candidate.revision


class AcceptedSpecCheck(Contract):
    history: SpecHistory
    approval: SpecApproval | None
    unsettled: str

    @property
    def verdict(self) -> Literal["clear", "gaps"]:
        return "gaps" if self.history.accounting().unresolved() else "clear"


def accept(past: SpecHistory, output: SpecCheckOutput, candidate: SpecCandidate, *, max_rounds: int) -> AcceptedSpecCheck:
    if max_rounds < 1 or len(past.rounds) >= max_rounds:
        raise InvalidOutput("spec_check: round cap reached")
    candidate = SpecCandidate.from_record(candidate.record())
    slices = {s.title: s for s in candidate.slices}
    if len(slices) != len(candidate.slices):
        raise InvalidOutput("candidate.slices: titles must be unique")
    accounting = past.accounting()
    resolutions = accounting.account([*output.findings, *output.notes], output.resolutions)
    previous = SpecCandidate.from_record(past.rounds[-1].candidate) if past.rounds else None
    old_slices = {s.title: (index, s) for index, s in enumerate(previous.slices)} if previous else {}
    changed = {s.title for index, s in enumerate(candidate.slices) if old_slices.get(s.title) != (index, s)}
    findings: list[SpecFinding] = []
    notes = list(output.notes)
    for finding in [*output.findings, *output.notes]:
        if finding.slice not in slices:
            raise InvalidOutput("findings.slice: unknown candidate slice")
    for finding in output.findings:
        if finding.ruling_id is not None:
            notes.append(finding)
            continue
        eligible = (previous is None or
                    (finding.section == "slice" and finding.slice in changed) or
                    (finding.section == "spec" and candidate.spec != previous.spec) or
                    (finding.section == "work_kind" and candidate.work_kind != previous.work_kind))
        if not eligible:
            raise InvalidOutput("findings: new blocker outside changed candidate sections")
        findings.append(finding)
    entry = SpecRound(candidate=candidate.record(), findings=findings, notes=notes, resolutions=resolutions)
    history = SpecHistory(rounds=[*past.rounds, entry], settled=past.settled)
    unresolved = history.accounting().unresolved()
    recurrence = accounting.recurrence(findings)
    unsettled = ""
    if recurrence:
        unsettled = f"Recurring spec kind {recurrence[1].kind}: {recurrence[0].id}, {recurrence[1].id}"
    elif unresolved and len(history.rounds) >= max_rounds:
        unsettled = "Unresolved spec findings at round cap: " + ", ".join(f.id for f in unresolved)
    approval = None if unresolved else SpecApproval(revision=candidate.revision, round=len(history.rounds))
    return AcceptedSpecCheck(history=history, approval=approval, unsettled=unsettled)
