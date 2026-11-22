"""Pure work-token and Claude dollar accounting over a run's attempt history."""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

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
        native = attempt.get("native_usage")
        if (usage is None or usage["cache_creation"] is None
                or (native is not None and any(row["cache_creation_input_tokens"] is None
                                               for row in native["entries"].values()))):
            return None
        total += usage["input"] + usage["cache_creation"] + usage["output"]
    return total
