"""Try the configured models in order. Quota exhaustion is not a rule change."""

from typing import TYPE_CHECKING, Protocol

from foreman.adapters import Outcome

if TYPE_CHECKING:
    from foreman.delivery import DeliveryState


class QuotaExhausted(Exception):
    def __init__(self, model: str | None):
        self.model = model
        super().__init__(model or "no model")


class CliMissing(Exception):
    pass


class InvalidOutput(ValueError):
    pass


class Handler(Protocol):
    def __call__(self, node: str, model: str | None, state: "DeliveryState", instruction: str) -> dict[str, object] | Outcome:
        pass


class FailoverDispatcher:
    def __init__(self, handler: Handler):
        self.handler = handler

    def run(
        self, node: str, models: list[str], state: "DeliveryState", instruction: str,
    ) -> tuple[dict[str, object] | Outcome, str | None]:
        candidates: list[str | None] = list(models) or [None]
        last: QuotaExhausted | None = None
        missing: CliMissing | None = None
        for model in candidates:
            try:
                update = self.handler(node, model, state, instruction)
            except QuotaExhausted as exc:
                last = exc
                continue
            if isinstance(update, Outcome):
                if update.kind == "failed":
                    raise InvalidOutput(f"{model}: {update.detail}")
                if update.kind == "quota":
                    last = QuotaExhausted(model)
                    continue
                if update.kind == "cli_missing":
                    missing = CliMissing(update.detail)
                    continue
                return update, model
            if not isinstance(update, dict):
                raise InvalidOutput(f"{node} returned {type(update).__name__}")
            return update, model
        raise last or missing or QuotaExhausted(None)
