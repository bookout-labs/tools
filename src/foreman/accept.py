"""Decide whether a GitHub delivery starts or wakes a run. This does not call a model."""

from collections.abc import Callable, Mapping
import re
from typing import Literal, NamedTuple

from foreman._raw import is_dict
from foreman.github import BOT_LOGIN, READY_LABEL
from foreman.stage_output import HumanPrompt
from foreman.steps import is_stop

REPOSITORY = re.compile(r"[\w.-]+/[\w.-]+")


class Paused(NamedTuple):
    """An issue's open run that waits: on the human's answer to its question, or on a provider or GitHub when question is None."""

    login: str
    question: HumanPrompt | None


class Accepted(NamedTuple):
    action: Literal["start", "wake"]
    repo: str
    issue: int


class AcceptError(ValueError):
    pass


def accept_event(name: str, payload: object, paused: Callable[[str, int], Paused | None]) -> Accepted | None:
    """Start on an `issues` delivery whose added label is Ready on a real issue. Admission re-reads the issue live.

    Wake on an `issue_comment` delivery that GitHub created under the paused run's saved login whose first line is its open
    question's reply marker, or is a stop when the run waits on a provider or GitHub (rules.gates.continuation). An edit
    wakes nothing, and neither does a delivery that comes once the question is closed. The wake reads the answer from the issue.

    Foreman's own label writes never start a run, like Warp's default of skipping bot-authored events
    (https://docs.warp.dev/factories/automation-triggers/). A child Spec labels Ready starts once, from its `start` line.
    """
    if not is_dict(payload):
        raise AcceptError("payload must be a mapping")
    if (name, payload.get("action")) == ("issue_comment", "created"):
        comment, issue, repository = payload.get("comment"), payload.get("issue"), payload.get("repository")
        if not (is_dict(comment) and is_dict(issue) and is_dict(repository)):
            raise AcceptError("an issue_comment delivery needs comment, issue and repository")
        if "pull_request" in issue:
            return None
        repo, number = _identity(repository, issue)
        author, body = comment.get("user"), comment.get("body")
        login = author.get("login") if is_dict(author) else None
        run = paused(repo, number)
        if run is None or not isinstance(login, str) or not isinstance(body, str) or login.casefold() != run.login.casefold():
            return None
        wakes = is_stop(body) if run.question is None else run.question.answer_in(body) is not None
        return Accepted("wake", repo, number) if wakes else None
    if name != "issues" or payload.get("action") != "labeled":
        return None
    label, issue, repository, sender = payload.get("label"), payload.get("issue"), payload.get("repository"), payload.get("sender")
    if is_dict(sender) and isinstance(login := sender.get("login"), str) and login.casefold() == BOT_LOGIN.casefold():
        return None
    if not (is_dict(label) and is_dict(issue) and is_dict(repository)):
        raise AcceptError("an issues delivery needs label, issue and repository")
    # The added label decides, not the issue's full label list, and a pull request is never an issue run.
    if label.get("name") != READY_LABEL or "pull_request" in issue:
        return None
    return Accepted("start", *_identity(repository, issue))


def _identity(repository: Mapping[object, object], issue: Mapping[object, object]) -> tuple[str, int]:
    repo, number = repository.get("full_name"), issue.get("number")
    if not isinstance(repo, str) or not REPOSITORY.fullmatch(repo):
        raise AcceptError("repository.full_name must be OWNER/NAME")
    if not isinstance(number, int) or isinstance(number, bool) or number < 1:
        raise AcceptError("issue.number must be a positive integer")
    return repo, number
