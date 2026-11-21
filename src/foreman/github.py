"""GitHub operations for delivery runs."""

from copy import deepcopy
from pathlib import Path
import subprocess
from typing import Literal, Protocol, TypedDict

from pydantic import BaseModel, PositiveInt, ValidationError

SQUASH_MERGE_FLAG = "--squash"
PullRequestState = Literal["OPEN", "CLOSED", "MERGED"]
MergeOutcome = Literal["armed", "skipped"]


class MergeNotArmed(RuntimeError):
    pass


class PullRequestStatus(BaseModel, strict=True):
    state: PullRequestState


class AutoMergeRequest(BaseModel, strict=True):
    enabledAt: str
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


class Issue(TypedDict):
    title: str
    body: str
    labels: list[str]


class Forge(Protocol):
    def load_issue(self, repo: str, number: int) -> Issue: ...
    def create_issue(self, repo: str, title: str, body: str, labels: list[str]) -> int: ...
    def is_merged(self, repo: str, number: int) -> bool: ...
    def add_label(self, repo: str, number: int, label: str) -> None: ...
    def arm_auto_merge(self, repo: str, issue: int | None, sha: str | None) -> MergeOutcome: ...


def _merge_command(repo: str, pull_request: str, sha: str) -> list[str]:
    return ["gh", "pr", "merge", pull_request, "--repo", repo, SQUASH_MERGE_FLAG, "--auto", "--match-head-commit", sha]


class LiveForge:
    def __init__(self, cwd: Path | None = None):
        self.cwd = cwd

    def load_issue(self, repo: str, number: int) -> Issue:
        output = subprocess.check_output(
            ["gh", "issue", "view", str(number), "--repo", repo, "--json", "title,body,labels"], text=True
        )
        issue = IssueView.model_validate_json(output)
        return {"title": issue.title, "body": issue.body or "", "labels": [label.name for label in issue.labels]}

    def create_issue(self, repo: str, title: str, body: str, labels: list[str]) -> int:
        args = ["gh", "issue", "create", "--repo", repo, "--title", title, "--body", body]
        for label in labels:
            args.extend(["--label", label])
        url = subprocess.check_output(args, text=True).strip()
        return int(url.rsplit("/", 1)[-1])

    def is_merged(self, repo: str, number: int) -> bool:
        issue = IssueClosure.model_validate_json(subprocess.check_output(
            ["gh", "issue", "view", str(number), "--repo", repo, "--json",
             "closed,closedByPullRequestsReferences"], text=True
        ))
        if not issue.closed:
            return False
        for pr in issue.closedByPullRequestsReferences:
            state = pr.state
            if state is None:
                state = PullRequestStatus.model_validate_json(subprocess.check_output(
                    ["gh", "pr", "view", str(pr.number), "--repo", repo, "--json", "state"], text=True
                )).state
            if state == "MERGED":
                return True
        return False

    def add_label(self, repo: str, number: int, label: str) -> None:
        subprocess.check_output(["gh", "issue", "edit", str(number), "--repo", repo, "--add-label", label], text=True)

    def arm_auto_merge(self, repo: str, issue: int | None, sha: str | None) -> MergeOutcome:
        if issue is None or not sha:
            raise MergeNotArmed("Live merge requires an issue number and a verified SHA.")
        branch = self._run_command(["git", "branch", "--show-current"]).strip()
        if not branch:
            raise MergeNotArmed("HEAD is detached. Merge requires a checked-out branch.")
        pr = self._view_pr(repo, branch)
        if pr.state != "OPEN":
            raise MergeNotArmed("The pull request is not open.")
        if pr.headRefOid.lower() != sha.lower():
            raise MergeNotArmed("The pull request head differs from the verified SHA.")
        if not pr.headRefName.startswith(f"{issue}-"):
            raise MergeNotArmed(f"The pull request head branch must start with {issue}-.")
        if pr.baseRefName != "main":
            raise MergeNotArmed("The pull request must target main.")
        try:
            self._run_command(_merge_command(repo, str(pr.number), sha))
        except MergeNotArmed as exc:
            if not isinstance(exc.__cause__, subprocess.TimeoutExpired):
                raise
            try:
                confirmed = self._view_pr(repo, str(pr.number))
            except MergeNotArmed as confirmation_error:
                raise MergeNotArmed(
                    f"Merge arming timed out and was not confirmed ({confirmation_error})."
                ) from exc
            if (
                confirmed.number != pr.number
                or confirmed.headRefOid.lower() != sha.lower()
                or confirmed.autoMergeRequest is None
            ):
                raise MergeNotArmed("Merge arming timed out and was not confirmed.") from exc
        return "armed"

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
        operation = " ".join(command[:3])
        try:
            result = subprocess.run(
                command, cwd=self.cwd, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30,
            )
        except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
            raise MergeNotArmed(f"{operation} failed ({exc}).") from exc
        if result.returncode:
            raise MergeNotArmed(result.stderr.strip() or f"{operation} exited with code {result.returncode}.")
        return result.stdout


class DryRunForge:
    def __init__(
        self,
        issues: dict[tuple[str, int], Issue] | None = None,
        merged: set[tuple[str, int]] | None = None,
    ):
        self.issues = deepcopy(issues or {})
        self.merged = set(merged or ())
        self.merge_commands: list[list[str]] = []

    def load_issue(self, repo: str, number: int) -> Issue:
        return deepcopy(self.issues[(repo, number)])

    def create_issue(self, repo: str, title: str, body: str, labels: list[str]) -> int:
        number = max((n for issue_repo, n in self.issues if issue_repo == repo), default=0) + 1
        self.issues[(repo, number)] = {"title": title, "body": body, "labels": list(labels)}
        return number

    def is_merged(self, repo: str, number: int) -> bool:
        return (repo, number) in self.merged

    def add_label(self, repo: str, number: int, label: str) -> None:
        labels = self.issues[(repo, number)]["labels"]
        if label not in labels:
            labels.append(label)

    def arm_auto_merge(self, repo: str, issue: int | None, sha: str | None) -> MergeOutcome:
        self.merge_commands.append(_merge_command(repo, "<number>", sha or "<sha>"))
        return "skipped"
