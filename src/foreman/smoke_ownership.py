"""GitHub ownership and process exclusion for smoke fixtures."""

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import re
from collections.abc import Generator
from uuid import uuid4

from foreman.github import BOT_LOGIN, ForgeError, LiveForge, SmokeIssue

_RUN = re.compile(r"^<!-- foreman-smoke-run: [0-9a-f]{32} -->$", re.MULTILINE)


@contextmanager
def smoke_lock(repo: str) -> Generator[None, None, None]:
    state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "foreman"
    state.mkdir(parents=True, exist_ok=True)
    with (state / f"smoke-{repo.replace('/', '--')}.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ForgeError(f"Smoke already running for {repo}.") from exc
        yield


def owned(issue: SmokeIssue, repo: str) -> bool:
    return (issue.repository_url.lower() == f"https://api.github.com/repos/{repo.lower()}"
            and issue.user.login == BOT_LOGIN and any(label.name == "smoke" for label in issue.labels)
            and "pull_request" not in issue.model_fields_set and bool(_RUN.search(issue.body or "")))


def cleanup_smoke(forge: LiveForge, repo: str) -> None:
    issues = {issue.number: issue for issue in forge.smoke_issues(repo) if owned(issue, repo)}
    protected: set[int] = set()
    for pull in forge.smoke_pull_requests(repo):
        links = {issue.number for issue in pull.closingIssuesReferences.nodes
                 if f"{issue.repository.owner.login}/{issue.repository.name}".lower() == repo.lower()}
        if not links.intersection(issues):
            continue
        if pull.autoMergeRequest is not None or pull.mergeQueueEntry is not None:
            protected.update(links)
        else:
            forge.close_pull_request(repo, pull.number)
    for number, issue in issues.items():
        if issue.state == "open" and number not in protected:
            forge.close_issue(repo, number)


def create_smoke_issue(forge: LiveForge, repo: str) -> int:
    marker = f"<!-- foreman-smoke-run: {uuid4().hex} -->"
    body = ("Add smoke_<this issue number>.txt containing exactly smoke <this issue number> "
            f"and a newline. Change no other files.\n\n{marker}")
    try:
        return forge.create_issue(repo, "Smoke fixture", body, ["smoke"])
    except ForgeError:
        matches = [issue.number for issue in forge.smoke_issues(repo)
                   if owned(issue, repo) and marker in (issue.body or "").splitlines()]
        if len(matches) == 1:
            return matches[0]
        raise
