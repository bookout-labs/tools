"""Typed delivery-facing subset of the pinned LangGraph 1.2.24 functional API."""
from collections.abc import Callable
from concurrent.futures import Future
from typing import Generic, ParamSpec, TypeVar

P = ParamSpec("P")
T = TypeVar("T")

class _TaskFunction(Generic[P, T]):
    def __call__(self, *args: P.args, **kwargs: P.kwargs) -> Future[T]: ...

def task(func: Callable[P, T], /) -> _TaskFunction[P, T]: ...
