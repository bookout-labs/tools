"""GitHub operations for delivery runs."""

import json
import subprocess
from typing import Literal, Protocol, TypedDict, TypeVar

from pydantic import BaseModel, PositiveInt, ValidationError

from foreman.redaction import redact
from foreman.stage_output import Slice
from foreman.workspace import commit_sha

SQUASH_MERGE_FLAG = "--squash"
PullRequestState = Literal["OPEN", "CLOSED", "MERGED"]
Model = TypeVar("Model", bound=BaseModel)


class ForgeError(RuntimeError):
    pass


class MergeNotArmed(ForgeError):
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


class Issue(TypedDict):
    title: str
    body: str
    labels: list[str]


class Forge(Protocol):
    def load_issue(self, repo: str, number: int) -> Issue: ...
    def create_issue(
        self, repo: str, title: str, body: str, labels: list[str], *, child: Slice | None = None,
    ) -> int: ...
    def is_merged(self, repo: str, number: int) -> bool: ...
    def add_label(self, repo: str, number: int, label: str) -> None: ...
    def arm_auto_merge(self, repo: str, issue: int, branch: str, sha: str) -> None: ...


def merge_command(repo: str, pull_request: str, sha: str) -> list[str]:
    return ["gh", "pr", "merge", pull_request, "--repo", repo, SQUASH_MERGE_FLAG, "--auto", "--match-head-commit", sha]


def render_slice_body(prose: str, child: Slice) -> str:
    dependencies = "\n".join(f"- {title}" for title in child.depends_on) or "None"
    constraints = "\n".join(f"- {prop.constraint}" for prop in child.properties) or "None"
    return (
        (f"{prose}\n\n" if prose else "")
        + f"Acceptance:\n{child.acceptance}\n\nDependencies:\n{dependencies}"
        + f"\n\nScreen:\n{child.screen or 'None'}\n\nConstraints:\n{constraints}"
        + f"\n\n```json\n{json.dumps(child.model_dump(), indent=2)}\n```"
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

    def arm_auto_merge(self, repo: str, issue: int, branch: str, sha: str) -> None:
        try:
            self._arm_auto_merge(repo, issue, branch, sha)
        except MergeNotArmed:
            raise
        except ForgeError as exc:
            raise MergeNotArmed(str(exc)) from exc

    def _arm_auto_merge(self, repo: str, issue: int, branch: str, sha: str) -> None:
        head = commit_sha(sha)
        if head is None:
            raise MergeNotArmed("Merge arming requires a full 40-hex commit SHA.")
        pr = self._view_pr(repo, branch)
        if pr.state != "OPEN":
            raise MergeNotArmed("The pull request is not open.")
        if pr.headRefOid.lower() != head.lower():
            raise MergeNotArmed("The pull request head differs from the verified SHA.")
        if not pr.headRefName.startswith(f"{issue}-"):
            raise MergeNotArmed(f"The pull request head branch must start with {issue}-.")
        if pr.baseRefName != "main":
            raise MergeNotArmed("The pull request must target main.")
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
            "--json", "number,state,headRefOid,headRefName,baseRefName,autoMergeRequest", "--", selector,
        ]
        return self._read(PullRequest, command)

    def _run_command(self, command: list[str], *, timeout: float | None = 30) -> str:
        operation = " ".join(command[:2] if command[1] == "api" else command[:3])
        try:
            result = subprocess.run(
                command, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout,
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
