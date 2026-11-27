"""GitHub operations for delivery runs."""

from __future__ import annotations

from collections.abc import Callable
import json
import re
import subprocess
from typing import TYPE_CHECKING, Generic, Iterator, Literal, Protocol, TypeVar
from typing_extensions import TypedDict, NotRequired
from urllib.parse import quote

from pydantic import AwareDatetime, BaseModel, ConfigDict, JsonValue, PositiveInt, RootModel, ValidationError, with_config

from foreman.readiness import BranchPolicy, CheckEvidence, CheckPages, Commit, Observation, PullSnapshot, Outcome, Rules, StatusPages, QueueSnapshot, QueueEntry, MergeReceipt, QueuePolicy, AutoMergeRequest, PullRequestState
from foreman.credentials import CredentialFailure, NotInstalled, Subscription, Unanswered, credentials, failure, COMMAND_TIMEOUT
from foreman.redaction import redact
if TYPE_CHECKING:
    from foreman.stage_output import StoredSlice
from foreman.workspace import commit_sha

BOT_LOGIN = "bookout-labs-foreman[bot]"
MERGE_HOLD_VARIABLE = "FOREMAN_MERGE_HOLD"
# A Ready label event starts an issue run, so Spec releases the children it files by adding it.
READY_LABEL = "Ready"
# The run labels, one at a time on an issue: the run's checkpoint decides, and a label never grants authority.
DOING_LABEL, NEEDS_ANSWER_LABEL, STOPPED_LABEL, DONE_LABEL = "Doing", "Needs answer", "Stopped", "Done"
# Foreman's one comment on a pull request it closes.
CLOSE_MARKER = "<!-- foreman close -->"
RUN_LABELS: dict[str, str] = {READY_LABEL: "0e8a16", DOING_LABEL: "1d76db", NEEDS_ANSWER_LABEL: "d93f0b", STOPPED_LABEL: "b60205", DONE_LABEL: "5319e7"}


def run_label(name: str) -> str | None:
    """The run label a live label name is. GitHub matches label names case-insensitively and keeps an existing label's case."""
    return next((label for label in RUN_LABELS if label.casefold() == name.casefold()), None)
Model = TypeVar("Model", bound=BaseModel)


# Whether the App is installed on a repository, or this host has no App to act as.
AppInstallation = Literal["installed", "not_installed", "unconfigured"]
# GitHub's base role for a login on a repository. It reports maintain as write and triage as read.
Permission = Literal["admin", "write", "read", "none"]


class ForgeError(RuntimeError):
    """A failed GitHub operation, with the HTTP status GitHub answered and whether it was a rate-limit answer."""

    def __init__(self, message: str, status: int | None = None, *, rate_limited: bool = False):
        super().__init__(message)
        self.status, self.rate_limited = status, rate_limited or status == 429

    @property
    def answer(self) -> str:
        """What GitHub answered, in words: its status, its rate limit, or no answer."""
        if self.status is None:
            return "answered with its rate limit" if self.rate_limited else "gave no answer"
        return f"answered HTTP {self.status}" + (" for its rate limit" if self.rate_limited else "")


class MergeNotArmed(ForgeError):
    pass


class HandoffChanged(MergeNotArmed):
    pass


class QueueRejected(MergeNotArmed):
    pass


class VariablesUnreadable(ForgeError):
    pass


class CommandFailed(ForgeError):
    pass


class CommandTimedOut(ForgeError):
    def __init__(self, operation: str):
        self.operation = operation
        super().__init__(f"{operation} timed out.")


class PullRequestStatus(BaseModel, strict=True):
    state: PullRequestState


class MergeCommit(BaseModel, strict=True):
    oid: str


class ClosingPullRequestStatus(PullRequestStatus):
    baseRefName: str
    mergeCommit: MergeCommit | None = None


class DefaultBranch(BaseModel, strict=True):
    name: str


class RepositoryDefaultBranch(BaseModel, strict=True):
    defaultBranchRef: DefaultBranch


@with_config(ConfigDict(extra="forbid"))
class AutoMergeRequestRecord(TypedDict):
    enabledAt: NotRequired[str | None]
    mergeMethod: Literal["MERGE", "REBASE", "SQUASH"]


@with_config(ConfigDict(extra="forbid"))
class PullRequestRecord(TypedDict):
    state: PullRequestState
    number: PositiveInt
    headRefOid: str
    headRefName: str
    baseRefName: str
    isCrossRepository: bool
    baseRefOid: str
    autoMergeRequest: AutoMergeRequestRecord | None


class PullRequest(PullRequestStatus):
    number: PositiveInt
    headRefOid: str
    headRefName: str
    baseRefName: str
    isCrossRepository: bool = False
    baseRefOid: str
    autoMergeRequest: AutoMergeRequest | None


class PublishedPullRequest(PullRequest):
    body: str


class IssueLabel(BaseModel, strict=True):
    name: str


class IssueView(BaseModel, strict=True):
    number: PositiveInt
    repository_url: str
    title: str
    body: str | None
    labels: list[IssueLabel]
    pull_request: JsonValue = None


class RepositoryOwner(BaseModel, strict=True):
    login: str


class RepositoryReference(BaseModel, strict=True):
    name: str
    owner: RepositoryOwner


class ClosingPullRequest(BaseModel, strict=True):
    id: str
    number: PositiveInt
    repository: RepositoryReference
    url: str


class IssueClosure(BaseModel, strict=True):
    stateReason: Literal["", "COMPLETED", "NOT_PLANNED", "DUPLICATE", "REOPENED"]
    closedByPullRequestsReferences: list[ClosingPullRequest]


class CreatedIssue(BaseModel, strict=True):
    number: PositiveInt


class SmokeIssue(BaseModel, strict=True):
    state: Literal["open", "closed"]
    number: PositiveInt
    body: str | None
    repository_url: str
    labels: list[IssueLabel]
    user: RepositoryOwner | None
    pull_request: JsonValue = None


class SmokeIssuePages(RootModel[list[list[SmokeIssue]]], strict=True):
    pass


class SmokePageInfo(BaseModel, strict=True):
    hasNextPage: bool
    endCursor: str | None


class SmokeConnection(BaseModel, Generic[Model], strict=True):
    nodes: list[Model]
    pageInfo: SmokePageInfo


