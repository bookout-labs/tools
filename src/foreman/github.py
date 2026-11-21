"""GitHub operations for delivery runs."""

import json
from pathlib import Path
import subprocess
from typing import Literal, Protocol, TypedDict

from pydantic import BaseModel, PositiveInt, ValidationError


class MergeNotArmed(RuntimeError):
    pass


class PullRequest(BaseModel, strict=True):
    number: PositiveInt
    state: Literal["OPEN", "CLOSED", "MERGED"]
    headRefOid: str
    headRefName: str
    baseRefName: str


class Issue(TypedDict):
    title: str
    body: str
    labels: list[str]


class Forge(Protocol):
    def load_issue(self, repo: str, number: int) -> Issue: ...
    def create_issue(self, repo: str, title: str, body: str, labels: list[str]) -> int: ...
    def is_merged(self, repo: str, number: int) -> bool: ...
    def add_label(self, repo: str, number: int, label: str) -> None: ...
    def arm_auto_merge(self, repo: str, issue: int, sha: str) -> None: ...


def _merge_command(repo: str, pull_request: str, sha: str) -> list[str]:
    return ["gh", "pr", "merge", pull_request, "--repo", repo, "--auto", "--match-head-commit", sha]


class LiveForge:
    def __init__(self, cwd: Path | None = None):
        self.cwd = cwd

    def load_issue(self, repo: str, number: int) -> Issue:
        output = subprocess.check_output(
            ["gh", "issue", "view", str(number), "--repo", repo, "--json", "title,body,labels"], text=True
        )
        issue = json.loads(output)
        return {"title": issue["title"], "body": issue["body"] or "", "labels": [x["name"] for x in issue["labels"]]}

    def create_issue(self, repo: str, title: str, body: str, labels: list[str]) -> int:
        args = ["gh", "issue", "create", "--repo", repo, "--title", title, "--body", body]
        for label in labels:
            args.extend(["--label", label])
        url = subprocess.check_output(args, text=True).strip()
        return int(url.rsplit("/", 1)[-1])

    def is_merged(self, repo: str, number: int) -> bool:
        issue = json.loads(subprocess.check_output(
            ["gh", "issue", "view", str(number), "--repo", repo, "--json",
             "closed,stateReason,closedByPullRequestsReferences"], text=True
        ))
        if not issue["closed"]:
            return False
        for pr in issue["closedByPullRequestsReferences"]:
            state = pr.get("state")
            if not state:
                state = json.loads(subprocess.check_output(
                    ["gh", "pr", "view", str(pr["number"]), "--repo", repo, "--json", "state"], text=True
                ))["state"]
            if state == "MERGED":
                return True
        return False

    def add_label(self, repo: str, number: int, label: str) -> None:
        subprocess.check_output(["gh", "issue", "edit", str(number), "--repo", repo, "--add-label", label], text=True)

    def arm_auto_merge(self, repo: str, issue: int, sha: str) -> None:
        # The empty selector lets gh use the current branch with an explicit repository.
        view = ["gh", "pr", "view", "--repo", repo, "--json", "number,state,headRefOid,headRefName,baseRefName", ""]
        try:
            pr = PullRequest.model_validate_json(self._run_pr_command(view))
        except ValidationError as exc:
            raise MergeNotArmed(f"Invalid response from gh pr view ({exc}).") from exc
        if pr.state != "OPEN":
            raise MergeNotArmed("The pull request is not open.")
        if pr.headRefOid.lower() != sha.lower():
            raise MergeNotArmed("The pull request head differs from the verified SHA.")
        if not pr.headRefName.startswith(f"{issue}-"):
            raise MergeNotArmed(f"The pull request head branch must start with {issue}-.")
        if pr.baseRefName != "main":
            raise MergeNotArmed("The pull request must target main.")
        self._run_pr_command(_merge_command(repo, str(pr.number), sha))

    def _run_pr_command(self, command: list[str]) -> str:
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
    def __init__(self):
        self.merge_commands: list[list[str]] = []

    def load_issue(self, repo: str, number: int) -> Issue:
        raise NotImplementedError("Dry runs do not load GitHub issues.")

    def create_issue(self, repo: str, title: str, body: str, labels: list[str]) -> int:
        raise NotImplementedError("Dry runs do not create GitHub issues.")

    def is_merged(self, repo: str, number: int) -> bool:
        raise NotImplementedError("Dry runs do not check closing pull requests.")

    def add_label(self, repo: str, number: int, label: str) -> None:
        raise NotImplementedError("Dry runs do not label GitHub issues.")

    def arm_auto_merge(self, repo: str, issue: int | None, sha: str | None) -> None:
        self.merge_commands.append(_merge_command(repo, "<pull-request>", sha or "<verified-sha>"))
