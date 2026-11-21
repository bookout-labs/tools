"""Immutable facts about the two committed trees in a pull request review."""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True)
class ReviewSide:
    sha: str
    files: frozenset[str]
    lines: Mapping[str, int]
    changed: frozenset[str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "lines", MappingProxyType(dict(self.lines)))


@dataclass(frozen=True)
class ReviewContext:
    base: ReviewSide
    head: ReviewSide


SCRIPTED_REVIEW_CONTEXT = ReviewContext(
    base=ReviewSide(
        sha="a" * 40,
        files=frozenset({"README.md", "items.txt", "deleted.txt"}),
        lines={"README.md": 1, "items.txt": 3, "deleted.txt": 1},
        changed=frozenset({"items.txt", "deleted.txt"}),
    ),
    head=ReviewSide(
        sha="b" * 40,
        files=frozenset({"README.md", "items.txt"}),
        lines={"README.md": 1, "items.txt": 3},
        changed=frozenset({"items.txt"}),
    ),
)
