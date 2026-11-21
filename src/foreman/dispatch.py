"""Try the configured models in order. Quota exhaustion is not a rule change."""

from collections.abc import Callable
from dataclasses import replace
from typing import TYPE_CHECKING, Protocol

from foreman._raw import is_dict, is_list
from foreman.adapters import Outcome
from foreman.config import ModelSpec
from foreman.redaction import redact

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
        self, node: str, model: ModelSpec | None, state: "DeliveryState", instruction: str,
        *, on_attempt: Callable[[str | None, Outcome], None] | None = None,
    ) -> tuple[Outcome, ModelSpec | None]:
        outcome = self.handler(node, model, state, instruction)
        def clean(value: object) -> object:
            if isinstance(value, str):
                return redact(value, credentials_only=True)
            if is_dict(value):
                return {key: clean(item) for key, item in value.items()}
            if is_list(value):
                return [clean(item) for item in value]
            return value
        outcome = replace(outcome, output={key: clean(value) for key, value in outcome.output.items()},
                          detail=redact(outcome.detail, credentials_only=True))
        name = model.name if model else None
        if on_attempt is not None:
            on_attempt(name, outcome)
        return outcome, model
