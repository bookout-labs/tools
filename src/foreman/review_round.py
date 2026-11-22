"""Review history, PR scope and graph-check adapter over ResolutionLedger."""

from typing import Literal, TYPE_CHECKING
from tomllib import TOMLDecodeError

from pydantic import Field, ValidationError

from foreman.fixture_manifest import recorded_evidence
from foreman.workspace import Workspace, WorktreeInspectionFailed
from foreman.resolution import ResolutionLedger
from foreman.stage_output import Contract, Finding, ReviewOutput, ReviewSelection, ResolutionStatus, SettledRuling, StoredToolFact, GraphCheck as GraphEvidence, parse_output

if TYPE_CHECKING:
    from foreman.state import DeliveryState


class GraphCheck(Contract):
    failing_heads: list[str] = Field(default_factory=list[str])
    detail: str
    passed: bool = False
    tool_facts: list[StoredToolFact] = Field(default_factory=list[StoredToolFact])


class ReviewRound(Contract):
    round: int
    reviewed_head: str
    provenance: ReviewSelection | None = None
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


def accept(past: ReviewHistory, output: ReviewOutput, head: str, gate: GraphCheck | None) -> AcceptedReview:
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
    detail = [output.risk.render(), *(f"Coordinator note {f.id}: {f.file}:{f.line} {f.summary}" for f in notes)]
    if gate and not gate.passed:
        detail.append(gate.detail)
    recurrence = accounting.recurrence(findings)
    rethink = "" if recurrence is None else "Design-level fix required:\n" + "\n".join(f.model_dump_json() for f in recurrence)
    return AcceptedReview(history=history, findings=history.unresolved(), detail="\n\n".join(detail), rethink=rethink)


def history(raw: object) -> ReviewHistory:
    return parse_output(ReviewHistory, raw)


def graph_check(state: "DeliveryState") -> GraphEvidence | None:
    checks = state["checks"]
    head, command, code = checks.verified_head, checks.check, checks.check_exit_code
    if head and command and code == 0 and checks.verify == "pass":
        return GraphEvidence(head=head, command=command, exit_code=code)
    return None


def carried_tool_facts(state: "DeliveryState") -> list[StoredToolFact]:
    review_state = state["review"]
    gate = history({"rounds": review_state.review_findings}).gate
    return gate.tool_facts if gate else []


def check_tool_facts(
    output: ReviewOutput, workspace: Workspace, state: "DeliveryState",
) -> GraphCheck | None:
    review_state = state["review"]
    head = state["checks"].verified_head
    manifest_path = review_state.fact_record
    facts = {fact.claim: fact for fact in carried_tool_facts(state)}
    for fact in output.tool_facts:
        facts.setdefault(fact.claim, StoredToolFact.from_fact(fact))
    proven = graph_check(state)
    outside = [fact for fact in facts.values() if not (
        proven is not None and fact.graph_check == proven and fact.claim == proven.claim
    )]
    recorded: dict[str, list[str]] = {}
    notes: list[str] = []
    if head and outside and manifest_path is not None:
        try:
            recorded = recorded_evidence(workspace, head, manifest_path)
        except (TOMLDecodeError, ValidationError, WorktreeInspectionFailed) as exc:
            notes.append(f"Could not check {manifest_path} at {head}. {exc}")
    claim_ids = {claim_id for ids in recorded.values() for claim_id in ids}
    unrecorded = [fact for fact in outside if fact.fixture not in recorded and fact.claim_id not in claim_ids]
    if not outside:
        return None
    if unrecorded and manifest_path is None:
        notes.append("No fact record was declared.")
    for fact in unrecorded:
        citation = f" (fixture {fact.fixture})" if fact.fixture is not None else ""
        notes.append(f"Unrecorded outside-tool fact {fact.claim_id}{citation}: {fact.claim}")
    return GraphCheck(failing_heads=[head or ""] if unrecorded else [],
                      detail="\n".join(notes), passed=not unrecorded, tool_facts=outside)
