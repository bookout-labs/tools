"""Immutable worker capabilities and their checkpoint representation."""

from dataclasses import dataclass
from pathlib import Path
import shlex
from typing import Literal

from pydantic import ConfigDict, TypeAdapter, with_config
from typing_extensions import TypedDict


@dataclass(frozen=True)
class CheckCommand:
    argv: tuple[str, ...]

    @property
    def command(self) -> str:
        return shlex.join(self.argv)


class UngrantableCheck(ValueError):
    """The check is a shell program rather than a sequence of commands."""


def parse_check(check: str) -> tuple[CheckCommand, ...]:
    if any(char in check for char in "\n\r\x00$`;|<>()"):
        raise UngrantableCheck("Checks cannot contain shell operators, expansion, substitution, or newlines")
    if not check.strip():
        return ()
    commands: list[CheckCommand] = []
    for segment in check.split("&&"):
        if "&" in segment:
            raise UngrantableCheck("Only && may join check commands")
        try:
            argv = tuple(shlex.split(segment))
        except ValueError as exc:
            raise UngrantableCheck(str(exc)) from exc
        if not argv or not argv[0]:
            raise UngrantableCheck("Expected a nonempty command")
        commands.append(CheckCommand(argv))
    return tuple(commands)


INSPECTION = (
    "git status", "git diff", "git log", "git show", "git ls-files",
    "rg", "ls", "cat", "head", "tail", "sed",
)
WORKFLOW_WRITES = (
    "git add", "git commit", "git push", "git merge", "git rebase", "git reset", "git checkout",
    "gh pr create", "gh pr edit", "gh pr merge", "gh pr review", "gh pr comment",
    "gh issue create", "gh issue edit", "gh issue comment",
)
SHELL_WRITE_PATTERNS = (">", ">>", "tee", "rm", "mv", "cp", "mkdir", "touch", "sed -i")


@with_config(ConfigDict(extra="forbid"))
class ReadOnlyRecord(TypedDict):
    kind: Literal["read_only"]
    read_roots: list[str]


@with_config(ConfigDict(extra="forbid"))
class EditingRecord(TypedDict):
    kind: Literal["editing"]
    read_roots: list[str]
    checks: list[list[str]]


GrantRecord = ReadOnlyRecord | EditingRecord


@dataclass(frozen=True)
class ReadOnly:
    read_roots: tuple[Path, ...] = ()

    def record(self) -> ReadOnlyRecord:
        return {"kind": "read_only", "read_roots": [str(root) for root in self.read_roots]}


@dataclass(frozen=True)
class Editing:
    read_roots: tuple[Path, ...] = ()
    checks: tuple[CheckCommand, ...] = ()

    def record(self) -> EditingRecord:
        return {"kind": "editing", "read_roots": [str(root) for root in self.read_roots],
                "checks": [list(check.argv) for check in self.checks]}


Grant = ReadOnly | Editing
_GRANT_RECORD = TypeAdapter(GrantRecord)


def grant_from_record(record: GrantRecord) -> Grant:
    value = _GRANT_RECORD.validate_python(record, strict=True)
    roots = tuple(Path(root) for root in value["read_roots"])
    if value["kind"] == "editing":
        return Editing(roots, tuple(CheckCommand(tuple(argv)) for argv in value["checks"]))
    return ReadOnly(roots)
