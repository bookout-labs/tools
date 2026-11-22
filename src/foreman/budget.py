"""Pure work-token and Claude dollar accounting over a run's attempt history."""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from foreman.stage_output import AttemptRecord
from foreman.usd import BudgetUsd


@dataclass(frozen=True)
class Remaining:
    usd: Decimal


@dataclass(frozen=True)
class Exhausted:
    pass


@dataclass(frozen=True)
class CostUnknown:
    pass


def claude_spend(attempts: Sequence[AttemptRecord]) -> Decimal | None:
    spent = Decimal(0)
    for attempt in attempts:
        if not (attempt["model"] or "").startswith("claude-") or attempt["outcome"] == "cli_missing":
            continue
        usage = attempt["usage"]
        if usage is None or usage["cost_usd"] is None:
            return None
        spent += Decimal(repr(usage["cost_usd"]))
    return spent


def dollar_admission(limit: BudgetUsd, attempts: Sequence[AttemptRecord]) -> Remaining | Exhausted | CostUnknown:
    spent = claude_spend(attempts)
    if spent is None:
        return CostUnknown()
    remaining = limit.amount - spent
    return Remaining(remaining) if remaining > 0 else Exhausted()


def work_tokens(attempts: Sequence[AttemptRecord]) -> int | None:
    """Charge canonical fresh input, cache creation and output once."""
    total = 0
    for attempt in attempts:
        if attempt["outcome"] == "cli_missing":
            continue
        usage = attempt["usage"]
        if usage is None or usage["cache_creation"] is None:
            return None
        total += usage["input"] + usage["cache_creation"] + usage["output"]
    return total


@dataclass(frozen=True)
class TokenSummary:
    known: int
    unknown: int
    buckets: dict[str, tuple[int, int]]
    charged: int


def token_summary(attempts: Sequence[AttemptRecord]) -> TokenSummary:
    charged = [a for a in attempts if a["outcome"] != "cli_missing"]
    buckets: dict[str, tuple[int, int]] = {}
    bucket: Literal["input", "output", "cache_read", "cache_creation"]
    for bucket in ("input", "output", "cache_read", "cache_creation"):
        values = [value for a in charged if (u := a["usage"]) is not None and (value := u[bucket]) is not None]
        buckets[bucket] = (sum(values), len(values))
    return TokenSummary(sum(buckets[b][0] for b in ("input", "output", "cache_creation")),
                        sum(work_tokens([a]) is None for a in charged), buckets, len(charged))
