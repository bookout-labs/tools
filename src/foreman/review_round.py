"""Review history, resolution accounting and convergence rules."""

from typing import Literal

from pydantic import Field
from unidiff.errors import UnidiffParseError
from unidiff.patch import PatchSet

from foreman.dispatch import InvalidOutput
from foreman.stage_output import Contract, Finding, ReviewOutput, SettledRuling, ToolFact, FindingResolution, parse_output


class ReviewRound(Contract):
    round: int
    reviewed_head: str
    findings: list[Finding]
    resolutions: dict[str, Literal["resolved", "unresolved"]]
    tool_fact_gate: str | None = None
    tool_facts: list[ToolFact] = Field(default_factory=list)
    coordinator_notes: list[Finding] = Field(default_factory=list)


class ReviewHistory(Contract):
    rounds: list[ReviewRound] = Field(default_factory=list)
    settled: list[SettledRuling] = Field(default_factory=list)

    def unresolved(self) -> list[Finding]:
        findings = {f.id: f for r in self.rounds for f in r.findings}
        for round in self.rounds:
            for id, status in round.resolutions.items():
                if status == "resolved":
                    findings.pop(id, None)
        return list(findings.values())

    def accept(self, output: ReviewOutput, head: str, patch: str | None, *, tool_fact_gate: Finding | None = None) -> tuple[list[Finding], str]:
        gate_ids = {r.tool_fact_gate for r in self.rounds if r.tool_fact_gate}
        earlier_gates = {f.id: f for f in self.unresolved() if f.id in gate_ids}
        earlier = {f.id: f for f in self.unresolved() if f.id not in gate_ids}
        resolutions: dict[str, Literal["resolved", "unresolved"]] = {r.id: r.status for r in output.resolutions}
        if len(resolutions) != len(output.resolutions) or resolutions.keys() != earlier.keys():
            raise InvalidOutput("resolutions: mark every earlier unresolved finding exactly once")
        previous = {f.id: f for r in self.rounds for f in r.findings}
        ids = [f.id for f in output.findings]
        if len(set(ids)) != len(ids) or previous.keys() & set(ids) or any(id.startswith("tool-fact-gate:") for id in ids):
            raise InvalidOutput("findings: IDs must be unique across rounds; use resolutions for earlier findings")
        settled = {r.id for r in self.settled} | {f"finding:{id}" for id in previous.keys() - earlier.keys() - gate_ids}
        try:
            changed = changed_lines(patch) if patch is not None else None
        except UnidiffParseError as exc:
            raise InvalidOutput("review interdiff could not be parsed") from exc
        accepted = []
        notes = []
        excluded = []
        for finding in output.findings:
            if finding.ruling_id is not None and finding.ruling_id not in settled:
                raise InvalidOutput("findings.ruling_id: unknown settled ruling")
            if finding.ruling_id or (self.rounds and changed is not None and (finding.file, finding.line) not in changed):
                excluded.append(finding)
                notes.append(f"Coordinator note {finding.id}: {finding.file}:{finding.line} {finding.summary}")
            else:
                accepted.append(finding)
        recurrence = next(((old, new) for old in previous.values() for new in accepted
                           if old.id not in gate_ids and old.severity == new.severity == "blocking" and old.kind == new.kind), None)
        active_gate = next(iter(earlier_gates), None)
        if active_gate:
            introduced = next(r for r in self.rounds if any(f.id == active_gate for f in r.findings))
            resolutions[active_gate] = "resolved" if tool_fact_gate is None and head != introduced.reviewed_head else "unresolved"
        elif tool_fact_gate is not None:
            active_gate = f"tool-fact-gate:{len(self.rounds) + 1}"
            accepted.append(tool_fact_gate.model_copy(update={"id": active_gate}))
        output.resolutions = [FindingResolution(id=id, status=status) for id, status in resolutions.items()]
        self.rounds.append(ReviewRound(round=len(self.rounds) + 1, reviewed_head=head,
                                      findings=accepted, resolutions=resolutions, coordinator_notes=excluded,
                                      tool_fact_gate=active_gate, tool_facts=output.tool_facts))
        output.findings = accepted
        output.detail = "\n\n".join([output.detail, *notes])
        detail = "" if recurrence is None else "Design-level fix required:\n" + "\n".join(f.model_dump_json() for f in recurrence)
        return self.unresolved(), detail

    def rulings(self) -> list[SettledRuling]:
        unresolved = {f.id for f in self.unresolved()} | {r.tool_fact_gate for r in self.rounds}
        return [*self.settled, *(SettledRuling(id=f"finding:{f.id}", text=f.summary)
                               for r in self.rounds for f in r.findings if f.id not in unresolved)]


def changed_lines(patch: str) -> set[tuple[str, int | None]]:
    return {(file.path, line.target_line_no) for file in PatchSet(patch) if not file.is_removed_file
            for hunk in file for line in hunk if line.is_added}


def history(raw: object) -> ReviewHistory:
    return parse_output(ReviewHistory, raw)