class SmokeLinkedIssue(BaseModel, strict=True):
    number: PositiveInt
    repository: RepositoryReference


class SmokeOpenPull(BaseModel, strict=True):
    number: PositiveInt
    closingIssuesReferences: SmokeConnection[SmokeLinkedIssue]
    autoMergeRequest: AutoMergeRequest | None
    mergeQueueEntry: QueueEntry | None


class ItemState(BaseModel, strict=True):
    state: Literal["open", "closed"]


class IssueComment(BaseModel, strict=True):
    id: PositiveInt | None = None
    body: str | None
    user: RepositoryOwner | None = None
    created_at: AwareDatetime | None = None


class EventLabel(BaseModel, strict=True):
    name: str


class IssueEvent(BaseModel, strict=True):
    event: str
    created_at: AwareDatetime
    label: EventLabel | None = None


class IssueEventPages(RootModel[list[list[IssueEvent]]], strict=True):
    pass


class RepositoryIssue(BaseModel, strict=True):
    number: PositiveInt
    body: str | None
    pull_request: JsonValue = None


class OpenIssuePages(RootModel[list[list[RepositoryIssue]]], strict=True):
    pass


class IssueCommentPages(RootModel[list[list[IssueComment]]], strict=True):
    pass


class ActionsVariable(BaseModel, strict=True):
    name: str
    value: str


class ActionsVariables(RootModel[list[ActionsVariable]], strict=True):
    # gh variable list --json name,value, v2.102.0:
    # https://github.com/cli/cli/blob/v2.102.0/pkg/cmd/variable/list/list_test.go#L112-L122
    # gh fetches every page with per_page=100 and RESTWithNext.
    # https://github.com/cli/cli/blob/v2.102.0/pkg/cmd/variable/list/list.go#L237-L250
    pass


class IssueIdentity(BaseModel, strict=True):
    id: PositiveInt


class IssueBlocker(BaseModel, strict=True):
    number: PositiveInt
    title: str
    repository_url: str
    html_url: str
    state: Literal["open", "closed"]

    def in_repo(self, repo: str) -> bool:
        return self.repo.lower() == repo.lower()

    @property
    def repo(self) -> str:
        return self.repository_url.removeprefix("https://api.github.com/repos/")


class BlockerPages(RootModel[list[list[IssueBlocker]]]):
    pass


class LinkedIssue(IssueBlocker):
    id: PositiveInt


class LinkedIssuePages(RootModel[list[list[LinkedIssue]]], strict=True):
    pass


class Issue(TypedDict):
    repo: str
    number: int
    title: str
    body: str
    labels: list[str]


class ReviewComment(TypedDict):
    path: str
    line: int
    side: Literal["RIGHT"]
    body: str


class ReviewReceipt(TypedDict):
    id: int
    state: Literal["COMMENTED"]
    commit_id: str


class PostedReview(BaseModel, strict=True):
    id: PositiveInt
    state: str
    commit_id: str
    body: str

    def receipt(self, head: str) -> ReviewReceipt:
        if self.state != "COMMENTED" or self.commit_id.lower() != head.lower():
            raise ForgeError("Review receipt does not match the submitted COMMENT at the verified head.")
        return {"id": self.id, "state": "COMMENTED", "commit_id": self.commit_id}


class ListedReview(BaseModel, strict=True):
    id: PositiveInt
    state: str
    commit_id: str | None
    body: str


class ReviewPages(RootModel[list[list[ListedReview]]]):
    pass


def queue_receipt(snapshot: QueueSnapshot, repo: str, number: int, head: str,
                  base: str, base_ref: str, policy: QueuePolicy) -> MergeReceipt | None:
    if (snapshot.number != number or snapshot.headRefOid.lower() != head.lower()
            or snapshot.baseRefOid.lower() != base.lower()
            or snapshot.baseRefName != base_ref or snapshot.state == "CLOSED" or snapshot.isDraft):
        raise HandoffChanged("PR changed during merge handoff. Renew readiness, verification and review.")
    if snapshot.state == "MERGED":
        identity, status = f"merged:{head}", "MERGED"
    elif (entry := snapshot.mergeQueueEntry) is not None:
        if entry.state == "UNMERGEABLE":
            raise QueueRejected(f"Queue rejected receipt {entry.id}: UNMERGEABLE. Repair and resume.")
        identity, status = entry.id, entry.state
    elif (request := snapshot.autoMergeRequest) is not None and request.enabledAt:
        identity, status = request.enabledAt, "AUTO_MERGE_ENABLED"
    else:
        return None
    return MergeReceipt(repo=repo, pr=number, source_head=head, base=snapshot.baseRefOid,
                        base_ref=base_ref, method=policy.merge_method, queue_policy=policy,
                        identity=identity, status=status)


class QueuedPullRequest(BaseModel, strict=True):
    id: str
    state: Literal["OPEN", "CLOSED", "MERGED"]
    mergeQueueEntry: QueueEntry | None


class Comparison(BaseModel, strict=True):
    status: str


class RepositoryPermission(BaseModel, strict=True):
    permission: Permission


