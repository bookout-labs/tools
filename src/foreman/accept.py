"""Decide whether a GitHub delivery starts a run. This does not call a model."""

import re
from typing import Literal
from typing_extensions import TypedDict

from foreman._raw import is_dict
from foreman.github import BOT_LOGIN, READY_LABEL

REPOSITORY = re.compile(r"[\w.-]+/[\w.-]+")


class IgnoreEvent(TypedDict):
    start: Literal[False]


class StartEvent(TypedDict):
    start: Literal[True]
    repo: str
    issue: int


class AcceptError(ValueError):
    pass


def accept_event(name: str, payload: object) -> IgnoreEvent | StartEvent:
    """Start on an `issues` delivery whose added label is Ready on a real issue. Admission re-reads the issue live.

    Foreman's own label writes never start a run, like Warp's default of skipping bot-authored events
    (https://docs.warp.dev/factories/automation-triggers/). A child Spec labels Ready starts once, from its `start` line.
    """
    if not is_dict(payload):
        raise AcceptError("payload must be a mapping")
    if name != "issues" or payload.get("action") != "labeled":
        return {"start": False}
    label, issue, repository, sender = payload.get("label"), payload.get("issue"), payload.get("repository"), payload.get("sender")
    if is_dict(sender) and isinstance(login := sender.get("login"), str) and login.casefold() == BOT_LOGIN.casefold():
        return {"start": False}
    if not (is_dict(label) and is_dict(issue) and is_dict(repository)):
        raise AcceptError("an issues delivery needs label, issue and repository")
    # The added label decides, not the issue's full label list, and a pull request is never an issue run.
    if label.get("name") != READY_LABEL or "pull_request" in issue:
        return {"start": False}
    repo, number = repository.get("full_name"), issue.get("number")
    if not isinstance(repo, str) or not REPOSITORY.fullmatch(repo):
        raise AcceptError("repository.full_name must be OWNER/NAME")
    if not isinstance(number, int) or isinstance(number, bool) or number < 1:
        raise AcceptError("issue.number must be a positive integer")
    return {"start": True, "repo": repo, "issue": number}
