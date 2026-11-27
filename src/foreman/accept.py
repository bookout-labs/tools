"""Decide whether a GitHub delivery starts or wakes a run. This does not call a model."""

from collections.abc import Mapping
import re
from typing import Literal, NamedTuple, Protocol

from foreman._raw import is_dict
from foreman.github import BOT_LOGIN, READY_LABEL
from foreman.stage_output import HumanPrompt
from foreman.steps import is_stop

REPOSITORY = re.compile(r"[\w.-]+/[\w.-]+")


class OpenRun(NamedTuple):
    """An issue's run that has not ended, or ended with a halt: running, waiting, halted, or paused at a gate on question."""

    login: str
    question: HumanPrompt | None


class MergeRun(NamedTuple):
    """A saved run that is in merge, whatever its step, with the number of its pull request, which is not the issue's."""

    issue: int
    pull_request: int | None


class SavedRuns(Protocol):
    """The saved runs, read without their locks."""

    def open(self, repo: str, issue: int) -> OpenRun | None: ...
    def in_merge(self, repo: str) -> list[MergeRun]: ...


# GitHub's events that wake a run in merge, by the payload key that names its pull request. A check names none.
MERGE_EVENTS = {"pull_request": "pull_request", "pull_request_review": "pull_request", "check_suite": None, "status": None}


class Accepted(NamedTuple):
    action: Literal["start", "wake", "recheck"]
    repo: str
    issue: int


class AcceptError(ValueError):
    pass


def accept_event(name: str, payload: object, runs: SavedRuns) -> list[Accepted]:
    """Start on an `issues` delivery whose added label is Ready on a real issue. Admission re-reads the issue live. A Ready for
    an issue whose saved run this release cannot read is refused, like a comment.

    Wake on an `issue_comment` delivery that GitHub created under the open run's saved login whose first line is a stop, or is
    the reply marker of the question the run is paused on (rules.gates.continuation). A stop wakes the run whatever it is
    doing, and a running run's wake waits for its lock. An edit wakes nothing, and neither does a delivery for a run that ended
    with a result other than a halt, or an answer once the question is closed. The wake reads the issue itself.

    A pull_request or pull_request_review delivery, whatever its action, wakes the run in merge whose pull request it names,
    and a check_suite completed or status delivery wakes every run in merge in its repository, because GitHub may record a
    required check on the pull request's test merge commit, which no run saves (merge's invariants). Each wake decides under
    the run's lock, from the saved run alone, so a wake that finds nothing to continue changes nothing.

    Recheck on an `issues` delivery whose action is closed, on a real issue that no Foreman run owns, whoever closed it: each
    of its open dependents returns to admission (intake.policy). An open run owns its issue, and its merge.release returns
    the dependents, so a closure while it runs or waits in merge rechecks nothing.

    Foreman's own label writes never start a run, like Warp's default of skipping bot-authored events
    (https://docs.warp.dev/factories/automation-triggers/).
    """
    if not is_dict(payload):
        raise AcceptError("payload must be a mapping")
    if name in MERGE_EVENTS:
        return _merge_wakes(name, payload, runs)
    if (name, payload.get("action")) == ("issue_comment", "created"):
        comment, issue, repository = payload.get("comment"), payload.get("issue"), payload.get("repository")
        if not (is_dict(comment) and is_dict(issue) and is_dict(repository)):
            raise AcceptError("an issue_comment delivery needs comment, issue and repository")
        if "pull_request" in issue:
            return []
        repo, number = _identity(repository, issue)
        author, body = comment.get("user"), comment.get("body")
        login = author.get("login") if is_dict(author) else None
        run = runs.open(repo, number)
        if run is None or not isinstance(login, str) or not isinstance(body, str) or login.casefold() != run.login.casefold():
            return []
        wakes = is_stop(body) or run.question is not None and run.question.answer_in(body) is not None
        return [Accepted("wake", repo, number)] if wakes else []
    if (name, payload.get("action")) == ("issues", "closed"):
        issue, repository = payload.get("issue"), payload.get("repository")
        if not (is_dict(issue) and is_dict(repository)):
            raise AcceptError("an issues delivery needs issue and repository")
        if "pull_request" in issue:
            return []
        repo, number = _identity(repository, issue)
        return [] if runs.open(repo, number) is not None else [Accepted("recheck", repo, number)]
    if name != "issues" or payload.get("action") != "labeled":
        return []
    label, issue, repository, sender = payload.get("label"), payload.get("issue"), payload.get("repository"), payload.get("sender")
    if is_dict(sender) and isinstance(login := sender.get("login"), str) and login.casefold() == BOT_LOGIN.casefold():
        return []
    if not (is_dict(label) and is_dict(issue) and is_dict(repository)):
        raise AcceptError("an issues delivery needs label, issue and repository")
    # The added label decides, not the issue's full label list, and a pull request is never an issue run.
    if label.get("name") != READY_LABEL or "pull_request" in issue:
        return []
    repo, number = _identity(repository, issue)
    # Ready reads the issue's saved run only so that a run this release cannot read refuses it, as it refuses a comment.
    runs.open(repo, number)
    return [Accepted("start", repo, number)]


def _merge_wakes(name: str, payload: Mapping[object, object], runs: SavedRuns) -> list[Accepted]:
    if name == "check_suite" and payload.get("action") != "completed":
        return []
    repository = payload.get("repository")
    if not is_dict(repository):
        raise AcceptError(f"a {name} delivery needs repository")
    repo = _repository(repository)
    number: int | None = None
    if key := MERGE_EVENTS[name]:
        if not is_dict(pull := payload.get(key)):
            raise AcceptError(f"a {name} delivery needs {key}")
        number = _number(pull, f"{key}.number")
    return [Accepted("wake", repo, run.issue) for run in runs.in_merge(repo) if number is None or run.pull_request == number]


def _identity(repository: Mapping[object, object], issue: Mapping[object, object]) -> tuple[str, int]:
    return _repository(repository), _number(issue, "issue.number")


def _repository(repository: Mapping[object, object]) -> str:
    if not isinstance(repo := repository.get("full_name"), str) or not REPOSITORY.fullmatch(repo):
        raise AcceptError("repository.full_name must be OWNER/NAME")
    return repo


def _number(item: Mapping[object, object], field: str) -> int:
    if not isinstance(number := item.get("number"), int) or isinstance(number, bool) or number < 1:
        raise AcceptError(f"{field} must be a positive integer")
    return number
