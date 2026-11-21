"""GitHub operations for delivery runs."""

import json
import subprocess
from typing import Literal, Protocol, TypedDict, TypeVar

from pydantic import BaseModel, PositiveInt, RootModel, ValidationError

from foreman.redaction import redact
from foreman.stage_output import Slice, StoredSlice
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


class AutoMergeRequest(BaseModel, strict=True):
    mergeMethod: Literal["MERGE", "REBASE", "SQUASH"]


class PullRequest(PullRequestStatus):
    number: PositiveInt
    headRefOid: str
    headRefName: str
    baseRefName: str
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


class ActionsVariable(BaseModel, strict=True):
    name: str
    value: str


class ActionsVariables(RootModel[list[ActionsVariable]], strict=True):
    # gh variable list --json name,value, v2.102.0:
    # https://github.com/cli/cli/blob/v2.102.0/pkg/cmd/variable/list/list_test.go#L112-L122
    # gh fetches every page with per_page=100 and RESTWithNext.
    # https://github.com/cli/cli/blob/v2.102.0/pkg/cmd/variable/list/list.go#L237-L250
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
    def load_issue(self, repo: str, number: int) -> Issue: ...
    def create_issue(
        self, repo: str, title: str, body: str, labels: list[str], *, child: Slice | None = None,
    ) -> int: ...
    def is_merged(self, repo: str, number: int) -> bool: ...
    def add_label(self, repo: str, number: int, label: str) -> None: ...
    def review_target(self, repo: str, issue: int, branch: str, sha: str) -> PullRequest: ...
    def post_review(self, repo: str, target: PullRequest, body: str, comments: list[ReviewComment]) -> None: ...
    def merge_hold(self, repo: str) -> str | None: ...
    def arm_auto_merge(self, repo: str, issue: int, branch: str, sha: str) -> None: ...


def merge_command(repo: str, pull_request: str, sha: str) -> list[str]:
    return ["gh", "pr", "merge", pull_request, "--repo", repo, SQUASH_MERGE_FLAG, "--auto", "--match-head-commit", sha]


def render_slice_body(prose: str, child: Slice) -> str:
    dependencies = "\n".join(f"- {title}" for title in child.depends_on) or "None"
    constraints = "\n".join(f"- {prop.constraint}" for prop in child.properties) or "None"
    stored = child.model_dump()
    if isinstance(child, StoredSlice) and child.work_kind is not None:
        stored["work_kind"] = child.work_kind
    return (
        (f"{prose}\n\n" if prose else "")
        + f"Acceptance:\n{child.acceptance}\n\nDependencies:\n{dependencies}"
        + f"\n\nScreen:\n{child.screen or 'None'}\n\nConstraints:\n{constraints}"
        + f"\n\n```json\n{json.dumps(stored, indent=2)}\n```"
    )


class LiveForge:
    def load_issue(self, repo: str, number: int) -> Issue:
        issue = self._read(
            IssueView, ["gh", "issue", "view", str(number), "--repo", repo, "--json", "title,body,labels"],
        )
        return {"title": issue.title, "body": issue.body or "", "labels": [label.name for label in issue.labels]}

    def create_issue(
        self, repo: str, title: str, body: str, labels: list[str], *, child: Slice | None = None,
    ) -> int:
        repo, title, body, *labels = (redact(value) for value in (repo, title, body, *labels))
        if child is not None:
            child = self._redact_slice(child)
            body = render_slice_body(body, child)
        args = ["gh", "api", f"repos/{repo}/issues", "--method", "POST", "-f", f"title={title}", "-f", f"body={body}"]
        for label in labels:
            args.extend(["-f", f"labels[]={label}"])
        return self._read(CreatedIssue, args, timeout=None).number

    @staticmethod
    def _redact_slice(child: Slice) -> Slice:
        screen_name = child.screen.removeprefix("docs/screens/").removesuffix(".md")
        return child.model_copy(update={
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
                PullRequestStatus, ["gh", "pr", "view", str(pr.number), "--repo", closing_repo, "--json", "state"],
            )
            if status.state == "MERGED":
                return True
        return False

    def add_label(self, repo: str, number: int, label: str) -> None:
        repo, label = (redact(value) for value in (repo, label))
        self._run_command(["gh", "issue", "edit", str(number), "--repo", repo, "--add-label", label])

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
        if pr.baseRefName != "main":
            raise ForgeError("The pull request must target main.")
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
            "--json", "number,state,headRefOid,headRefName,baseRefName,baseRefOid,autoMergeRequest", "--", selector,
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
