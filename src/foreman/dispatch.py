"""Model handler and dispatch errors."""

from typing import TYPE_CHECKING, Protocol

from foreman.adapters import Outcome
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
