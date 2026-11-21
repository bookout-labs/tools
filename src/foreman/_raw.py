"""Read-only views of containers whose contents have yet to be validated."""

from collections.abc import Iterable, Mapping, Sequence
from typing import TypeGuard


def is_dict(value: object) -> TypeGuard[Mapping[object, object]]:
    return isinstance(value, dict)


def is_list(value: object) -> TypeGuard[Sequence[object]]:
    return isinstance(value, list)


def sorted_keys(keys: Iterable[object]) -> list[object]:
    """Preserve native comparisons between keys, even before validation."""
    return sorted(keys, key=lambda key: (key,))
