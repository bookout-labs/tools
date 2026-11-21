"""Read-only views of containers whose contents have yet to be validated."""

from collections.abc import Mapping, Sequence
from typing import TypeGuard


def is_dict(value: object) -> TypeGuard[Mapping[object, object]]:
    return isinstance(value, dict)


def is_list(value: object) -> TypeGuard[Sequence[object]]:
    return isinstance(value, list)
