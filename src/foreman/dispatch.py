"""Try the configured models in order. Quota exhaustion is not a rule change."""

from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol

from foreman.adapters import Outcome, OutcomeKind
from foreman.config import ModelSpec

if TYPE_CHECKING:
    from foreman.delivery import DeliveryState


class QuotaExhausted(Exception):
    def __init__(self, model: str | None):
        self.model = model
        super().__init__(model or "no model")


class InvalidOutput(ValueError):
    pass


class Handler(Protocol):
    def __call__(self, node: str, model: ModelSpec | None, state: "DeliveryState", instruction: str, /) -> Outcome: ...


class FailoverDispatcher:
    def __init__(self, handler: Handler):
        self.handler = handler

    def run(
        self, node: str, models: list[ModelSpec], state: "DeliveryState", instruction: str,
        *, on_attempt: Callable[[str | None, OutcomeKind], None] | None = None,
    ) -> tuple[Outcome, ModelSpec | None]:
        candidates: list[ModelSpec | None] = list(models) or [None]
        quota: tuple[Outcome, ModelSpec | None] | None = None
        missing = Outcome("cli_missing")
        for model in candidates:
            outcome = self.handler(node, model, state, instruction)
            name = model.name if model else None
            if on_attempt is not None:
                on_attempt(name, outcome.kind)
            if outcome.kind == "quota":
                quota = outcome, model
            elif outcome.kind == "cli_missing":
                missing = outcome
            else:
                return outcome, model
        return quota if quota else (missing, None)
