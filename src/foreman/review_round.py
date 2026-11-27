"""Current-head review reports and settled rulings."""

from typing import get_args

from pydantic import Field

from foreman.contracts import InvalidOutput
from foreman.stage_output import (BlockingSeverity, Contract, Finding, ReviewOutput, ReviewPass, ReviewSelection, SettledRuling,
                                  parse_output)

PASSES: tuple[ReviewPass, ...] = get_args(ReviewPass)
# Each pass answers one fixed question in a fresh session (item 17). Pass (a) reads the issue text, not the plan.
QUESTIONS: dict[ReviewPass, str] = {
    "a": "Answer pass (a) only: against the issue text and not the plan, does the change deliver the issue, and only the issue?",
    "b": "Answer pass (b) only: what does the change break beyond the diff, in callers, data and security?",
    "c": "Answer pass (c) only: is it a fix or a patch? Does it remove the cause, or add a guard, retry, wrapper or flag the "
         "plan did not name, duplicate existing code or ignore the repository's pattern?",
}


class ReviewRound(Contract):
    reviewed_head: str
    base: str
    findings: list[Finding]
    passes: list[ReviewPass] = Field(default_factory=list[ReviewPass])
    provenance: dict[ReviewPass, ReviewSelection] = Field(default_factory=dict[ReviewPass, ReviewSelection])

    @property
    def complete(self) -> bool:
        return len(self.passes) == len(PASSES)

    @property
    def severity(self) -> BlockingSeverity | None:
        priorities = {finding.blocking_severity for finding in self.findings}
        return "P0" if "P0" in priorities else "P1" if "P1" in priorities else None


class ReviewHistory(Contract):
    rounds: list[ReviewRound] = Field(default_factory=list[ReviewRound])
    settled: list[SettledRuling] = Field(default_factory=list[SettledRuling])

    @property
    def blocking(self) -> int:
        """The completed blocking rounds, which review compares with max_review_rounds."""
        return sum(round.severity is not None for round in self.rounds)

    def open_round(self, head: str, base: str) -> ReviewRound | None:
        """The round still answering its passes for this head and base, if any."""
        last = self.rounds[-1] if self.rounds else None
        return last if last and not last.complete and (last.reviewed_head, last.base) == (head, base) else None

    def next_pass(self, head: str, base: str) -> ReviewPass:
        current = self.open_round(head, base)
        return PASSES[len(current.passes)] if current else PASSES[0]


def accept(past: ReviewHistory, output: ReviewOutput, head: str, base: str) -> tuple[ReviewRound, bool]:
    """The round with this pass's findings, and whether it replaces the last round, which is unfinished and so never posted.

    A pass that asks a question records its findings but not the pass, so the answer resumes the same pass. A round for
    another head or base replaces an unfinished one."""
    ids = {finding.id for finding in output.findings}
    if len(ids) != len(output.findings):
        raise InvalidOutput("findings: IDs must be unique within a report")
    rulings = {ruling.id for ruling in past.settled}
    if any(finding.ruling_id is not None and finding.ruling_id not in rulings for finding in output.findings):
        raise InvalidOutput("findings.ruling_id: unknown settled ruling")
    current = past.open_round(head, base)
    review_pass = past.next_pass(head, base)
    kept = [finding for finding in current.findings if finding.review_pass != review_pass] if current else []
    found = [Finding.model_validate({**finding.model_dump(), "review_pass": review_pass}) for finding in output.findings]
    passes: list[ReviewPass] = [*(current.passes if current else []), *([] if output.human_question else [review_pass])]
    provenance = current.provenance if current else {}
    unfinished = bool(past.rounds) and not past.rounds[-1].complete
    return ReviewRound(reviewed_head=head, base=base, findings=[*kept, *found], passes=passes, provenance=dict(provenance)), unfinished


def history(raw: object) -> ReviewHistory:
    return parse_output(ReviewHistory, raw)