class Forge(Protocol):
    def default_branch(self, repo: str) -> str: ...
    def open_pull_request(self, repo: str, head: str, base: str, title: str, body: str) -> None: ...
    def pull_request_for(self, repo: str, branch: str) -> PublishedPullRequest | None: ...
    def close_pull_request(self, repo: str, number: int, comment: str) -> None: ...
    def load_issue(self, repo: str, number: int) -> Issue: ...
    def marked_issues(self, repo: str, marker: str) -> list[int]: ...
    def sub_issues(self, repo: str, number: int) -> list[LinkedIssue]: ...
    def parent_issue(self, repo: str, number: int) -> LinkedIssue | None: ...
    def add_sub_issue(self, repo: str, number: int, child: int) -> None: ...
    def update_issue_body(self, repo: str, number: int, body: str) -> None: ...
    def issue_comment_records(self, repo: str, number: int) -> list[IssueComment]: ...
    def ready_labeled_at(self, repo: str, number: int) -> AwareDatetime | None: ...
    def comment_issue(self, repo: str, number: int, body: str, *, marker: str) -> None: ...
    def upsert_comment(self, repo: str, number: int, text: str, facts: list[str], *, marker: str) -> None: ...
    def create_issue(
        self, repo: str, title: str, body: str, labels: list[str], *, marker: str, child: StoredSlice | None = None,
    ) -> int: ...
    def add_blocker(self, repo: str, number: int, blocker: int) -> None: ...
    def blockers(self, repo: str, number: int) -> list[IssueBlocker]: ...
    def blocking(self, repo: str, number: int) -> list[IssueBlocker]: ...
    def is_merged(self, repo: str, number: int, *, contains: Callable[[str], bool] | None = None) -> bool: ...
    def descends(self, repo: str, ancestor: str, head: str) -> bool: ...
    def ensure_label(self, repo: str, name: str, color: str) -> None: ...
    def edit_labels(self, repo: str, number: int, add: list[str], remove: list[str]) -> None: ...
    def review_target(self, repo: str, issue: int, branch: str, sha: str) -> PullRequest: ...
    def post_review(self, repo: str, target: PullRequest, body: str, comments: list[ReviewComment], *, marker: str) -> ReviewReceipt: ...
    def readiness(self, repo: str, issue: int, branch: str, sha: str) -> Observation: ...
    def reconcile_merge(self, receipt: MergeReceipt) -> MergeReceipt: ...
    def variables(self, repo: str) -> dict[str, str]: ...
    def app_installation(self, repo: str) -> AppInstallation: ...
    def app_subscription(self, repo: str) -> Subscription: ...
    def permission(self, repo: str, login: str) -> Permission | None: ...
    def has_label(self, repo: str, name: str) -> bool: ...
    def merge_queue(self, repo: str, branch: str) -> QueuePolicy | None: ...
    def arm_auto_merge(self, repo: str, issue: int, branch: str, sha: str, observation: Observation) -> MergeReceipt | None: ...


def owned_comment(comments: list[IssueComment], marker: str) -> IssueComment | None:
    """The first comment Foreman itself posted with this marker. A comment by anyone else never counts."""
    return next((comment for comment in comments if comment.user is not None
                 and comment.user.login.casefold() == BOT_LOGIN.casefold() and (comment.body or "").endswith(marker)), None)


def merge_queue_policy(rules: Rules) -> QueuePolicy | None:
    """The one merge queue policy GitHub's rules apply to a branch, or None when there is none or more than one."""
    policies = [rule.parameters for rule in rules.root if rule.type == "merge_queue"]
    if len(policies) != 1 or policies[0].merge_method is None:
        return None
    return QueuePolicy(merge_method=policies[0].merge_method, grouping_strategy=policies[0].grouping_strategy)


def pull_request_command(repo: str, head: str, base: str, title: str, body: str) -> list[str]:
    return ["gh", "pr", "create", "--repo", redact(repo), "--base", redact(base), "--head", redact(head),
            "--title", redact(" ".join(title.split())), "--body", redact(body)]


def merge_command(repo: str, pull_request: str, sha: str) -> list[str]:
    return ["gh", "pr", "merge", pull_request, "--repo", repo, "--auto", "--squash", "--match-head-commit", sha]


def redact_slice(child: StoredSlice) -> StoredSlice:
    screen_name = child.screen.removeprefix("docs/screens/").removesuffix(".md")
    return child.model_copy(update={
        "title": redact(child.title),
        "acceptance": redact(child.acceptance),
        "files": [redact(path) for path in child.files],
        "depends_on": [redact(title) for title in child.depends_on],
        "screen": f"docs/screens/{redact(screen_name)}.md" if child.screen else "",
        "properties": [
            prop.model_copy(update={"name": redact(prop.name), "constraint": redact(prop.constraint)})
            for prop in child.properties
        ],
    })


def unmet_prerequisites(forge: "Forge", repo: str, blockers: list[IssueBlocker], item: StoredSlice | None) -> str:
    """Why triage cannot admit an issue yet, or "" once it can.

    A blocker counts once its closing pull request merged, even before GitHub closes its issue."""
    merged = {(blocker.repo.lower(), blocker.number) for blocker in blockers if forge.is_merged(blocker.repo, blocker.number)}
    if open_urls := [blocker.html_url for blocker in blockers
                     if blocker.state == "open" and (blocker.repo.lower(), blocker.number) not in merged]:
        return "Open blockers: " + ", ".join(open_urls)
    if item is None:
        return ""
    local, planned = {blocker.number for blocker in blockers if blocker.in_repo(repo)}, item.blocker_numbers
    if len(planned) != len(item.depends_on) or not set(planned) <= local:
        return "Planned blocker links are missing."
    unmerged = [number for number in planned if (repo.lower(), number) not in merged]
    return "Blockers have no merged pull request: " + ", ".join(f"#{number}" for number in unmerged) if unmerged else ""


def human_comment_records(forge: "Forge", login: str, repo: str, issue: int) -> list[IssueComment]:
    """Comments the configured human posted on the issue, which the coordinator relays under their identity."""
    expected = login.strip().casefold()
    assert expected, "A run reads human comments only under its configured login."
    return [comment for comment in forge.issue_comment_records(repo, issue)
            if comment.user is not None and comment.user.login.casefold() == expected]


def human_comments(forge: "Forge", login: str, repo: str, issue: int) -> list[str]:
    return [(comment.body or "").strip() for comment in human_comment_records(forge, login, repo, issue)]


def status_body(text: str, facts: list[str], marker: str) -> str:
    """Foreman's status comment. The text is redacted, while facts keep validated SHAs, which full redaction would wipe."""
    return marked("\n\n".join(part for part in (redact(text), *(redact(fact, credentials_only=True) for fact in facts)) if part), marker)


def marked(content: str, marker: str) -> str:
    """Foreman's marker follows the content verbatim. Redaction would rewrite a digest, and the marker could not be found again."""
    return "\n\n".join(part for part in (content, marker) if part)


def render_slice_body(prose: str, child: StoredSlice) -> str:
    child = redact_slice(child)
    dependencies = "\n".join(f"- {title}" for title in child.depends_on) or "None"
    constraints = "\n".join(f"- {prop.constraint}" for prop in child.properties) or "None"
    stored = child.model_dump(exclude_unset=True)
    if child.work_kind is not None:
        stored["work_kind"] = child.work_kind
    return (
        (f"{prose}\n\n" if prose else "")
        + f"Acceptance:\n{child.acceptance}\n\nDependencies:\n{dependencies}"
        + f"\n\nScreen:\n{child.screen or 'None'}\n\nConstraints:\n{constraints}"
        + f"\n\n```json\n{json.dumps(stored, indent=2)}\n```"
    )


