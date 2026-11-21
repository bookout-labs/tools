"""Pure candidate-bound spec acceptance over the shared resolution ledger."""

from typing import Literal, Self

from pydantic import Field, PositiveInt, field_validator, model_validator

from foreman.dispatch import InvalidOutput
from foreman.resolution import ResolutionLedger
from foreman.stage_output import (
    Contract, ResolutionStatus, SettledRuling, SpecCandidate, SpecCandidateRecord,
    SpecCheckOutput, SpecFinding,
)


class SpecInterdiff(Contract):
    slices: list[str]
    sections: list[Literal["slice", "spec", "work_kind"]]


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

    def interdiff(self, index: int = -1) -> SpecInterdiff:
        """Derive scope for a round by list index, defaulting to the latest round."""
        index = range(len(self.rounds))[index]
        candidate = SpecCandidate.from_record(self.rounds[index].candidate)
        previous = SpecCandidate.from_record(self.rounds[index - 1].candidate) if index else None
        old_slices = {s.title: (index, s) for index, s in enumerate(previous.slices)} if previous else {}
        new_slices = {s.title: (index, s) for index, s in enumerate(candidate.slices)}
        changed = sorted(title for title in old_slices.keys() | new_slices.keys() if old_slices.get(title) != new_slices.get(title))
        sections: list[Literal["slice", "spec", "work_kind"]] = ["slice"] if changed else []
        if previous is None or candidate.spec != previous.spec:
            sections.append("spec")
        if previous is None or candidate.work_kind != previous.work_kind:
            sections.append("work_kind")
        return SpecInterdiff(slices=changed, sections=sections)

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

    @model_validator(mode="after")
    def valid_approval(self) -> Self:
        expected = None
        if self.history.rounds and self.verdict == "clear":
            expected = SpecApproval(revision=self.history.rounds[-1].candidate["revision"], round=len(self.history.rounds))
        if self.approval != expected:
            raise ValueError("approval must match the accepted history")
        return self


def accept(past: SpecHistory, output: SpecCheckOutput, candidate: SpecCandidate, *, max_rounds: int) -> AcceptedSpecCheck:
    if max_rounds < 1 or len(past.rounds) >= max_rounds:
        raise InvalidOutput("spec_check: round cap reached")
    candidate = SpecCandidate.from_record(candidate.record())
    slices = {s.title: s for s in candidate.slices}
    accounting = past.accounting()
    new = [*output.findings, *output.notes]
    reserved = {note.id for entry in past.rounds for note in entry.notes}
    if reserved.intersection(f.id for f in new):
        raise InvalidOutput("findings: IDs must be unique across rounds, including notes")
    resolutions = accounting.account(new, output.resolutions)
    for finding in accounting.unresolved():
        if resolutions[finding.id] == "unresolved" and finding.slice not in slices:
            raise InvalidOutput("resolutions: unresolved finding targets unknown candidate slice")
    findings: list[SpecFinding] = []
    notes = list(output.notes)
    for finding in [*output.findings, *output.notes]:
        if finding.slice not in slices:
            raise InvalidOutput("findings.slice: unknown candidate slice")
    for finding in output.findings:
        if finding.ruling_id is not None:
            notes.append(finding)
            continue
        findings.append(finding)
    entry = SpecRound(candidate=candidate.record(),
                      findings=findings, notes=notes, resolutions=resolutions)
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
