"""A handler that walks the graph without calling a model."""

from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from foreman.adapters import Outcome, Usage
from foreman.config import ModelSpec
from foreman.stage_output import PlanState, DispatchPlan, SpecCheckOutput, StoredSlice, WorkKind
from foreman.github import (
    BOT_LOGIN, PullRequest, PublishedPullRequest, Issue, IssueComment, OpenIssue, LinkedIssue, IssueBlocker, ReviewComment, ReviewReceipt,
    marked, pull_request_command, render_slice_body,
)
from foreman.readiness import MergeReceipt, Observation, PullSnapshot, Ref
from foreman.review_context import ReviewContext, SCRIPTED_REVIEW_CONTEXT
from foreman.workspace import WorktreeState, ReviewDiff, ImplementationCandidate, branch_command, commit_commands, push_command, CheckLog, PullRequestSize, ReviewScope

SCRIPTED_USAGE = Usage(cache_read_input_tokens=0, cache_creation_input_tokens=0, input_tokens=1, output_tokens=1, total_cost_usd=0.0)
CLEAR = SpecCheckOutput(findings=[], notes=[], human_question=None)

class ScriptHandler:
    def __init__(
        self,
        triage: str = "supplied_slice",
        spec_gate: str = "ready",
        reviews: list[str] | None = None,
        spec: str = "Behavior, decisions, and technical design.",
        slices: list[object] | None = None,
        implementation: str = "diff",
        research: str = "answer",
        slice: Mapping[str, object] | None = None,
        work_kind: WorkKind = "feature",
    ):
        self.triage = triage
        self.work_kind = work_kind
        self.spec_gate = spec_gate
        self.reviews = list(reviews if reviews is not None else ["approve"])
        self.spec = spec
        self.slices = slices
        self.slice: Mapping[str, object] = slice if slice is not None else {
            "title": "Implement the requested change",
            "acceptance": "The requested change passes verification.",
            "builds": [],
            "uses": [],
            "screen": "",
            "properties": [],
            "stateful": False,
        }
        self.implementation = implementation
        self.research = research
        self.calls: list[dict[str, str | None]] = []

    def __call__(self, node: str, model: ModelSpec | None, state: Mapping[str, object], instruction: str) -> Outcome:
        self.calls.append({"node": node, "model": model.name if model else None, "instruction": instruction,
                           "budget_usd": DispatchPlan.model_validate(state["dispatch_plan"]).budget_usd if state.get("dispatch_plan") else None})
        if node == "triage":
            return Outcome("ran", {
                "triage": self.triage, "work_kind": self.work_kind,
                "human_question": "Which scope should this change cover?" if self.triage == "needs_human" else None,
                "slice": self.slice if self.triage == "supplied_slice" and PlanState.model_validate(state["plan"]).issue_kind == "parent" else None,
            }, usage=SCRIPTED_USAGE)
        if node == "spec":
            return Outcome("ran", {
                "spec": self.spec, "spec_gate": self.spec_gate, "slices": self.slices,
                "questions": [{"question": "Which policy should apply?",
                    "why_code_unanswered": "The issue supplies no policy and no checkout file covers it.",
                    "checked_paths": []}] if self.spec_gate == "needs_human" else [],
            }, usage=SCRIPTED_USAGE)
        if node == "spec_check":
            return Outcome("ran", CLEAR.model_dump(), usage=SCRIPTED_USAGE)
        if node == "implement":
            return Outcome("ran", {"implementation": self.implementation, "human_question": None}, usage=SCRIPTED_USAGE)
        if node == "research":
            return Outcome("ran", {"research": self.research, "human_question": None}, usage=SCRIPTED_USAGE)
        if node == "review":
            verdict = self.reviews.pop(0) if self.reviews else "changes"
            findings = [{
                "id": "check", "kind": "correctness", "severity": "P1", "ruling_id": None,
                "location": {"side": "head", "path": "items.txt", "line": 1},
                "summary": "The first item is listed twice.",
                "impact": "The item is counted twice.", "evidence": "The first two entries have the same ID.",
            }] if verdict == "changes" else []
            return Outcome("ran", {"findings": findings, "human_question": None}, usage=SCRIPTED_USAGE)
        raise ValueError(f"no script for {node}")


