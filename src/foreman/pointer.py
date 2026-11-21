"""The file an engine repository keeps so it points at this repo."""

from pathlib import Path
import re
from typing import NotRequired, TypedDict

import yaml

from foreman._raw import is_dict

SOURCE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
REQUIRED = ("source", "ref", "graph")
OPTIONAL = ("check",)
KNOWN_GRAPHS = ("delivery",)


class PointerError(ValueError):
    pass


class Pointer(TypedDict):
    source: str
    ref: str
    graph: str
    check: NotRequired[str]


def load_pointer(path: Path) -> Pointer:
    data: object = yaml.safe_load(path.read_text())
    if not is_dict(data):
        raise PointerError("pointer must be a mapping")
    keys: set[str] = set()
    for key in data:
        if not isinstance(key, str):
            raise PointerError(f"pointer keys must be strings, got {type(key).__name__}")
        keys.add(key)
    unknown = keys - set(REQUIRED) - set(OPTIONAL)
    if unknown:
        raise PointerError(f"unknown pointer keys: {', '.join(sorted(unknown))}")
    required: dict[str, str] = {}
    for key in REQUIRED:
        value = data.get(key)
        if isinstance(value, str) and (text := value.strip()):
            required[key] = text
    missing = [key for key in REQUIRED if key not in required]
    if missing:
        raise PointerError(f"pointer is missing {', '.join(missing)}")
    source = required["source"]
    if not SOURCE.fullmatch(source):
        raise PointerError("source must be owner/repo")
    graph = required["graph"]
    if graph not in KNOWN_GRAPHS:
        raise PointerError(f"unknown graph {graph}")
    pointer: Pointer = {
        "source": source,
        "ref": required["ref"],
        "graph": graph,
    }
    check = data.get("check")
    if check is not None:
        if not isinstance(check, str) or not check.strip():
            raise PointerError("check must be a command")
        pointer["check"] = check.strip()
    return pointer
