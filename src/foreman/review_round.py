"""Review history, PR scope and graph-check adapter over ResolutionLedger."""

from typing import Literal

from pydantic import Field

from foreman.resolution import ResolutionLedger
from foreman.stage_output import Contract, Finding, ReviewOutput, ResolutionStatus, SettledRuling, StoredToolFact, parse_output
from foreman.workspace import ReviewScope


class GraphCheck(Contract):
    failing_heads: list[str] = Field(default_factory=list[str])
    detail: str
    passed: bool = False
    tool_facts: list[StoredToolFact] = Field(default_factory=list[StoredToolFact])


class ReviewRound(Contract):
    round: int
    reviewed_head: str
    findings: list[Finding]
    resolutions: dict[str, ResolutionStatus]
    graph_check: GraphCheck | None = None
    coordinator_notes: list[Finding] = Field(default_factory=list[Finding])


class ReviewHistory(Contract):
    rounds: list[ReviewRound] = Field(default_factory=list[ReviewRound])
    settled: list[SettledRuling] = Field(default_factory=list[SettledRuling])

    @property
    def gate(self) -> GraphCheck | None:
        return self.rounds[-1].graph_check if self.rounds else None

    def accounting(self) -> ResolutionLedger[Finding]:
        return ResolutionLedger(self.rounds, self.settled)

    def unresolved(self) -> list[Finding]:
        return self.accounting().unresolved()

    def rulings(self) -> list[SettledRuling]:
        return self.accounting().rulings()


class AcceptedReview(Contract):
    history: ReviewHistory
    findings: list[Finding]
    detail: str
    rethink: str = ""

    @property
    def change_kind(self) -> Literal["blocking", "nits"] | None:
        gate = self.history.rounds[-1].graph_check
        if (gate and not gate.passed) or any(f.severity == "blocking" for f in self.findings):
            return "blocking"
        return "nits" if self.findings else None

    @property
    def review(self) -> Literal["approve", "changes"]:
        return "changes" if self.change_kind else "approve"


def accept(past: ReviewHistory, output: ReviewOutput, head: str, gate: GraphCheck | None, scope: ReviewScope | None = None) -> AcceptedReview:
    accounting = past.accounting()
    resolutions = accounting.account(output.findings, output.resolutions)
    findings: list[Finding] = []
    notes: list[Finding] = []
    for finding in output.findings:
        (notes if finding.ruling_id else findings).append(finding)
    old_gate = past.gate
    if old_gate and not old_gate.passed:
        heads = sorted(set(old_gate.failing_heads).union(gate.failing_heads if gate else []))
        gate = GraphCheck(failing_heads=heads, detail=gate.detail if gate else old_gate.detail,
                          passed=(gate is None or gate.passed) and head not in heads,
                          tool_facts=gate.tool_facts if gate else old_gate.tool_facts)
    entry = ReviewRound(round=len(past.rounds) + 1, reviewed_head=head, findings=findings,
                        resolutions=resolutions, coordinator_notes=notes, graph_check=gate)
    history = ReviewHistory(rounds=[*past.rounds, entry], settled=past.settled)
    detail = [output.detail, *(f"Coordinator note {f.id}: {f.file}:{f.line} {f.summary}" for f in notes)]
    if gate and not gate.passed:
        detail.append(gate.detail)
    recurrence = accounting.recurrence(findings)
    rethink = "" if recurrence is None else "Design-level fix required:\n" + "\n".join(f.model_dump_json() for f in recurrence)
    return AcceptedReview(history=history, findings=history.unresolved(), detail="\n\n".join(detail), rethink=rethink)


def history(raw: object) -> ReviewHistory:
    return parse_output(ReviewHistory, raw)
