"""Try the configured models in order. Quota exhaustion is not a rule change."""

from typing import Protocol


class QuotaExhausted(Exception):
    def __init__(self, model: str | None):
        self.model = model
        super().__init__(model or "no model")


class InvalidOutput(ValueError):
    pass


class Handler(Protocol):
    def __call__(self, node: str, model: str | None, state: dict, instruction: str) -> dict:
        pass


class FailoverDispatcher:
    def __init__(self, handler: Handler):
        self.handler = handler

    def run(self, node: str, models: list[str], state: dict, instruction: str) -> tuple[dict, str | None]:
        candidates: list[str | None] = list(models) or [None]
        last: QuotaExhausted | None = None
        for model in candidates:
            try:
                update = self.handler(node, model, state, instruction)
            except QuotaExhausted as exc:
                last = exc
                continue
            if not isinstance(update, dict):
                raise InvalidOutput(f"{node} returned {type(update).__name__}")
            return update, model
        raise last or QuotaExhausted(None)
