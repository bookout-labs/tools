"""The file an engine repository keeps so it points at this repo."""

from pathlib import Path
import re

import yaml

SOURCE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
REQUIRED = ("source", "ref", "graph")
OPTIONAL = ("check",)
KNOWN_GRAPHS = ("delivery",)


class PointerError(ValueError):
    pass


def load_pointer(path: Path) -> dict[str, str]:
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict):
        raise PointerError("pointer must be a mapping")
    unknown = set(data) - set(REQUIRED) - set(OPTIONAL)
    if unknown:
        raise PointerError(f"unknown pointer keys: {', '.join(sorted(unknown))}")
    missing = [key for key in REQUIRED if not isinstance(data.get(key), str) or not data[key].strip()]
    if missing:
        raise PointerError(f"pointer is missing {', '.join(missing)}")
    source = data["source"].strip()
    if not SOURCE.fullmatch(source):
        raise PointerError("source must be owner/repo")
    graph = data["graph"].strip()
    if graph not in KNOWN_GRAPHS:
        raise PointerError(f"unknown graph {graph}")
    pointer = {
        "source": source,
        "ref": data["ref"].strip(),
        "graph": graph,
    }
    check = data.get("check")
    if check is not None:
        if not isinstance(check, str) or not check.strip():
            raise PointerError("check must be a command")
        pointer["check"] = check.strip()
    return pointer
