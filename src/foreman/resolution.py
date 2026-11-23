"""Stable finding IDs, resolutions, settled rulings and recurrence."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar

from foreman.contracts import InvalidOutput
from foreman.stage_output import FindingResolution, ResolutionStatus, SettledRuling


class AccountedFinding(Protocol):
    @property
    def id(self) -> str: ...
    @property
    def summary(self) -> str: ...
    @property
    def ruling_id(self) -> str | None: ...
    @property
    def severity(self) -> str: ...
    @property
    def kind(self) -> str: ...


F = TypeVar("F", bound=AccountedFinding)
F_co = TypeVar("F_co", bound=AccountedFinding, covariant=True)


class AccountedRound(Protocol[F_co]):
    @property
    def findings(self) -> Sequence[F_co]: ...
    @property
    def resolutions(self) -> Mapping[str, ResolutionStatus]: ...


@dataclass(frozen=True)
class ResolutionLedger(Generic[F]):
    rounds: Sequence[AccountedRound[F]]
    settled: Sequence[SettledRuling]

    def unresolved(self) -> list[F]:
        findings = {f.id: f for round in self.rounds for f in round.findings}
        for round in self.rounds:
            for finding_id, status in round.resolutions.items():
                if status == "resolved":
                    findings.pop(finding_id, None)
        return list(findings.values())

    def rulings(self) -> list[SettledRuling]:
        unresolved = {f.id for f in self.unresolved()}
        return [*self.settled, *(SettledRuling(id=f"finding:{f.id}", text=f.summary)
                               for round in self.rounds for f in round.findings if f.id not in unresolved)]

    def account(self, findings: Sequence[F], entries: Sequence[FindingResolution]) -> dict[str, ResolutionStatus]:
        earlier = {f.id for f in self.unresolved()}
        resolutions: dict[str, ResolutionStatus] = {r.id: r.status for r in entries}
        if len(resolutions) != len(entries) or resolutions.keys() != earlier:
            raise InvalidOutput("resolutions: mark every earlier unresolved finding exactly once")
        previous = {f.id for round in self.rounds for f in round.findings}
        ids = [f.id for f in findings]
        if len(set(ids)) != len(ids) or previous.intersection(ids):
            raise InvalidOutput("findings: IDs must be unique across rounds; use resolutions for earlier findings")
        rulings = {r.id for r in self.rulings()}
        for finding in findings:
            if finding.ruling_id is not None and finding.ruling_id not in rulings:
                raise InvalidOutput("findings.ruling_id: unknown settled ruling")
        return resolutions

    def recurrence(self, findings: Sequence[F]) -> tuple[F, F] | None:
        return next(((old, new) for round in self.rounds for old in round.findings for new in findings
                     if old.severity == new.severity == "blocking" and old.kind == new.kind), None)
