"""GitHub operations for delivery runs."""

from datetime import UTC, datetime
import subprocess
from typing import Literal, Protocol, TypedDict, TypeVar

from pydantic import AwareDatetime, BaseModel, PositiveInt, ValidationError

SQUASH_MERGE_FLAG = "--squash"
PullRequestState = Literal["OPEN", "CLOSED", "MERGED"]
Model = TypeVar("Model", bound=BaseModel)


class ForgeError(RuntimeError):
    pass


class MergeNotArmed(ForgeError):
    pass


class CommandTimedOut(MergeNotArmed):
    def __init__(self, operation: str):
        self.operation = operation
        super().__init__(f"{operation} timed out.")


class PullRequestStatus(BaseModel, strict=True):
    state: PullRequestState


class AutoMergeRequest(BaseModel, strict=True):
    enabledAt: AwareDatetime
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


class ClosingPullRequest(BaseModel, strict=True):
    number: PositiveInt
    state: PullRequestState | None = None


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
    def create_issue(self, repo: str, title: str, body: str, labels: list[str]) -> int: ...
    def is_merged(self, repo: str, number: int) -> bool: ...
    def add_label(self, repo: str, number: int, label: str) -> None: ...
    def arm_auto_merge(self, repo: str, issue: int, branch: str, sha: str) -> None: ...


def merge_command(repo: str, pull_request: str, sha: str) -> list[str]:
    return ["gh", "pr", "merge", pull_request, "--repo", repo, SQUASH_MERGE_FLAG, "--auto", "--match-head-commit", sha]


class LiveForge:
    def load_issue(self, repo: str, number: int) -> Issue:
        issue = self._read_issue_response(
            IssueView, ["gh", "issue", "view", str(number), "--repo", repo, "--json", "title,body,labels"],
        )
        return {"title": issue.title, "body": issue.body or "", "labels": [label.name for label in issue.labels]}

    def create_issue(self, repo: str, title: str, body: str, labels: list[str]) -> int:
        args = ["gh", "api", f"repos/{repo}/issues", "--method", "POST", "-f", f"title={title}", "-f", f"body={body}"]
        for label in labels:
            args.extend(["-f", f"labels[]={label}"])
        return self._read_issue_response(CreatedIssue, args).number

    def is_merged(self, repo: str, number: int) -> bool:
        issue = self._read_issue_response(
            IssueClosure, ["gh", "issue", "view", str(number), "--repo", repo, "--json",
                           "closed,closedByPullRequestsReferences"],
        )
        if not issue.closed:
            return False
        for pr in issue.closedByPullRequestsReferences:
            state = pr.state
            if state is None:
                state = self._read_issue_response(
                    PullRequestStatus, ["gh", "pr", "view", str(pr.number), "--repo", repo, "--json", "state"],
                ).state
            if state == "MERGED":
                return True
        return False

    def add_label(self, repo: str, number: int, label: str) -> None:
        self._run_issue_command(["gh", "issue", "edit", str(number), "--repo", repo, "--add-label", label])

    def arm_auto_merge(self, repo: str, issue: int, branch: str, sha: str) -> None:
        pr = self._view_pr(repo, branch)
        if pr.state != "OPEN":
            raise MergeNotArmed("The pull request is not open.")
        if pr.headRefOid.lower() != sha.lower():
            raise MergeNotArmed("The pull request head differs from the verified SHA.")
        if not pr.headRefName.startswith(f"{issue}-"):
            raise MergeNotArmed(f"The pull request head branch must start with {issue}-.")
        if pr.baseRefName != "main":
            raise MergeNotArmed("The pull request must target main.")
        command = merge_command(repo, str(pr.number), sha)
        started_at = datetime.now(UTC)
        try:
            self._run_command(command)
        except CommandTimedOut as exc:
            try:
                confirmed = self._view_pr(repo, str(pr.number))
            except MergeNotArmed as confirmation_error:
                raise MergeNotArmed(
                    f"Merge arming timed out and was not confirmed ({confirmation_error})."
                ) from exc
            if (
                pr.autoMergeRequest is not None
                or confirmed.number != pr.number
                or confirmed.headRefOid.lower() != sha.lower()
                or confirmed.autoMergeRequest is None
                or confirmed.autoMergeRequest.mergeMethod != "SQUASH"
                or confirmed.autoMergeRequest.enabledAt < started_at
            ):
                raise MergeNotArmed("Merge arming timed out and was not confirmed.") from exc

    def _view_pr(self, repo: str, selector: str) -> PullRequest:
        command = [
            "gh", "pr", "view", "--repo", repo,
            "--json", "number,state,headRefOid,headRefName,baseRefName,autoMergeRequest", "--", selector,
        ]
        try:
            return PullRequest.model_validate_json(self._run_command(command))
        except ValidationError as exc:
            raise MergeNotArmed(f"Invalid response from gh pr view ({exc}).") from exc

    def _run_command(self, command: list[str]) -> str:
        operation = " ".join(command[:2] if command[1] == "api" else command[:3])
        try:
            result = subprocess.run(
                command, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30,
            )
        except subprocess.TimeoutExpired as exc:
            raise CommandTimedOut(operation) from exc
        except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
            raise MergeNotArmed(f"{operation} failed ({exc}).") from exc
        if result.returncode:
            raise MergeNotArmed(result.stderr.strip() or f"{operation} exited with code {result.returncode}.")
        return result.stdout

    def _run_issue_command(self, command: list[str]) -> str:
        try:
            return self._run_command(command)
        except MergeNotArmed as exc:
            raise ForgeError(str(exc)) from exc

    def _read_issue_response(self, model: type[Model], command: list[str]) -> Model:
        output = self._run_issue_command(command)
        try:
            return model.model_validate_json(output)
        except ValidationError as exc:
            raise ForgeError(f"Invalid {model.__name__} response ({exc}).") from exc
