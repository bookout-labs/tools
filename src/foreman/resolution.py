"""Stable finding IDs, resolutions, settled rulings and recurrence."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Generic, Literal, Protocol, TypeVar

from foreman.dispatch import InvalidOutput
from foreman.stage_output import FindingResolution, SettledRuling


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
Status = Literal["resolved", "unresolved"]


@dataclass(frozen=True)
class ResolutionLedger(Generic[F]):
    rounds: Sequence[tuple[Sequence[F], Mapping[str, Status]]]
    settled: Sequence[SettledRuling]

    def unresolved(self) -> list[F]:
        findings = {f.id: f for findings, _ in self.rounds for f in findings}
        for _, resolutions in self.rounds:
            for finding_id, status in resolutions.items():
                if status == "resolved":
                    findings.pop(finding_id, None)
        return list(findings.values())

    def rulings(self) -> list[SettledRuling]:
        unresolved = {f.id for f in self.unresolved()}
        return [*self.settled, *(SettledRuling(id=f"finding:{f.id}", text=f.summary)
                               for findings, _ in self.rounds for f in findings if f.id not in unresolved)]

    def account(self, findings: Sequence[F], entries: Sequence[FindingResolution]) -> dict[str, Status]:
        earlier = {f.id for f in self.unresolved()}
        resolutions: dict[str, Status] = {r.id: r.status for r in entries}
        if len(resolutions) != len(entries) or resolutions.keys() != earlier:
            raise InvalidOutput("resolutions: mark every earlier unresolved finding exactly once")
        previous = {f.id for findings, _ in self.rounds for f in findings}
        ids = [f.id for f in findings]
        if len(set(ids)) != len(ids) or previous.intersection(ids):
            raise InvalidOutput("findings: IDs must be unique across rounds; use resolutions for earlier findings")
        return resolutions

    def check_ruling(self, finding: F) -> None:
        if finding.ruling_id is not None and finding.ruling_id not in {r.id for r in self.rulings()}:
            raise InvalidOutput("findings.ruling_id: unknown settled ruling")

    def recurrence(self, findings: Sequence[F]) -> tuple[F, F] | None:
        return next(((old, new) for earlier, _ in self.rounds for old in earlier for new in findings
                     if old.severity == new.severity == "blocking" and old.kind == new.kind), None)
