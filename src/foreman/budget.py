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
        if "dollar_charge" in attempt:
            spent += Decimal(attempt["dollar_charge"])
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
        if "work_charge" in attempt:
            total += attempt["work_charge"]
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
                        sum("work_charge" in a or work_tokens([a]) is None for a in charged), buckets, len(charged))


def dollar_detail(limit: str | None, attempts: Sequence[AttemptRecord]) -> str:
    spend = claude_spend(attempts)
    detail = f"Run Claude spend: ${spend}." if spend is not None else "Run Claude spend: unknown."
    return detail + (f" Dollar allowance: ${limit}." if limit is not None else "")


def budget_stop(cap: int, limit: str | None, attempts: Sequence[AttemptRecord]) -> str | None:
    """Why no allowance remains for another paid attempt, or None while one does."""
    last = attempts[-1] if attempts else None
    note = (last.get("detail") or "") if last else ""
    charged = f" {note}" if note.startswith("usage_unknown") else ""
    if (work := work_tokens(attempts)) is None:
        return "usage_unknown: A launched attempt has unknown work usage; the work cap cannot be enforced."
    if work >= cap and last is not None:
        return (f"Run used {work} work tokens; cap is {cap}. Last attempt: {last['stage']} {last['model']} used "
                f"{work_tokens([last])} work tokens. Measured work overshoot: {work - cap}. "
                f"An in-flight attempt can overshoot the cap.{charged}")
    match dollar_admission(BudgetUsd(Decimal(limit)), attempts) if limit is not None else None:
        case CostUnknown():
            return ("usage_unknown: A Claude attempt ran without a dollar cost. The allowance cannot be enforced. "
                    + dollar_detail(limit, attempts))
        case Exhausted():
            return dollar_detail(limit, attempts) + charged
        case _:
            return None
