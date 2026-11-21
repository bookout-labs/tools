"""GitHub operations for delivery runs."""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING, Literal, Protocol, TypeVar
from typing_extensions import TypedDict

from pydantic import BaseModel, ConfigDict, JsonValue, PositiveInt, RootModel, ValidationError, with_config

from foreman.redaction import redact
if TYPE_CHECKING:
    from foreman.stage_output import StoredSlice
from foreman.workspace import commit_sha

SQUASH_MERGE_FLAG = "--squash"
MERGE_HOLD_VARIABLE = "FOREMAN_MERGE_HOLD"
PullRequestState = Literal["OPEN", "CLOSED", "MERGED"]
Model = TypeVar("Model", bound=BaseModel)


class ForgeError(RuntimeError):
    pass


class MergeNotArmed(ForgeError):
    pass


class MergeHoldUnreadable(ForgeError):
    pass


class CommandFailed(ForgeError):
    pass


class CommandTimedOut(ForgeError):
    def __init__(self, operation: str):
        self.operation = operation
        super().__init__(f"{operation} timed out.")


class PullRequestStatus(BaseModel, strict=True):
    state: PullRequestState


class ClosingPullRequestStatus(PullRequestStatus):
    baseRefName: str


class DefaultBranch(BaseModel, strict=True):
    name: str


class RepositoryDefaultBranch(BaseModel, strict=True):
    defaultBranchRef: DefaultBranch


class AutoMergeRequest(BaseModel, strict=True):
    mergeMethod: Literal["MERGE", "REBASE", "SQUASH"]


@with_config(ConfigDict(extra="forbid"))
class AutoMergeRequestRecord(TypedDict):
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


class IssueLabel(BaseModel, strict=True):
    name: str


class IssueView(BaseModel, strict=True):
    title: str
    body: str | None
    labels: list[IssueLabel]


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
    closed: bool
    closedByPullRequestsReferences: list[ClosingPullRequest]


class CreatedIssue(BaseModel, strict=True):
    number: PositiveInt


class OpenIssue(BaseModel, strict=True):
    number: PositiveInt
    body: str


class IssueComment(BaseModel, strict=True):
    body: str | None


class RepositoryIssue(IssueComment):
    number: PositiveInt
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
        return self.repository_url.lower() == f"https://api.github.com/repos/{repo.lower()}"


class BlockerPages(RootModel[list[list[IssueBlocker]]]):
    pass


class Issue(TypedDict):
    title: str
    body: str
    labels: list[str]


class ReviewComment(TypedDict):
    path: str
    line: int
    side: Literal["RIGHT"]
    body: str


class Forge(Protocol):
    def default_branch(self, repo: str) -> str: ...
    def open_pull_request(self, repo: str, head: str, base: str, title: str, body: str) -> None: ...
    def load_issue(self, repo: str, number: int) -> Issue: ...
    def open_issues(self, repo: str) -> list[OpenIssue]: ...
    def issue_comments(self, repo: str, number: int) -> list[str]: ...
    def comment_issue(self, repo: str, number: int, body: str) -> None: ...
    def create_issue(
        self, repo: str, title: str, body: str, labels: list[str], *, child: StoredSlice | None = None,
    ) -> int: ...
    def add_blocker(self, repo: str, number: int, blocker: int) -> None: ...
    def blockers(self, repo: str, number: int) -> list[IssueBlocker]: ...
    def is_merged(self, repo: str, number: int) -> bool: ...
    def add_label(self, repo: str, number: int, label: str) -> None: ...
    def pull_request_base(self, repo: str, branch: str) -> str: ...
    def review_target(self, repo: str, issue: int, branch: str, sha: str) -> PullRequest: ...
    def post_review(self, repo: str, target: PullRequest, body: str, comments: list[ReviewComment]) -> None: ...
    def merge_hold(self, repo: str) -> str | None: ...
    def arm_auto_merge(self, repo: str, issue: int, branch: str, sha: str) -> None: ...


def pull_request_command(repo: str, head: str, base: str, title: str, body: str) -> list[str]:
    return ["gh", "pr", "create", "--repo", redact(repo), "--base", redact(base), "--head", redact(head),
            "--title", redact(" ".join(title.split())), "--body", redact(body)]


def merge_command(repo: str, pull_request: str, sha: str) -> list[str]:
    return ["gh", "pr", "merge", pull_request, "--repo", repo, SQUASH_MERGE_FLAG, "--auto", "--match-head-commit", sha]


def render_slice_body(prose: str, child: StoredSlice) -> str:
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


