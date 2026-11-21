"""Decide whether an outside event enters the graph. This does not call a model."""

from typing import Literal, TypedDict

from foreman._raw import is_dict, is_list

FACTORY_LABEL = "factory"


class IgnoreEvent(TypedDict):
    start: Literal[False]


class StartEvent(TypedDict):
    start: Literal[True]
    source: Literal["github"]
    request: str


class AcceptError(ValueError):
    pass


def _request(title: object, body: object) -> str:
    if not isinstance(title, str) or not title.strip():
        raise AcceptError("event needs a title")
    if body is None:
        body = ""
    if not isinstance(body, str):
        raise AcceptError("body must be text")
    if not body.strip():
        return title.strip()
    return f"{title.strip()}\n\n{body.strip()}"


def accept_event(event: object) -> IgnoreEvent | StartEvent:
    if not is_dict(event):
        raise AcceptError("event must be a mapping")
    source = event.get("source")
    if source == "github":
        if event.get("event") != "issue_labeled":
            return {"start": False}
        labels = event.get("labels")
        if not labels or not is_list(labels) or FACTORY_LABEL not in labels:
            return {"start": False}
        return {"start": True, "source": "github", "request": _request(event.get("title"), event.get("body"))}
    if source == "alert":
        return {"start": False}
    raise AcceptError(f"unknown event source {source}")
