"""Dollar amounts at CLI and usage boundaries."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Annotated

from pydantic import Field

CostUsd = Annotated[float, Field(ge=0, allow_inf_nan=False)]


@dataclass(frozen=True)
class BudgetUsd:
    amount: Decimal

    def __post_init__(self) -> None:
        if not self.amount.is_finite() or self.amount <= 0:
            raise ValueError("dollar budget must be finite and positive")


def usd_text(value: BudgetUsd) -> str:
    text = format(value.amount, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text