class SmokeActor(BaseModel, strict=True):
    name: str
    email: str


class SmokeCommit(BaseModel, strict=True):
    author: SmokeActor
    committer: SmokeActor


class SmokeGitHubCommit(BaseModel, strict=True):
    sha: str
    author: RepositoryOwner | None
    committer: RepositoryOwner | None
    commit: SmokeCommit


class SmokeHead(BaseModel, strict=True):
    sha: str


class SmokePull(BaseModel, strict=True):
    user: RepositoryOwner
    head: SmokeHead


class SmokeReview(BaseModel, strict=True):
    user: RepositoryOwner
    state: Literal["COMMENTED", "APPROVED", "CHANGES_REQUESTED", "DISMISSED", "PENDING"]
    commit_id: str


class LiveForge:
    def __init__(self):
        self._defaults: dict[str, str] = {}

    def bot_identity(self, repo: str) -> dict[str, str]:
        user_id = self._read(repo, IssueIdentity, [
            "gh", "api", f"users/{quote(BOT_LOGIN, safe='')}",
        ]).id
        email = f"{user_id}+{BOT_LOGIN}@users.noreply.github.com"
        return {f"GIT_{role}_{field}": value for role in ("AUTHOR", "COMMITTER")
                for field, value in (("NAME", BOT_LOGIN), ("EMAIL", email))}

    def smoke_identity(self, repo: str, pr: int, head: str, review: int) -> tuple[bool, bool, bool]:
        pull = self._read(repo, SmokePull, ["gh", "api", f"repos/{repo}/pulls/{pr}"])
        commit = self._read(repo, SmokeGitHubCommit, ["gh", "api", f"repos/{repo}/commits/{head}"])
        observed = self._read(repo, SmokeReview, ["gh", "api", f"repos/{repo}/pulls/{pr}/reviews/{review}"])
        identity = self.bot_identity(repo)
        return (
            pull.user.login == BOT_LOGIN and pull.head.sha.lower() == head.lower(),
            commit.sha.lower() == head.lower() and all(actor is not None and actor.login == BOT_LOGIN
                                                      for actor in (commit.author, commit.committer))
            and all(actor.name == BOT_LOGIN and actor.email == identity["GIT_AUTHOR_EMAIL"]
                    for actor in (commit.commit.author, commit.commit.committer)),
            observed.user.login == BOT_LOGIN and observed.state == "COMMENTED" and observed.commit_id.lower() == head.lower(),
        )

    def default_branch(self, repo: str) -> str:
        if repo not in self._defaults:
            self._defaults[repo] = self._read(
                repo, RepositoryDefaultBranch, ["gh", "repo", "view", repo, "--json", "defaultBranchRef"],
            ).defaultBranchRef.name
        return self._defaults[repo]

    def open_pull_request(self, repo: str, head: str, base: str, title: str, body: str) -> None:
        self._run_command(repo, pull_request_command(repo, head, base, title, body))

    def pull_request_for(self, repo: str, branch: str) -> PublishedPullRequest | None:
        command = ["gh", "pr", "view", "--repo", repo, "--json",
                   "number,state,headRefOid,headRefName,baseRefName,baseRefOid,isCrossRepository,autoMergeRequest,body", "--", branch]
        try:
            return self._read(repo, PublishedPullRequest, command)
        except CommandFailed as exc:
            if "no pull requests found for branch" in str(exc):
                return None
            raise

    def load_issue(self, repo: str, number: int) -> Issue:
        issue = self._read(
            repo, IssueView, ["gh", "api", f"repos/{repo}/issues/{number}"],
        )
        canonical = issue.repository_url.removeprefix("https://api.github.com/repos/")
        if issue.pull_request is not None or issue.number != number or canonical.lower() != repo.lower():
            raise ForgeError("GitHub returned an issue outside the requested repository and number.")
        return {"repo": canonical, "number": issue.number, "title": issue.title,
                "body": issue.body or "", "labels": [label.name for label in issue.labels]}

    def smoke_issues(self, repo: str) -> list[SmokeIssue]:
        pages = self._read(repo, SmokeIssuePages, [
            "gh", "api", f"repos/{repo}/issues?state=all&labels=smoke&per_page=100", "--paginate", "--slurp",
        ])
        return [item for page in pages.root for item in page if "pull_request" not in item.model_fields_set
                and item.repository_url.lower() == f"https://api.github.com/repos/{repo.lower()}"
                and any(label.name.lower() == "smoke" for label in item.labels)]

    def smoke_pull_requests(self, repo: str) -> list[SmokeOpenPull]:
        owner, name = repo.split("/", 1)
        fields = "number repository { name owner { login } }"
        query = """query($owner:String!, $name:String!, $endCursor:String) {
          repository(owner:$owner, name:$name) {
            pullRequests(states:OPEN, first:100, after:$endCursor) {
              pageInfo { hasNextPage endCursor }
              nodes { number autoMergeRequest { enabledAt mergeMethod } mergeQueueEntry { id state }
                closingIssuesReferences(first:100) { nodes { FIELDS } pageInfo { hasNextPage endCursor } }
              }
            }
          }
        }""".replace("FIELDS", fields)
        args = ["gh", "api", "graphql", "-f", f"owner={owner}", "-f", f"name={name}"]
        pulls = [pull for page in self._smoke_pages(repo, SmokeOpenPull, [
            *args, "-f", f"query={query}", "--jq", ".data.repository.pullRequests",
        ]) for pull in page.nodes]
        for pull in pulls:
            query = """query($owner:String!, $name:String!, $number:Int!, $endCursor:String) {
              repository(owner:$owner, name:$name) { pullRequest(number:$number) {
                closingIssuesReferences(first:100, after:$endCursor) {
                  nodes { FIELDS } pageInfo { hasNextPage endCursor }
                }
              } }
            }""".replace("FIELDS", fields)
            for connection in self._smoke_pages(repo, SmokeLinkedIssue, [
                *args, "-f", f"query={query}", "-F", f"number={pull.number}",
                "--jq", ".data.repository.pullRequest.closingIssuesReferences",
            ], first=pull.closingIssuesReferences):
                if connection is not pull.closingIssuesReferences:
                    pull.closingIssuesReferences.nodes.extend(connection.nodes)
                pull.closingIssuesReferences.pageInfo = connection.pageInfo
        return pulls

    def _smoke_pages(self, repo: str, model: type[Model], command: list[str], *,
                     first: SmokeConnection[Model] | None = None) -> Iterator[SmokeConnection[Model]]:
        page = first if first is not None else self._read(repo, SmokeConnection[model], command)
        seen: set[str] = set()
        while True:
            yield page
            if not page.pageInfo.hasNextPage:
                return
            cursor = page.pageInfo.endCursor
            if not cursor or cursor in seen:
                raise ForgeError("Missing or repeated smoke pagination cursor.")
            seen.add(cursor)
            page = self._read(repo, SmokeConnection[model], [*command, "-f", f"endCursor={cursor}"])

    def has_label(self, repo: str, name: str) -> bool:
        try:
            self._run_command(repo, ["gh", "api", f"repos/{repo}/labels/{quote(name, safe='')}"])
            return True
        except CommandFailed as exc:
            if exc.status != 404:
                raise
            return False

    def ensure_label(self, repo: str, name: str, color: str) -> None:
        """Create the label only when it is missing, so an existing label keeps its color."""
        repo, name = (redact(value) for value in (repo, name))
        if self.has_label(repo, name):
            return
        try:
            self._run_command(repo, ["gh", "api", f"repos/{repo}/labels", "--method", "POST", "-f", f"name={name}", "-f", f"color={color}"])
        except CommandFailed as exc:
            if exc.status != 422:  # Another writer created it first.
                raise

    def close_issue(self, repo: str, number: int) -> None:
        self._close_item(repo, f"repos/{repo}/issues/{number}")

    def close_pull_request(self, repo: str, number: int, comment: str) -> None:
        """Take the pull request out of the merge queue when it is queued, comment and close it. The branch stays in place.

        A PR that GitHub merged or closed first is left alone, and a merge that beats the dequeue wins (DQ1)."""
        owner, name = repo.split("/", 1)
        query = """query($owner:String!, $name:String!, $number:Int!) {
          repository(owner:$owner, name:$name) { pullRequest(number:$number) { id state mergeQueueEntry { id state } } }
        }"""

        def read() -> QueuedPullRequest:
            return self._read(repo, QueuedPullRequest, ["gh", "api", "graphql", "-f", f"query={query}", "-f", f"owner={owner}",
                              "-f", f"name={name}", "-F", f"number={number}", "--jq", ".data.repository.pullRequest"])

        queued = read()
        if queued.state != "OPEN":
            return
        if queued.mergeQueueEntry is not None:
            try:
                self._run_command(repo, ["gh", "api", "graphql", "-f",
                                         "query=mutation($id:ID!) { dequeuePullRequest(input:{id:$id}) { clientMutationId } }",
                                         "-f", f"id={queued.id}"])
            except CommandFailed:
                if read().state != "OPEN":
                    return
                raise
        self.upsert_comment(repo, number, comment, [], marker=CLOSE_MARKER)
        self._close_item(repo, f"repos/{repo}/pulls/{number}")

    def _close_item(self, repo: str, endpoint: str) -> None:
        try:
            item = self._read(repo, ItemState, ["gh", "api", endpoint, "--method", "PATCH", "-f", "state=closed"])
        except (CommandFailed, CommandTimedOut) as original:
            if isinstance(original, CommandFailed) and original.status not in {404, 422}:
                raise
            try:
                item = self._read(repo, ItemState, ["gh", "api", endpoint])
            except CommandFailed as exc:
                if exc.status == 404:
                    try:
                        self._run_command(repo, ["gh", "api", f"repos/{repo}"])
                    except ForgeError:
                        raise original from None
                    return
                raise original from None
            except ForgeError:
                raise original from None
            if item.state != "closed":
                raise original
        if item.state != "closed":
            raise ForgeError(f"{endpoint} did not close.")

    def issue_comment_records(self, repo: str, number: int) -> list[IssueComment]:
        pages = self._read(repo, IssueCommentPages, [
            "gh", "api", f"repos/{repo}/issues/{number}/comments?per_page=100", "--paginate", "--slurp",
        ])
        return [comment for page in pages.root for comment in page]

    def ready_labeled_at(self, repo: str, number: int) -> AwareDatetime | None:
        """When the issue's latest Ready label event happened, or None when it never had one."""
        pages = self._read(repo, IssueEventPages, [
            "gh", "api", f"repos/{repo}/issues/{number}/events?per_page=100", "--paginate", "--slurp",
        ])
        return max((event.created_at for page in pages.root for event in page
                    if event.event == "labeled" and event.label is not None and event.label.name == READY_LABEL), default=None)

    def marked_issues(self, repo: str, marker: str) -> list[int]:
        pages = self._read(repo, OpenIssuePages, [
            "gh", "api", f"repos/{repo}/issues?state=all&per_page=100", "--paginate", "--slurp",
        ])
        return [issue.number for page in pages.root for issue in page
                if "pull_request" not in issue.model_fields_set and marker in (issue.body or "")]

    def sub_issues(self, repo: str, number: int) -> list[LinkedIssue]:
        pages = self._read(repo, LinkedIssuePages, [
            "gh", "api", f"repos/{repo}/issues/{number}/sub_issues?per_page=100", "--paginate", "--slurp",
        ])
        return [issue for page in pages.root for issue in page]

    def parent_issue(self, repo: str, number: int) -> LinkedIssue | None:
        try:
            return self._read(repo, LinkedIssue, ["gh", "api", f"repos/{repo}/issues/{number}/parent"])
        except CommandFailed as exc:
            if str(exc) == "gh: No parent issue found (HTTP 404)":
                return None
            raise

    def add_sub_issue(self, repo: str, number: int, child: int) -> None:
        repo = redact(repo)
        identity = self._read(repo, IssueIdentity, ["gh", "api", f"repos/{repo}/issues/{child}"])
        self._run_command(repo, [
            "gh", "api", f"repos/{repo}/issues/{number}/sub_issues",
            "--method", "POST", "-F", f"sub_issue_id={identity.id}",
        ])

    def update_issue_body(self, repo: str, number: int, body: str) -> None:
        repo, body = (redact(value) for value in (repo, body))
        self._run_command(repo, [
            "gh", "api", f"repos/{repo}/issues/{number}", "--method", "PATCH", "-f", f"body={body}",
        ])

    def comment_issue(self, repo: str, number: int, body: str, *, marker: str) -> None:
        repo, body = (redact(value) for value in (repo, body))
        body = marked(body, marker)
        self._run_command(repo, [
            "gh", "api", f"repos/{repo}/issues/{number}/comments", "--method", "POST", "-f", f"body={body}",
        ])

    def upsert_comment(self, repo: str, number: int, text: str, facts: list[str], *, marker: str) -> None:
        """Converge Foreman's one marked comment: patch the first one it posted when its body differs, or post it."""
        body = status_body(text, facts, marker)
        owned = owned_comment(self.issue_comment_records(repo, number), marker)
        if owned is None:
            self._run_command(repo, ["gh", "api", f"repos/{repo}/issues/{number}/comments", "--method", "POST", "-f", f"body={body}"])
        elif owned.body != body:
            if owned.id is None:
                raise ForgeError("GitHub returned Foreman's status comment without its id.")
            self._run_command(repo, ["gh", "api", f"repos/{repo}/issues/comments/{owned.id}", "--method", "PATCH", "-f", f"body={body}"])

    def create_issue(
        self, repo: str, title: str, body: str, labels: list[str], *, marker: str, child: StoredSlice | None = None,
    ) -> int:
        repo, title, body, *labels = (redact(value) for value in (repo, title, body, *labels))
        body = marked(body, marker)
        if child is not None:
            body = render_slice_body(body, child)
        args = ["gh", "api", f"repos/{repo}/issues", "--method", "POST", "-f", f"title={title}", "-f", f"body={body}"]
        for label in labels:
            args.extend(["-f", f"labels[]={label}"])
        return self._read(repo, CreatedIssue, args, timeout=None).number

    def add_blocker(self, repo: str, number: int, blocker: int) -> None:
        repo = redact(repo)
        identity = self._read(repo, IssueIdentity, ["gh", "api", f"repos/{repo}/issues/{blocker}"])
        self._run_command(repo, [
            "gh", "api", f"repos/{repo}/issues/{number}/dependencies/blocked_by",
            "--method", "POST", "-F", f"issue_id={identity.id}",
        ])

    def blockers(self, repo: str, number: int) -> list[IssueBlocker]:
        return self._dependencies(repo, number, "blocked_by")

    def blocking(self, repo: str, number: int) -> list[IssueBlocker]:
        return self._dependencies(repo, number, "blocking")

    def _dependencies(self, repo: str, number: int, relation: str) -> list[IssueBlocker]:
        pages = self._read(repo, BlockerPages, [
            "gh", "api", f"repos/{repo}/issues/{number}/dependencies/{relation}", "--paginate", "--slurp",
        ])
        return [blocker for page in pages.root for blocker in page]

    def is_merged(self, repo: str, number: int, *, contains: Callable[[str], bool] | None = None) -> bool:
        issue = self._read(
            repo, IssueClosure, ["gh", "issue", "view", str(number), "--repo", repo, "--json",
                           "stateReason,closedByPullRequestsReferences"],
        )
        # A reopened issue is unfinished whatever its earlier pull request merged.
        if issue.stateReason == "REOPENED":
            return False
        # GitHub closes the issue after its pull request merges, so the merged pull request decides.
        for pr in issue.closedByPullRequestsReferences:
            closing_repo = f"{pr.repository.owner.login}/{pr.repository.name}"
            if contains is not None and closing_repo.lower() != repo.lower():
                continue
            fields = "state,baseRefName,mergeCommit" if contains is not None else "state,baseRefName"
            status = self._read(
                closing_repo, ClosingPullRequestStatus, ["gh", "pr", "view", str(pr.number), "--repo", closing_repo, "--json", fields],
            )
            if status.state == "MERGED":
                repository = self._read(
                    closing_repo, RepositoryDefaultBranch, ["gh", "repo", "view", closing_repo, "--json", "defaultBranchRef"],
                )
                if status.baseRefName == repository.defaultBranchRef.name and (
                    contains is None or (status.mergeCommit is not None and contains(status.mergeCommit.oid))
                ):
                    return True
        return False

    def descends(self, repo: str, ancestor: str, head: str) -> bool:
        comparison = self._read(repo, Comparison, ["gh", "api", f"repos/{repo}/compare/{ancestor}...{head}"])
        return comparison.status in ("ahead", "identical")

    def edit_labels(self, repo: str, number: int, add: list[str], remove: list[str]) -> None:
        """One edit adds and removes labels, and leaves every other label on the issue."""
        repo, *labels = (redact(value) for value in (repo, *add, *remove))
        command = ["gh", "issue", "edit", str(number), "--repo", repo]
        for flag, names in (("--add-label", labels[:len(add)]), ("--remove-label", labels[len(add):])):
            if names:
                command.extend([flag, ",".join(names)])
        self._run_command(repo, command)

    def post_review(self, repo: str, target: PullRequest, body: str, comments: list[ReviewComment], *, marker: str) -> ReviewReceipt:
        current = self._view_pr(repo, str(target.number))
        if (current.state != "OPEN" or current.baseRefName != target.baseRefName
                or current.baseRefOid != target.baseRefOid or current.headRefOid != target.headRefOid):
            raise ForgeError("The pull request changed during review.")
        endpoint = f"repos/{redact(repo)}/pulls/{target.number}/reviews"
        tag = f"<!-- foreman-review:{marker} -->"
        pages = self._read(repo, ReviewPages, ["gh", "api", endpoint, "--paginate", "--slurp"])
        for page in pages.root:
            for review in page:
                if tag in review.body:
                    try:
                        posted = PostedReview.model_validate(review.model_dump())
                    except ValidationError as exc:
                        raise ForgeError("Marked review has an invalid receipt.") from exc
                    return posted.receipt(target.headRefOid)
        payload = {
            "commit_id": target.headRefOid, "event": "COMMENT", "body": redact(body) + "\n\n" + tag,
            "comments": [{**comment, "body": redact(comment["body"]), "path": redact(comment["path"])} for comment in comments],
        }
        posted = self._read(repo, PostedReview,
            ["gh", "api", endpoint, "--method", "POST",
             "-H", "X-GitHub-Api-Version: 2022-11-28", "--input", "-"],
            body=json.dumps(payload),
        )
        return posted.receipt(target.headRefOid)

    def variables(self, repo: str) -> dict[str, str]:
        """The repository's GitHub Actions variables, which hold its merge hold and run settings."""
        try:
            variables = self._read(repo, ActionsVariables, ["gh", "variable", "list", "--repo", repo, "--json", "name,value"])
        except ForgeError as exc:
            raise VariablesUnreadable(str(exc), exc.status, rate_limited=exc.rate_limited) from exc
        return {variable.name: variable.value for variable in variables.root}

    def app_installation(self, repo: str) -> AppInstallation:
        """GitHub's answer for the App's installation on the repository and its token request.

        A failure GitHub answered, or gave no answer to, is a ForgeError. A failure of the host's own key or settings stays a
        CredentialFailure.
        """
        try:
            return "installed" if credentials.environment(repo) is not None else "unconfigured"
        except NotInstalled:
            return "not_installed"
        except CredentialFailure as exc:
            if exc.status is None and not isinstance(exc, Unanswered):
                raise
            raise ForgeError(str(exc), exc.status, rate_limited=exc.rate_limited) from None

    def app_subscription(self, repo: str) -> Subscription:
        """The App's installation on the repository, its events and permissions, read from GitHub now. A failure is a
        ForgeError, except one of the host's own key or settings."""
        try:
            return credentials.subscription(repo) or Subscription()
        except CredentialFailure as exc:
            if exc.status is None and not isinstance(exc, Unanswered):
                raise
            raise ForgeError(str(exc), exc.status, rate_limited=exc.rate_limited) from None

    def permission(self, repo: str, login: str) -> Permission | None:
        """The login's base role on the repository, or None when GitHub answers 404 because it does not know the login."""
        try:
            return self._read(repo, RepositoryPermission, [
                "gh", "api", f"repos/{repo}/collaborators/{quote(login, safe='')}/permission",
            ]).permission
        except CommandFailed as exc:
            if exc.status != 404:
                raise
            return None

    def merge_queue(self, repo: str, branch: str) -> QueuePolicy | None:
        return merge_queue_policy(self._read(repo, Rules, ["gh", "api", f"repos/{repo}/rules/branches/{quote(branch, safe='')}"]))

    def readiness(self, repo: str, issue: int, branch: str, sha: str) -> Observation:
        target = self._view_pr(repo, branch)
        endpoint = f"repos/{repo}"
        pr = self._read(repo, PullSnapshot, ["gh", "api", f"{endpoint}/pulls/{target.number}"])
        queue_policy: QueuePolicy | None = None
        def observed(outcome: Outcome, recovery: str,
                     checks: list[CheckEvidence] | None = None) -> Observation:
            return Observation(repo=repo, pr=pr, checks=checks or [], outcome=outcome, recovery=recovery, queue_policy=queue_policy)
        if pr.state != "open" and not pr.merged:
            return observed("closed", "Restore an open PR before resuming.")
        if pr.base.ref != self.default_branch(repo) or target.isCrossRepository or pr.head.ref != branch or not branch.startswith(f"{issue}-"):
            return observed("retargeted", "Restore the same-repository PR and default-branch target before resuming.")
        if pr.head.sha.lower() != sha.lower():
            return observed("head_changed", "Fetch the repaired PR head, then renew verification and review.")
        if pr.draft:
            return observed("pending", "Mark the PR ready, then resume.")
        if not pr.merged and pr.mergeable is False:
            return observed("conflict", "Foreman merges the base head into the owned branch, then verification and review run again.")
        if not pr.merged and (pr.mergeable is None or pr.mergeable_state == "unknown"):
            return observed("uncomputed", "GitHub has not computed mergeability. Add Ready again to read the PR again.")
        rules = self._read(repo, Rules, ["gh", "api", f"{endpoint}/rules/branches/{pr.base.ref}"])
        if (queue_policy := merge_queue_policy(rules)) is None:
            return observed("policy", "Effective merge queue policy is missing or ambiguous. Restore readable queue policy.")
        if pr.merged:
            return observed("ready", "Verified head has merged. Reconcile the handoff receipt.")
        legacy = self._read(repo, BranchPolicy, ["gh", "api", f"{endpoint}/branches/{pr.base.ref}"])
        requirements = [check for rule in rules.root if rule.type == "required_status_checks"
                        for check in rule.parameters.required_status_checks]
        classic = legacy.protection.required_status_checks
        if set(classic.contexts) - {check.context for check in classic.checks}:
            return observed("policy", "Legacy policy lacks required app identity. Restore readable policy.")
        requirements.extend(classic.checks)
        if not requirements:
            return observed("policy", "Effective required-check policy is empty. Restore readable policy.")
        commits = [pr.head.sha]
        if pr.merge_commit_sha:
            merge = self._read(repo, Commit, ["gh", "api", f"{endpoint}/commits/{pr.merge_commit_sha}"])
            if {parent.sha for parent in merge.parents} == {pr.head.sha, legacy.commit.sha}:
                commits.insert(0, pr.merge_commit_sha)
        evidence: list[CheckEvidence] = []
        for commit in commits:
            runs = self._read(repo, CheckPages, ["gh", "api", f"{endpoint}/commits/{commit}/check-runs?filter=latest&per_page=100", "--paginate", "--slurp"])
            statuses = self._read(repo, StatusPages, ["gh", "api", f"{endpoint}/commits/{commit}/statuses?per_page=100", "--paginate", "--slurp"])
            for page in runs.root:
                evidence.extend(CheckEvidence(context=run.name, app=run.app.id, run=run.id,
                    source_commit=commit, suite=run.check_suite.id, tested_commit=commit if commit != pr.head.sha else None, state=(run.conclusion or "pending") if run.status == "completed" else "pending")
                    for run in page.check_runs if run.head_sha == commit)
            evidence.extend(CheckEvidence(context=status.context, app=None, run=status.id,
                source_commit=commit, state=status.state) for page in statuses.root for status in page)
        effective: list[CheckEvidence] = []
        for required in requirements:
            candidates = [check for check in evidence if check.context == required.context
                          and (required.integration_id in (None, -1) or check.app == required.integration_id)]
            if len({check.app for check in candidates}) > 1:
                return observed("policy", "Ambiguous required-check app identity. Bind the policy to an app.", evidence)
            tested = next((commit for commit in commits if any(c.source_commit == commit for c in candidates)), pr.head.sha)
            latest = [c for c in candidates if c.source_commit == tested]
            effective.append(max(latest, key=lambda c: c.run or 0) if latest else CheckEvidence(
                context=required.context, app=required.integration_id, run=None, source_commit=tested, state="missing"))
        current = self._read(repo, PullSnapshot, ["gh", "api", f"{endpoint}/pulls/{target.number}"])
        if current != pr:
            return observed("pending", "PR changed during observation. Resume with fresh evidence.", effective)
        if any(c.state not in ("success", "pending", "missing") for c in effective):
            return observed("red", "Repair failing required checks, then resume.", effective)
        if any(c.state != "success" for c in effective):
            return observed("pending", "Wait for required checks, then resume.", effective)
        return observed("ready", "Eligible for merge arming.", effective)

    def arm_auto_merge(self, repo: str, issue: int, branch: str, sha: str, observation: Observation) -> MergeReceipt:
        try:
            return self._arm_auto_merge(repo, issue, branch, sha, observation)
        except MergeNotArmed:
            raise
        except ForgeError as exc:
            raise MergeNotArmed(str(exc)) from exc

    def review_target(self, repo: str, issue: int, branch: str, sha: str) -> PullRequest:
        pr = self._view_pr(repo, branch)
        if pr.state != "OPEN":
            raise ForgeError("The pull request is not open.")
        if pr.headRefOid.lower() != sha.lower():
            raise ForgeError("The pull request head differs from the verified SHA.")
        if not pr.headRefName.startswith(f"{issue}-"):
            raise ForgeError(f"The pull request head branch must start with {issue}-.")
        if pr.baseRefName != self.default_branch(repo) or pr.isCrossRepository:
            raise ForgeError("The pull request must target the repository default branch.")
        return pr

    def _queue_snapshot(self, repo: str, number: int) -> QueueSnapshot:
        owner, name = repo.split("/", 1)
        query = """query($owner:String!, $name:String!, $number:Int!) {
          repository(owner:$owner, name:$name) { pullRequest(number:$number) {
            number state headRefOid baseRefOid baseRefName isDraft
            autoMergeRequest { enabledAt mergeMethod }
            mergeQueueEntry { id state }
          } }
        }"""
        return self._read(repo, QueueSnapshot, ["gh", "api", "graphql", "-f", f"query={query}",
            "-f", f"owner={owner}", "-f", f"name={name}", "-F", f"number={number}",
            "--jq", ".data.repository.pullRequest"])

    def reconcile_merge(self, receipt: MergeReceipt) -> MergeReceipt:
        try:
            confirmed = queue_receipt(self._queue_snapshot(receipt.repo, receipt.pr), receipt.repo, receipt.pr,
                                      receipt.source_head, receipt.base, receipt.base_ref, receipt.queue_policy)
            if confirmed is None:
                raise QueueRejected(f"Queue receipt {receipt.identity} disappeared. Inspect rejection and repair before a new admission.")
            return confirmed
        except MergeNotArmed:
            raise
        except ForgeError as exc:
            raise MergeNotArmed(str(exc)) from exc

    def _arm_auto_merge(self, repo: str, issue: int, branch: str, sha: str, observation: Observation) -> MergeReceipt:
        head = commit_sha(sha)
        if head is None:
            raise MergeNotArmed("Merge arming requires a full 40-hex commit SHA.")
        policy, pr = observation.queue_policy, observation.pr
        if policy is None or observation.outcome != "ready" or observation.repo != repo or pr.head.sha.lower() != head or pr.head.ref != branch or not branch.startswith(f"{issue}-"):
            raise MergeNotArmed("Merge requires a ready observation for this PR and head.")
        existing = queue_receipt(self._queue_snapshot(repo, pr.number), repo, pr.number, head, pr.base.sha, pr.base.ref, policy)
        if existing is not None:
            return existing
        timeout: CommandTimedOut | None = None
        try:
            self._run_command(repo, merge_command(redact(repo), str(pr.number), head))
        except CommandTimedOut as exc:
            timeout = exc
        confirmed = queue_receipt(self._queue_snapshot(repo, pr.number), repo, pr.number, head, pr.base.sha, pr.base.ref, policy)
        if confirmed is None:
            raise MergeNotArmed("Merge handoff was not confirmed. Inspect queue admission and resume.") from timeout
        return confirmed

    def _view_pr(self, repo: str, selector: str) -> PullRequest:
        command = [
            "gh", "pr", "view", "--repo", repo,
            "--json", "number,state,headRefOid,headRefName,baseRefName,baseRefOid,isCrossRepository,autoMergeRequest", "--", selector,
        ]
        return self._read(repo, PullRequest, command)

    def _run_command(self, repo: str, command: list[str], *, timeout: float | None = COMMAND_TIMEOUT, body: str | None = None) -> str:
        operation = " ".join(command[:2] if command[1] == "api" else command[:3])
        try:
            env = credentials.environment(repo)
            if env is not None:
                timeout = min(timeout or COMMAND_TIMEOUT, COMMAND_TIMEOUT)
        except CredentialFailure as exc:
            raise CommandFailed(str(exc), exc.status, rate_limited=exc.rate_limited) from None
        try:
            result = subprocess.run(
                command, env=env, stdin=subprocess.DEVNULL if body is None else None, input=body,
                capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise CommandTimedOut(operation) from exc
        except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
            raise CommandFailed(redact(f"{operation} failed ({exc}).", credentials_only=True)) from None
        if result.returncode:
            match = re.search(r"HTTP (\d{3})", result.stderr)
            status = int(match[1]) if match else None
            rate_limited = "rate limit" in result.stderr.lower()
            if env is not None and status:
                if status == 401:
                    credentials.invalidate()
                raise CommandFailed(failure(status, rate_limited), status, rate_limited=rate_limited)
            raise CommandFailed(redact(result.stderr.strip(), credentials_only=True) or f"{operation} exited with code {result.returncode}.",
                                status, rate_limited=rate_limited)
        return result.stdout

    def _read(self, repo: str, model: type[Model], command: list[str], *, timeout: float | None = COMMAND_TIMEOUT, body: str | None = None) -> Model:
        output = self._run_command(repo, command, timeout=timeout, body=body)
        try:
            return model.model_validate_json(output)
        except ValidationError as exc:
            raise ForgeError(f"Invalid {model.__name__} response ({exc}).") from exc
