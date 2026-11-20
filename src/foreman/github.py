"""GitHub issue operations used by live runs."""

import json
import subprocess
from typing import Protocol, TypedDict


class Issue(TypedDict):
    title: str
    body: str
    labels: list[str]


class GitHub(Protocol):
    def load_issue(self, repo: str, number: int) -> Issue: ...
    def create_issue(self, repo: str, title: str, body: str, labels: list[str]) -> int: ...
    def is_merged(self, repo: str, number: int) -> bool: ...
    def add_label(self, repo: str, number: int, label: str) -> None: ...


class GhClient:
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
