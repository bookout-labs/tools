"""A handler that walks the graph without calling a model."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from foreman.adapters import Outcome, Usage
from foreman.config import ModelSpec
from foreman.stage_output import MergeRiskRecord, SpecCheckOutput, StoredSlice, WorkKind
from foreman.github import PullRequest, Issue, OpenIssue, LinkedIssue, IssueBlocker, ReviewComment, ReviewReceipt, pull_request_command
from foreman.readiness import MergeReceipt, Observation, PullSnapshot, Ref
from foreman.review_context import ReviewContext, SCRIPTED_REVIEW_CONTEXT
from foreman.workspace import WorktreeState, ReviewDiff, ImplementationCandidate, branch_command, commit_commands, push_command, CheckLog, PullRequestSize, ReviewScope

SCRIPTED_USAGE = Usage(input_tokens=1, output_tokens=1)
CLEAR = SpecCheckOutput(findings=[], notes=[], resolutions=[])

DEFAULT_RISK: MergeRiskRecord = {
    "alignment": {"applies": True, "assessment": "Items are listed once.", "evidence": [{"side": "head", "path": "items.txt", "line": 1}]},
    "impact": {"applies": True, "assessment": "The item list changes.", "evidence": [{"side": "head", "path": "items.txt", "line": 1}]},
    "verification": {"applies": True, "assessment": "The item check covers this edit.", "evidence": [{"side": "head", "path": "items.txt", "line": 1}]},
    "rollback": {"applies": True, "assessment": "Revert the item edit.", "evidence": [{"side": "head", "path": "items.txt", "line": 1}]},
    "error_detection": {"applies": True, "assessment": "The item check detects duplicates.", "evidence": [{"side": "head", "path": "items.txt", "line": 1}]},
    "level": "low",
}


class ScriptHandler:
    def __init__(
        self,
        triage: str = "implement",
        spec_gate: str = "ready",
        reviews: list[str] | None = None,
        spec: str = "Behavior, decisions, and technical design.",
        slices: list[object] | None = None,
        implementation: str = "diff",
        research: str = "answer",
        risk: Mapping[str, object] | None = None,
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
        self.risk = dict(DEFAULT_RISK if risk is None else risk)
        self.calls: list[dict[str, str | None]] = []

    def __call__(self, node: str, model: ModelSpec | None, state: Mapping[str, object], instruction: str) -> Outcome:
        self.calls.append({"node": node, "model": model.name if model else None, "instruction": instruction})
        if node == "triage":
            return Outcome("ran", {
                "triage": self.triage, "work_kind": self.work_kind, "human_question": None,
                "slice": self.slice if self.triage == "implement" and state["issue_kind"] == "parent" else None,
            }, usage=SCRIPTED_USAGE)
        if node == "spec":
            return Outcome("ran", {
                "spec": self.spec, "spec_gate": self.spec_gate, "slices": self.slices,
                "human_question": None,
            }, usage=SCRIPTED_USAGE)
        if node == "spec_check":
            return Outcome("ran", CLEAR.model_dump(), usage=SCRIPTED_USAGE)
        if node == "implement":
            return Outcome("ran", {
                "implementation": self.implementation,
                "browser_proof": None, "formal_proof": None,
            }, usage=SCRIPTED_USAGE)
        if node == "research":
            return Outcome("ran", {"research": self.research}, usage=SCRIPTED_USAGE)
        if node == "review":
            verdict = self.reviews.pop(0) if self.reviews else "changes"
            findings = (
                [{"id": "check", "kind": "untested-path", "fix": "Add a check.", "ruling_id": None, "file": "items.txt", "line": 1, "severity": "blocking", "summary": "Add a check."}]
                if verdict == "changes" and not state.get("review") else []
            )
            return Outcome("ran", {
                "resolutions": [{"id": "check", "status": "resolved" if verdict == "approve" else "unresolved"}] if state.get("review") else [], "risk": dict(self.risk), "findings": findings, "tool_facts": [],
            }, usage=SCRIPTED_USAGE)
        raise ValueError(f"no script for {node}")


@dataclass
class DryRun:
    issue_placeholder = 0
    commands: list[list[str]] = field(default_factory=list[list[str]])
    root: Path = Path(".")
    branch: str | None = None
    head: str = "<sha>"
    clean: bool = True

    def run(self, command: str, *, log: CheckLog | None = None) -> int:
        return 0

    def inspect(self) -> WorktreeState:
        return WorktreeState(self.head, self.clean, self.branch)

    def create_branch(self, branch: str, base: str) -> None:
        if self.branch != branch:
            self.commands.append(branch_command(branch, base))
        self.branch = branch

    def reconcile(self, parent: str, branch: str, base_branch: str, fact_record: str) -> ImplementationCandidate:
        self.clean = False
        return {"disposition": "edits", "fingerprint": self.fingerprint()}

    def fingerprint(self) -> str:
        return "dry-run"

    def commit(self, message: str) -> None:
        self.commands.extend(commit_commands(message))
        self.clean = True

    def push(self, branch: str, repo: str) -> None:
        self.commands.append(push_command("origin", branch))

    def default_branch(self, repo: str) -> str:
        return "<default-branch>"

    def pull_request_base(self, repo: str, branch: str) -> str:
        return self.default_branch(repo)

    def open_pull_request(self, repo: str, head: str, base: str, title: str, body: str) -> None:
        self.commands.append(pull_request_command(repo, head, base, title, body))

    def pull_request_size(self, head: str, base_branch: str) -> PullRequestSize:
        return {"head": head, "merge_base": "<base>", "lines": 0}

    def review_target(self, repo: str, issue: int, branch: str, sha: str) -> PullRequest:
        return PullRequest(state="OPEN", number=1, headRefOid=sha, headRefName=branch,
                           baseRefName=self.default_branch(repo), baseRefOid="<base>", autoMergeRequest=None)

    def review_scope(self, base: str, head: str, previous: str | None) -> ReviewScope:
        return ReviewScope("", frozenset({("example.txt", 1)}))

    def post_review(self, repo: str, target: PullRequest, body: str, comments: list[ReviewComment], *, marker: str) -> ReviewReceipt:
        return {"id": 1, "state": "COMMENTED", "commit_id": target.headRefOid}

    def merge_hold(self, repo: str) -> str | None:
        return None

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

    def tree(self, head: str) -> str:
        raise NotImplementedError("Not used by dry delivery")

    def soft_reset(self, parent: str) -> None:
        raise NotImplementedError("Not used by dry delivery")

    def files_at(self, head: str, directory: str) -> set[str]:
        return set()

    def read_file_at(self, head: str, path: str) -> str:
        raise NotImplementedError("Not used by dry delivery")

    def has_file(self, path: str) -> bool:
        return False

    def review_diff(self, base: str, head: str, *, interdiff: bool = False) -> ReviewDiff:
        raise NotImplementedError("Not used by dry delivery")

    def load_issue(self, repo: str, number: int) -> Issue:
        raise NotImplementedError("Not used by dry delivery")

    def open_issues(self, repo: str) -> list[OpenIssue]:
        raise NotImplementedError("Not used by dry delivery")

    def marked_issues(self, repo: str, marker: str) -> list[int]:
        raise NotImplementedError("Not used by dry delivery")

    def sub_issues(self, repo: str, number: int) -> list[LinkedIssue]:
        raise NotImplementedError("Not used by dry delivery")

    def parent_issue(self, repo: str, number: int) -> LinkedIssue | None:
        raise NotImplementedError("Not used by dry delivery")

    def add_sub_issue(self, repo: str, number: int, child: int) -> None:
        raise NotImplementedError("Not used by dry delivery")

    def update_issue_body(self, repo: str, number: int, body: str) -> None:
        raise NotImplementedError("Not used by dry delivery")

    def issue_comments(self, repo: str, number: int) -> list[str]:
        raise NotImplementedError("Not used by dry delivery")

    def comment_issue(self, repo: str, number: int, body: str) -> None:
        raise NotImplementedError("Not used by dry delivery")

    def create_issue(self, repo: str, title: str, body: str, labels: list[str], *, child: StoredSlice | None = None) -> int:
        raise NotImplementedError("Not used by dry delivery")

    def add_blocker(self, repo: str, number: int, blocker: int) -> None:
        raise NotImplementedError("Not used by dry delivery")

    def blockers(self, repo: str, number: int) -> list[IssueBlocker]:
        raise NotImplementedError("Not used by dry delivery")

    def is_merged(self, repo: str, number: int) -> bool:
        raise NotImplementedError("Not used by dry delivery")

    def add_label(self, repo: str, number: int, label: str) -> None:
        raise NotImplementedError("Not used by dry delivery")

    def reconcile_merge(self, receipt: MergeReceipt) -> MergeReceipt:
        raise NotImplementedError("Not used by dry delivery")