class LiveForge:
    def __init__(self):
        self._defaults: dict[str, str] = {}

    def default_branch(self, repo: str) -> str:
        if repo not in self._defaults:
            self._defaults[repo] = self._read(
                RepositoryDefaultBranch, ["gh", "repo", "view", repo, "--json", "defaultBranchRef"],
            ).defaultBranchRef.name
        return self._defaults[repo]

    def open_pull_request(self, repo: str, head: str, base: str, title: str, body: str) -> None:
        def exists() -> bool:
            try:
                pr = self._view_pr(repo, head)
            except CommandFailed as exc:
                if "no pull requests found for branch" in str(exc):
                    return False
                raise
            if pr.state != "OPEN" or pr.headRefName != head or pr.baseRefName != base or pr.isCrossRepository:
                raise ForgeError("The branch pull request must be open on the selected base in this repository.")
            return True

        if exists():
            return
        try:
            self._run_command(pull_request_command(repo, head, base, title, body))
        except ForgeError:
            if not exists():
                raise

    def load_issue(self, repo: str, number: int) -> Issue:
        issue = self._read(
            IssueView, ["gh", "issue", "view", str(number), "--repo", repo, "--json", "title,body,labels"],
        )
        return {"title": issue.title, "body": issue.body or "", "labels": [label.name for label in issue.labels]}

    def open_issues(self, repo: str) -> list[OpenIssue]:
        pages = self._read(OpenIssuePages, [
            "gh", "api", f"repos/{repo}/issues?state=open&per_page=100", "--paginate", "--slurp",
        ])
        issues = [
            OpenIssue(number=issue.number, body=issue.body or "")
            for page in pages.root for issue in page if "pull_request" not in issue.model_fields_set
        ]
        if not issues:
            raise ForgeError("No open issues returned.")
        return issues

    def issue_comments(self, repo: str, number: int) -> list[str]:
        pages = self._read(IssueCommentPages, [
            "gh", "api", f"repos/{repo}/issues/{number}/comments?per_page=100", "--paginate", "--slurp",
        ])
        comments = [comment.body or "" for page in pages.root for comment in page]
        if not comments:
            raise ForgeError("No issue comments returned.")
        return comments

    def comment_issue(self, repo: str, number: int, body: str) -> None:
        repo, body = (redact(value) for value in (repo, body))
        self._run_command([
            "gh", "api", f"repos/{repo}/issues/{number}/comments", "--method", "POST", "-f", f"body={body}",
        ])

    def create_issue(
        self, repo: str, title: str, body: str, labels: list[str], *, child: StoredSlice | None = None,
    ) -> int:
        repo, title, body, *labels = (redact(value) for value in (repo, title, body, *labels))
        if child is not None:
            child = self._redact_slice(child)
            body = render_slice_body(body, child)
        args = ["gh", "api", f"repos/{repo}/issues", "--method", "POST", "-f", f"title={title}", "-f", f"body={body}"]
        for label in labels:
            args.extend(["-f", f"labels[]={label}"])
        return self._read(CreatedIssue, args, timeout=None).number

    def add_blocker(self, repo: str, number: int, blocker: int) -> None:
        repo = redact(repo)
        identity = self._read(IssueIdentity, ["gh", "api", f"repos/{repo}/issues/{blocker}"])
        self._run_command([
            "gh", "api", f"repos/{repo}/issues/{number}/dependencies/blocked_by",
            "--method", "POST", "-F", f"issue_id={identity.id}",
        ])

    def blockers(self, repo: str, number: int) -> list[IssueBlocker]:
        pages = self._read(BlockerPages, [
            "gh", "api", f"repos/{repo}/issues/{number}/dependencies/blocked_by", "--paginate", "--slurp",
        ])
        return [blocker for page in pages.root for blocker in page]

    @staticmethod
    def _redact_slice(child: StoredSlice) -> StoredSlice:
        screen_name = child.screen.removeprefix("docs/screens/").removesuffix(".md")
        return child.model_copy(update={
            "builds": [seam.model_copy(update={"name": redact(seam.name), "path": redact(seam.path) if seam.path else None}) for seam in child.builds],
            "uses": [seam.model_copy(update={"name": redact(seam.name), "path": redact(seam.path) if seam.path else None}) for seam in child.uses],
            "title": redact(child.title),
            "acceptance": redact(child.acceptance),
            "depends_on": [redact(title) for title in child.depends_on],
            "screen": f"docs/screens/{redact(screen_name)}.md" if child.screen else "",
            "properties": [
                prop.model_copy(update={"name": redact(prop.name), "constraint": redact(prop.constraint)})
                for prop in child.properties
            ],
        })

    def is_merged(self, repo: str, number: int) -> bool:
        issue = self._read(
            IssueClosure, ["gh", "issue", "view", str(number), "--repo", repo, "--json",
                           "closed,closedByPullRequestsReferences"],
        )
        if not issue.closed:
            return False
        for pr in issue.closedByPullRequestsReferences:
            closing_repo = f"{pr.repository.owner.login}/{pr.repository.name}"
            status = self._read(
                ClosingPullRequestStatus, ["gh", "pr", "view", str(pr.number), "--repo", closing_repo, "--json", "state,baseRefName"],
            )
            if status.state == "MERGED":
                repository = self._read(
                    RepositoryDefaultBranch, ["gh", "repo", "view", closing_repo, "--json", "defaultBranchRef"],
                )
                if status.baseRefName == repository.defaultBranchRef.name:
                    return True
        return False

    def add_label(self, repo: str, number: int, label: str) -> None:
        repo, label = (redact(value) for value in (repo, label))
        self._run_command(["gh", "issue", "edit", str(number), "--repo", repo, "--add-label", label])

    def pull_request_base(self, repo: str, branch: str) -> str:
        return self._view_pr(repo, branch).baseRefName

    def post_review(self, repo: str, target: PullRequest, body: str, comments: list[ReviewComment]) -> None:
        current = self._view_pr(repo, str(target.number))
        if current.state != "OPEN" or current.baseRefName != target.baseRefName:
            raise ForgeError("The pull request changed during review.")
        payload = {
            "commit_id": target.headRefOid, "event": "COMMENT", "body": redact(body),
            "comments": [{**comment, "body": redact(comment["body"]), "path": redact(comment["path"])} for comment in comments],
        }
        repo = redact(repo)
        self._run_command(
            ["gh", "api", f"repos/{repo}/pulls/{target.number}/reviews", "--method", "POST",
             "-H", "X-GitHub-Api-Version: 2022-11-28", "--input", "-"],
            body=json.dumps(payload),
        )

    def merge_hold(self, repo: str) -> str | None:
        try:
            variables = self._read(ActionsVariables, ["gh", "variable", "list", "--repo", repo, "--json", "name,value"])
        except ForgeError as exc:
            raise MergeHoldUnreadable(str(exc)) from exc
        return next((variable.value for variable in variables.root if variable.name == MERGE_HOLD_VARIABLE), None)

    def arm_auto_merge(self, repo: str, issue: int, branch: str, sha: str) -> None:
        try:
            self._arm_auto_merge(repo, issue, branch, sha)
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

    def _arm_auto_merge(self, repo: str, issue: int, branch: str, sha: str) -> None:
        head = commit_sha(sha)
        if head is None:
            raise MergeNotArmed("Merge arming requires a full 40-hex commit SHA.")
        pr = self.review_target(repo, issue, branch, head)
        command = merge_command(redact(repo), str(pr.number), head)
        try:
            self._run_command(command)
        except CommandTimedOut as exc:
            confirmed = self._view_pr(repo, str(pr.number))
            open_squash_request = (
                confirmed.state == "OPEN"
                and confirmed.autoMergeRequest is not None
                and confirmed.autoMergeRequest.mergeMethod == "SQUASH"
            )
            same_head = confirmed.number == pr.number and confirmed.headRefOid.lower() == head.lower()
            if not (same_head and (confirmed.state == "MERGED" or open_squash_request)):
                raise MergeNotArmed("Merge arming timed out and was not confirmed.") from exc

    def _view_pr(self, repo: str, selector: str) -> PullRequest:
        command = [
            "gh", "pr", "view", "--repo", repo,
            "--json", "number,state,headRefOid,headRefName,baseRefName,baseRefOid,isCrossRepository,autoMergeRequest", "--", selector,
        ]
        return self._read(PullRequest, command)

    def _run_command(self, command: list[str], *, timeout: float | None = 30, body: str | None = None) -> str:
        operation = " ".join(command[:2] if command[1] == "api" else command[:3])
        try:
            result = subprocess.run(
                command, stdin=subprocess.DEVNULL if body is None else None, input=body,
                capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise CommandTimedOut(operation) from exc
        except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
            raise CommandFailed(f"{operation} failed ({exc}).") from exc
        if result.returncode:
            raise CommandFailed(result.stderr.strip() or f"{operation} exited with code {result.returncode}.")
        return result.stdout

    def _read(self, model: type[Model], command: list[str], *, timeout: float | None = 30) -> Model:
        output = self._run_command(command, timeout=timeout)
        try:
            return model.model_validate_json(output)
        except ValidationError as exc:
            raise ForgeError(f"Invalid {model.__name__} response ({exc}).") from exc
