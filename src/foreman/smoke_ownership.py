"""GitHub ownership and process exclusion for smoke fixtures."""

from contextlib import contextmanager
import fcntl
import re
from collections.abc import Generator
from uuid import uuid4

from foreman.github import BOT_LOGIN, READY_LABEL, ForgeError, LiveForge, SmokeIssue, human_gh
from foreman.state import checkpoint_store

_RUN = re.compile(r"<!-- foreman-smoke-run: [0-9a-f]{4}(?:-[0-9a-f]{4}){7} -->")
CLEANUP = "Foreman smoke cleanup closed this leftover pull request."


@contextmanager
def smoke_lock(repo: str) -> Generator[None, None, None]:
    state = checkpoint_store().parent
    state.mkdir(parents=True, exist_ok=True)
    with (state / f"smoke-{repo.lower().replace('/', '--')}.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ForgeError(f"Smoke already running for {repo}.") from exc
        yield


def _markers(body: str | None) -> list[str]:
    return [line for line in (body or "").splitlines() if _RUN.fullmatch(line)]


def owned(issue: SmokeIssue, repo: str) -> bool:
    return (issue.repository_url.lower() == f"https://api.github.com/repos/{repo.lower()}"
            and bool(issue.user and issue.user.login == BOT_LOGIN)
            and any(label.name.lower() == "smoke" for label in issue.labels)
            and "pull_request" not in issue.model_fields_set and len(_markers(issue.body)) == 1)


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
        elif len(links) == len(pull.closingIssuesReferences.nodes) and links.issubset(issues):
            forge.close_pull_request(repo, pull.number, CLEANUP)
    for number, issue in issues.items():
        if issue.state == "open" and number not in protected:
            forge.close_issue(repo, number)


def create_smoke_issue(forge: LiveForge, repo: str) -> int:
    run = uuid4().hex
    marker = "<!-- foreman-smoke-run: " + "-".join(run[i:i+4] for i in range(0, 32, 4)) + " -->"
    body = "Add smoke_<this issue number>.txt containing exactly smoke <this issue number> and a newline. Change no other files."
    try:
        # Ready comes later, from the human login, so that its label delivery is the human's and starts the run.
        return forge.create_issue(repo, "Smoke fixture", body, ["smoke"], marker=marker)
    except ForgeError:
        matches = [issue.number for issue in forge.smoke_issues(repo)
                   if issue.state == "open" and owned(issue, repo) and _markers(issue.body) == [marker]]
        if len(matches) == 1:
            return matches[0]
        raise


class HumanSessionMissing(Exception):
    """The host's gh is not signed in as the repository's human login, so the smoke cannot start its run."""


def human_session(login: str) -> None:
    """Refuse a host whose gh is not signed in as the repository's human login, before the smoke files anything."""
    try:
        signed = human_gh(["api", "user", "--jq", ".login"])
    except OSError as exc:
        raise HumanSessionMissing(f"gh is not available: {exc}") from exc
    if signed.returncode or signed.stdout.strip().casefold() != login.casefold():
        raise HumanSessionMissing(f"gh must be signed in as {login}: {signed.stdout.strip() or signed.stderr.strip()}")


def add_ready(repo: str, issue: int) -> None:
    """Add Ready to the smoke's own issue under the human login: the one write the smoke makes under a personal login."""
    added = human_gh(["api", "--method", "POST", f"repos/{repo}/issues/{issue}/labels", "-f", f"labels[]={READY_LABEL}"])
    if added.returncode:
        raise ForgeError(f"gh could not add {READY_LABEL} to {repo}#{issue}: {added.stderr.strip()}")
