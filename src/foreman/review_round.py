"""Current-head review reports and settled rulings."""

from pydantic import Field

from foreman.contracts import InvalidOutput
from foreman.stage_output import BlockingSeverity, Contract, Finding, ReviewOutput, ReviewSelection, SettledRuling, parse_output


class ReviewRound(Contract):
    reviewed_head: str
    base: str
    findings: list[Finding]
    provenance: ReviewSelection | None = None

    @property
    def severity(self) -> BlockingSeverity | None:
        priorities = {finding.blocking_severity for finding in self.findings}
        return "P0" if "P0" in priorities else "P1" if "P1" in priorities else None


class ReviewHistory(Contract):
    rounds: list[ReviewRound] = Field(default_factory=list[ReviewRound])
    settled: list[SettledRuling] = Field(default_factory=list[SettledRuling])


def accept(past: ReviewHistory, output: ReviewOutput, head: str, base: str) -> ReviewRound:
    ids = {finding.id for finding in output.findings}
    if len(ids) != len(output.findings):
        raise InvalidOutput("findings: IDs must be unique within a report")
    rulings = {ruling.id for ruling in past.settled}
    if any(finding.ruling_id is not None and finding.ruling_id not in rulings for finding in output.findings):
        raise InvalidOutput("findings.ruling_id: unknown settled ruling")
    return ReviewRound(reviewed_head=head, base=base, findings=list(output.findings))


def history(raw: object) -> ReviewHistory:
    return parse_output(ReviewHistory, raw)
