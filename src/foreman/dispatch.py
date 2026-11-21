"""Model handler and dispatch errors."""

from dataclasses import replace
from typing import TYPE_CHECKING, Protocol

from foreman._raw import is_dict, is_list
from foreman.adapters import Outcome
from foreman.config import ModelSpec
from foreman.redaction import redact

if TYPE_CHECKING:
    from foreman.state import DeliveryState


class QuotaExhausted(Exception):
    def __init__(self, model: str | None):
        self.model = model
        super().__init__(model or "no model")


class InvalidOutput(ValueError):
    pass


class Handler(Protocol):
    def __call__(self, node: str, model: ModelSpec | None, state: "DeliveryState", instruction: str, /) -> Outcome: ...


def clean_outcome(outcome: Outcome) -> Outcome:
    def clean(value: object) -> object:
        if isinstance(value, str):
            return redact(value, credentials_only=True)
        if is_dict(value):
            return {key: clean(item) for key, item in value.items()}
        if is_list(value):
            return [clean(item) for item in value]
        return value
    return replace(outcome, output={key: clean(value) for key, value in outcome.output.items()},
                   detail=redact(outcome.detail, credentials_only=True))
