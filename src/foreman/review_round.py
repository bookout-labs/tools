"""Review history and pure resolution accounting."""

from typing import Literal

from pydantic import Field

from foreman.dispatch import InvalidOutput
from foreman.stage_output import Contract, Finding, ReviewOutput, SettledRuling, ToolFact, parse_output


class GraphCheck(Contract):
    failing_heads: list[str] = Field(default_factory=list[str])
    detail: str
    passed: bool = False


class ReviewRound(Contract):
    round: int
    reviewed_head: str
    findings: list[Finding]
    resolutions: dict[str, Literal["resolved", "unresolved"]]
    graph_check: GraphCheck | None = None
    tool_facts: list[ToolFact] = Field(default_factory=list[ToolFact])
    coordinator_notes: list[Finding] = Field(default_factory=list[Finding])


class ReviewHistory(Contract):
    rounds: list[ReviewRound] = Field(default_factory=list[ReviewRound])
    settled: list[SettledRuling] = Field(default_factory=list[SettledRuling])

    def unresolved(self) -> list[Finding]:
        findings = {f.id: f for r in self.rounds for f in r.findings}
        for entry in self.rounds:
            for finding_id, status in entry.resolutions.items():
                if status == "resolved":
                    findings.pop(finding_id, None)
        return list(findings.values())

    def rulings(self) -> list[SettledRuling]:
        unresolved = {f.id for f in self.unresolved()}
        return [*self.settled, *(SettledRuling(id=f"finding:{f.id}", text=f.summary)
                               for r in self.rounds for f in r.findings if f.id not in unresolved)]


class AcceptedReview(Contract):
    history: ReviewHistory
    findings: list[Finding]
    detail: str

    @property
    def change_kind(self) -> Literal["blocking", "nits"] | None:
        gate = self.history.rounds[-1].graph_check
        if (gate and not gate.passed) or any(f.severity == "blocking" for f in self.findings):
            return "blocking"
        return "nits" if self.findings else None

    @property
    def review(self) -> Literal["approve", "changes"]:
        return "changes" if self.change_kind else "approve"


def accept(past: ReviewHistory, output: ReviewOutput, head: str, gate: GraphCheck | None) -> AcceptedReview:
    earlier = {f.id for f in past.unresolved()}
    resolutions: dict[str, Literal["resolved", "unresolved"]] = {r.id: r.status for r in output.resolutions}
    if len(resolutions) != len(output.resolutions) or resolutions.keys() != earlier:
        raise InvalidOutput("resolutions: mark every earlier unresolved finding exactly once")
    previous = {f.id for r in past.rounds for f in r.findings}
    ids = [f.id for f in output.findings]
    if len(set(ids)) != len(ids) or previous.intersection(ids):
        raise InvalidOutput("findings: IDs must be unique across rounds; use resolutions for earlier findings")
    settled = {r.id for r in past.rulings()}
    findings: list[Finding] = []
    notes: list[Finding] = []
    for finding in output.findings:
        if finding.ruling_id is not None and finding.ruling_id not in settled:
            raise InvalidOutput("findings.ruling_id: unknown settled ruling")
        (notes if finding.ruling_id else findings).append(finding)
    old_gate = past.rounds[-1].graph_check if past.rounds else None
    if old_gate and not old_gate.passed:
        heads = sorted(set(old_gate.failing_heads).union(gate.failing_heads if gate else []))
        gate = GraphCheck(failing_heads=heads, detail=gate.detail if gate else old_gate.detail,
                          passed=gate is None and head not in heads)
    entry = ReviewRound(round=len(past.rounds) + 1, reviewed_head=head, findings=findings,
                        resolutions=resolutions, coordinator_notes=notes, graph_check=gate, tool_facts=output.tool_facts)
    history = ReviewHistory(rounds=[*past.rounds, entry], settled=past.settled)
    detail = [output.detail, *(f"Coordinator note {f.id}: {f.file}:{f.line} {f.summary}" for f in notes)]
    if gate and not gate.passed:
        detail.append(gate.detail)
    return AcceptedReview(history=history, findings=history.unresolved(), detail="\n\n".join(detail))


def history(raw: object) -> ReviewHistory:
    return parse_output(ReviewHistory, raw)
