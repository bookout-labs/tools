"""The file an engine repository keeps so it points at this repo."""

from pathlib import Path
import re

import yaml

SOURCE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
REQUIRED = ("source", "ref", "graph")
OPTIONAL = ("check", "save")
KNOWN_GRAPHS = ("delivery",)
SAVE_KEYS = {"app", "privacy", "work_id_pattern", "board_repo", "repo", "proof", "queue", "hooks", "paths"}
PROOF_MODES = {"required", "optional"}


class PointerError(ValueError):
    pass


def _text(value: object, detail: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PointerError(detail)
    return value.strip()


def _repo(value: object, detail: str) -> str:
    repo = _text(value, detail)
    if not SOURCE.fullmatch(repo):
        raise PointerError(detail)
    return repo


def _save(raw: object) -> dict:
    if not isinstance(raw, dict):
        raise PointerError("save must be a mapping")
    unknown = set(raw) - SAVE_KEYS
    if unknown:
        raise PointerError(f"unknown save keys: {', '.join(sorted(unknown))}")
    if "repo" not in raw and "board_repo" not in raw:
        raise PointerError("save needs repo or board_repo")
    save: dict = {}
    if "app" in raw:
        app = raw["app"]
        if app == "none":
            save["app"] = None
        elif isinstance(app, dict):
            save["app"] = {
                "id": _text(app.get("id"), "save.app.id must be a string"),
                "installation": _text(app.get("installation"), "save.app.installation must be a string"),
                "key": _text(app.get("key"), "save.app.key must be a string"),
            }
            extra = set(app) - {"id", "installation", "key"}
            if extra:
                raise PointerError(f"unknown save.app keys: {', '.join(sorted(extra))}")
        else:
            raise PointerError("save.app must be a mapping or none")
    if "privacy" in raw:
        privacy = raw["privacy"]
        if privacy == "none":
            save["privacy"] = None
        else:
            save["privacy"] = _text(privacy, "save.privacy must be a path or none")
    if "work_id_pattern" in raw:
        save["work_id_pattern"] = _text(raw["work_id_pattern"], "save.work_id_pattern must be a regex")
    if "board_repo" in raw:
        save["board_repo"] = _repo(raw["board_repo"], "save.board_repo must be owner/repo")
    if "repo" in raw:
        save["repo"] = _repo(raw["repo"], "save.repo must be owner/repo")
    if "proof" in raw:
        proof = raw["proof"]
        if proof not in PROOF_MODES:
            raise PointerError("save.proof must be required or optional")
        save["proof"] = proof
    if "queue" in raw:
        if not isinstance(raw["queue"], bool):
            raise PointerError("save.queue must be true or false")
        save["queue"] = raw["queue"]
    if "hooks" in raw:
        hooks = raw["hooks"]
        if not isinstance(hooks, dict):
            raise PointerError("save.hooks must be a mapping")
        extra = set(hooks) - {"pre_save", "post_save"}
        if extra:
            raise PointerError(f"unknown save.hooks keys: {', '.join(sorted(extra))}")
        save["hooks"] = {
            name: _text(hooks[name], f"save.hooks.{name} must be a command")
            for name in ("pre_save", "post_save")
            if name in hooks
        }
    if "paths" in raw:
        paths = raw["paths"]
        if not isinstance(paths, dict):
            raise PointerError("save.paths must be a mapping")
        extra = set(paths) - {"code", "system"}
        if extra:
            raise PointerError(f"unknown save.paths keys: {', '.join(sorted(extra))}")
        cleaned: dict[str, list[str]] = {}
        for kind in ("code", "system"):
            values = paths.get(kind, [])
            if not isinstance(values, list) or not all(isinstance(item, str) and item for item in values):
                raise PointerError(f"save.paths.{kind} must be a list of paths")
            cleaned[kind] = values
        save["paths"] = cleaned
    return save


def load_pointer(path: Path) -> dict:
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
    pointer: dict = {
        "source": source,
        "ref": data["ref"].strip(),
        "graph": graph,
    }
    check = data.get("check")
    if check is not None:
        if not isinstance(check, str) or not check.strip():
            raise PointerError("check must be a command")
        pointer["check"] = check.strip()
    if "save" in data:
        pointer["save"] = _save(data["save"])
    return pointer