@dataclass
class DryRun:
    issue_placeholder = 0
    commands: list[list[str]] = field(default_factory=list[list[str]])
    root: Path = Path(".")
    branch: str | None = None
    head: str = "<sha>"
    clean: bool = True
    remote: str | None = None
    pull_request: PublishedPullRequest | None = None
    comments: list[IssueComment] = field(default_factory=list[IssueComment])
    issues: dict[int, Issue] = field(default_factory=dict[int, Issue])
    links: dict[int, tuple[int, list[int]]] = field(default_factory=dict[int, tuple[int, list[int]]])

    def run(self, command: str, *, log: CheckLog | None = None) -> int:
        return 0

    def inspect(self) -> WorktreeState:
        return WorktreeState(self.head, self.clean, self.branch)

    def validate_origin(self, repo: str) -> None:
        pass

    def pin_base(self, base: str) -> str:
        return self.head

    def release(self, heads: Collection[str]) -> bool:
        return False

    def create_branch(self, branch: str, base: str, *, pinned: str = "") -> None:
        if self.branch != branch:
            self.commands.append(branch_command(branch, f"refs/remotes/origin/{base}"))
        self.branch = branch

    def reconcile(self, parent: str, branch: str, base_branch: str) -> ImplementationCandidate:
        self.clean = False
        return {"disposition": "edits", "fingerprint": self.fingerprint(),
                "size": self.pull_request_size(self.tree(None), base_branch)}

    def fingerprint(self) -> str:
        return "dry-run"

    def commit(self, message: str) -> None:
        self.commands.extend(commit_commands(message))
        self.clean = True

    def push(self, branch: str, repo: str) -> None:
        self.commands.append(push_command("origin", branch))
        self.remote = self.head

    def remote_head(self, branch: str) -> str | None:
        return self.remote

    def uncommit(self, parent: str) -> None:
        raise NotImplementedError("Not used by dry delivery")

    def default_branch(self, repo: str) -> str:
        return "<default-branch>"

    def open_pull_request(self, repo: str, head: str, base: str, title: str, body: str) -> None:
        self.commands.append(pull_request_command(repo, head, base, title, body))
        self.pull_request = PublishedPullRequest(
            state="OPEN", number=1, headRefOid=self.head, headRefName=head, baseRefName=base,
            baseRefOid="<base>", autoMergeRequest=None, body=body)

    def pull_request_for(self, repo: str, branch: str) -> PublishedPullRequest | None:
        return self.pull_request

    def pull_request_size(self, head: str | None, base_branch: str, *, merge_base: str | None = None) -> PullRequestSize:
        return {"head": head or self.tree(None), "merge_base": merge_base or "<base>", "lines": 0}

    def review_target(self, repo: str, issue: int, branch: str, sha: str) -> PullRequest:
        return PullRequest(state="OPEN", number=1, headRefOid=sha, headRefName=branch,
                           baseRefName=self.default_branch(repo), baseRefOid="<base>", autoMergeRequest=None)

    def review_scope(self, base: str, head: str) -> ReviewScope:
        return ReviewScope("", frozenset({("example.txt", 1)}))

    def post_review(self, repo: str, target: PullRequest, body: str, comments: list[ReviewComment], *, marker: str) -> ReviewReceipt:
        return {"id": 1, "state": "COMMENTED", "commit_id": target.headRefOid}

    def variables(self, repo: str) -> dict[str, str]:
        return {}

    def readiness(self, repo: str, issue: int, branch: str, sha: str) -> Observation:
        return Observation(repo=repo, pr=PullSnapshot(number=1, state="OPEN", draft=False,
            head=Ref(sha=sha, ref=branch), base=Ref(sha="<base>", ref=self.default_branch(repo)),
            mergeable=True, merge_commit_sha=None, mergeable_state="clean"), checks=[], outcome="ready", recovery="")

    def arm_auto_merge(self, repo: str, issue: int, branch: str, sha: str, observation: Observation) -> None:
        return None

    def review_context(self, base: str, head: str) -> ReviewContext:
        return SCRIPTED_REVIEW_CONTEXT

    def refresh(self, branch: str, head: str) -> None:
        raise NotImplementedError("Not used by dry delivery")

    def committed(self, parent: str, message: str) -> bool:
        raise NotImplementedError("Not used by dry delivery")

    def tree(self, head: str | None) -> str:
        return "<tree>"

    def soft_reset(self, parent: str) -> None:
        raise NotImplementedError("Not used by dry delivery")

    def has_tracked_file(self, base: str, path: str) -> bool:
        return False

    def contains_commit(self, base: str, commit: str) -> bool:
        return False

    def review_diff(self, base: str, head: str) -> ReviewDiff:
        raise NotImplementedError("Not used by dry delivery")

    def load_issue(self, repo: str, number: int) -> Issue:
        return self.issues[number]

    def open_issues(self, repo: str) -> list[OpenIssue]:
        raise NotImplementedError("Not used by dry delivery")

    def marked_issues(self, repo: str, marker: str) -> list[int]:
        return [number for number, issue in self.issues.items() if marker in issue["body"]]

    def sub_issues(self, repo: str, number: int) -> list[LinkedIssue]:
        raise NotImplementedError("Not used by dry delivery")

    def parent_issue(self, repo: str, number: int) -> LinkedIssue | None:
        if number not in self.links:
            return None
        parent = self.links[number][0]
        # The dry run's parent is the issue placeholder, which GitHub would never number.
        return LinkedIssue.model_construct(id=parent, number=parent, title="", repository_url=f"https://api.github.com/repos/{repo}",
                                           html_url=f"https://github.com/{repo}/issues/{parent}", state="open")

    def add_sub_issue(self, repo: str, number: int, child: int) -> None:
        self.commands.append(["gh", "api", f"repos/{repo}/issues/{number}/sub_issues", "--method", "POST", "-F", f"sub_issue_id=<#{child}>"])
        self.links[child] = (number, [])

    def update_issue_body(self, repo: str, number: int, body: str) -> None:
        raise NotImplementedError("Not used by dry delivery")

    def issue_comments(self, repo: str, number: int) -> list[str]:
        raise NotImplementedError("Not used by dry delivery")

    def issue_comment_records(self, repo: str, number: int) -> list[IssueComment]:
        return list(self.comments)

    def comment_issue(self, repo: str, number: int, body: str, *, marker: str) -> None:
        body = marked(body, marker)
        self.commands.append(["gh", "issue", "comment", str(number), "--repo", repo, "--body", body])
        self.comments.append(IssueComment.model_validate({"body": body, "user": {"login": BOT_LOGIN}}))

    def create_issue(self, repo: str, title: str, body: str, labels: list[str], *, marker: str, child: StoredSlice | None = None) -> int:
        body = marked(body, marker)
        if child is not None:
            body = render_slice_body(body, child)
        number = len(self.issues) + 1
        self.commands.append(["gh", "issue", "create", "--repo", repo, "--title", title, "--body", body,
                              *(argument for label in labels for argument in ("--label", label))])
        self.issues[number] = {"repo": repo, "number": number, "title": title, "body": body, "labels": list(labels)}
        return number

    def add_blocker(self, repo: str, number: int, blocker: int) -> None:
        self.commands.append(["gh", "api", f"repos/{repo}/issues/{number}/dependencies/blocked_by", "--method", "POST", "-F", f"issue_id=<#{blocker}>"])
        self.links[number][1].append(blocker)

    def blockers(self, repo: str, number: int) -> list[IssueBlocker]:
        return [IssueBlocker(number=blocker, title="", repository_url=f"https://api.github.com/repos/{repo}",
                             html_url=f"https://github.com/{repo}/issues/{blocker}", state="open")
                for blocker in self.links.get(number, (0, []))[1]]

    def is_merged(self, repo: str, number: int, *, contains: Callable[[str], bool] | None = None) -> bool:
        return False

    def descends(self, repo: str, ancestor: str, head: str) -> bool:
        raise NotImplementedError("Not used by dry delivery")

    def add_label(self, repo: str, number: int, label: str) -> None:
        self.commands.append(["gh", "issue", "edit", str(number), "--repo", repo, "--add-label", label])
        self.issues[number]["labels"].append(label)

    def reconcile_merge(self, receipt: MergeReceipt) -> MergeReceipt:
        raise NotImplementedError("Not used by dry delivery")
