"""Try the configured models in order. Quota exhaustion is not a rule change."""

from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from foreman.delivery import DeliveryState


class QuotaExhausted(Exception):
    def __init__(self, model: str | None):
        self.model = model
        super().__init__(model or "no model")


class InvalidOutput(ValueError):
    pass


class Handler(Protocol):
    def __call__(self, node: str, model: str | None, state: "DeliveryState", instruction: str) -> dict[str, object]:
        pass


class FailoverDispatcher:
    def __init__(self, handler: Handler):
        self.handler = handler

    def run(
        self, node: str, models: list[str], state: "DeliveryState", instruction: str,
        *, on_attempt: Callable[[str | None], None] | None = None,
    ) -> tuple[dict[str, object], str | None]:
        candidates: list[str | None] = list(models) or [None]
        last: QuotaExhausted | None = None
        for model in candidates:
            if on_attempt is not None:
                on_attempt(model)
            try:
                update = self.handler(node, model, state, instruction)
            except QuotaExhausted as exc:
                last = exc
                continue
            if not isinstance(update, dict):
                raise InvalidOutput(f"{node} returned {type(update).__name__}")
            return update, model
        raise last or QuotaExhausted(None)
