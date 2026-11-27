"""A handler that walks the graph without calling a model."""

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from foreman.adapters import Outcome, Usage
from foreman.config import ModelSpec
from foreman.credentials import Subscription
from foreman.doctor import EVENTS
from foreman.stage_output import PlanState, DispatchPlan, SpecCheckOutput, StoredSlice, WorkKind
from foreman.github import (
    BOT_LOGIN, AppInstallation, Permission, PullRequest, PublishedPullRequest, Issue, IssueComment, LinkedIssue, IssueBlocker, ReviewComment, ReviewReceipt,
    marked, pull_request_command, render_slice_body, status_body,
)
from foreman.readiness import MergeReceipt, Observation, PullSnapshot, QueuePolicy, Ref
from foreman.review_context import ReviewContext, SCRIPTED_REVIEW_CONTEXT
from foreman.workspace import (
    WorkState, WorktreeState, ReviewDiff, ImplementationCandidate, LiveWorkspace, branch_command, commit_commands, push_command, CheckLog,
    PathKind, PullRequestSize, RelatedCheckout, ReviewScope, repository_parts,
)

if TYPE_CHECKING:
    from foreman.dispatch import Handler
    from foreman.state import DeliveryState

SCRIPTED_USAGE = Usage(cache_read_input_tokens=0, cache_creation_input_tokens=0, input_tokens=1, output_tokens=1, total_cost_usd=0.0)
CLEAR = SpecCheckOutput(findings=[], notes=[], human_question=None)
# A dry run reads no repository, so it walks with this contract text, parsed like a target repository's own.
DRY_CONTRACT = (b"check: <check from .foreman/contract.yaml>\nverify_skill: .claude/skills/verify-web/SKILL.md\n"
                b"image: <image from .foreman/contract.yaml>\nsetup: []\nsecrets: []\n")

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
        assumptions: list[object] | None = None,
    ):
        self.triage = triage
        self.work_kind = work_kind
        self.spec_gate = spec_gate
        self.reviews = list(reviews if reviews is not None else ["approve"])
        self.spec = spec
        self.slices = slices
        self.assumptions = assumptions or []
        self.slice: Mapping[str, object] = slice if slice is not None else {
            "title": "Implement the requested change",
            "criteria": [{"text": "The requested change passes verification.", "proof": {"kind": "drive", "not_drivable": None}}],
            "non_goals": [],
            "removed_tests": [],
            "files": [],
            "depends_on": [],
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
                "spec": self.spec, "spec_gate": self.spec_gate, "slices": self.slices, "assumptions": self.assumptions,
                "questions": [{"question": "Which policy should apply?",
                    "why_code_unanswered": "The issue supplies no policy and no checkout file covers it.",
                    "checked_paths": []}] if self.spec_gate == "needs_human" else [],
            }, usage=SCRIPTED_USAGE)
        if node == "spec_check":
            return Outcome("ran", CLEAR.model_dump(), usage=SCRIPTED_USAGE)
        if node == "implement":
            return Outcome("ran", {"implementation": self.implementation, "no_change": None, "human_question": None}, usage=SCRIPTED_USAGE)
        if node == "research":
            return Outcome("ran", {"research": self.research, "human_question": None}, usage=SCRIPTED_USAGE)
        if node == "verify":
            driven = StoredSlice.model_validate(PlanState.model_validate(state.get("plan") or {}).slice or self.slice)
            return Outcome("ran", {"tested_head": WorkState.model_validate(state.get("work") or {}).published_head, "criteria": [{
                "criterion": criterion.text, "result": "passed",
                "observation": "The scripted drive used the change.", "evidence": "captures/scripted.txt"}
                for criterion in driven.criteria]}, usage=SCRIPTED_USAGE)
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
    # A target checkout whose HEAD stands in for the pinned base, so the stages read its tracked skills.
    checkout: Path | None = None

    def prepare_container(self, *, base: str, log: CheckLog | None) -> None:
        pass

    def remove_container(self, *, unless: Callable[[], bool] = lambda: False) -> None:
        pass

    def remove_leftover_containers(self) -> None:
        pass

    def run(self, command: str, *, base: str, log: CheckLog | None) -> int:
        return 0

    def inspect(self) -> WorktreeState:
        return WorktreeState(self.head, self.clean, self.branch)

    def validate_origin(self, repo: str) -> None:
        pass

    def pin_base(self, base: str) -> str:
        return self.head

    def check_out_related(self, repo: str, branch: str) -> RelatedCheckout:
        return RelatedCheckout(repository=repo, commit=self.head, checkout=f"<checkout of {repo}>")

    def release(self, heads: Collection[str]) -> bool:
        return False

    def create_branch(self, branch: str, base: str, *, pinned: str = "") -> None:
        if self.branch != branch:
            self.commands.append(branch_command(branch, f"refs/remotes/origin/{base}"))
        self.branch = branch

    def builder(self, handler: "Handler") -> "Handler":
        """The handler with its builder writing to this checkout, so inspect shows the edits as a worktree would.

        A builder answer that asks a question or reports no_change edits nothing. Any other answer leaves edits."""
        def call(node: str, model: ModelSpec | None, state: "DeliveryState", instruction: str, /) -> Outcome:
            outcome = handler(node, model, state, instruction)
            if node == "implement" and outcome.kind == "ran" and not (outcome.output.get("no_change") or outcome.output.get("human_question")):
                self.clean = False
            return outcome
        return call

    def reconcile(self, parent: str, branch: str, base_branch: str, *, base: str | None = None) -> ImplementationCandidate:
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

    def close_pull_request(self, repo: str, number: int, comment: str) -> None:
        self.commands.append(["gh", "pr", "close", str(number), "--repo", repo, "--comment", comment])
        if self.pull_request is not None:
            self.pull_request = self.pull_request.model_copy(update={"state": "CLOSED"})

    def pull_request_size(self, head: str | None, base_branch: str, *, merge_base: str | None = None) -> PullRequestSize:
        return {"head": head or self.tree(None), "merge_base": merge_base or "<base>", "lines": 0}

    def review_target(self, repo: str, issue: int, branch: str, sha: str) -> PullRequest:
        return PullRequest(state="OPEN", number=1, headRefOid=sha, headRefName=branch,
                           baseRefName=self.default_branch(repo), baseRefOid="<base>", autoMergeRequest=None)

    def review_scope(self, base: str, head: str) -> ReviewScope:
        return ReviewScope(f"git diff {base}...{head} --", frozenset({("example.txt", 1)}))

    def post_review(self, repo: str, target: PullRequest, body: str, comments: list[ReviewComment], *, marker: str) -> ReviewReceipt:
        return {"id": 1, "state": "COMMENTED", "commit_id": target.headRefOid}

    def variables(self, repo: str) -> dict[str, str]:
        # The dry run's repository sets its operator variables, so doctor's quick check passes.
        return {"FOREMAN_HUMAN_LOGIN": "ghost"}

    def app_installation(self, repo: str) -> AppInstallation:
        return "installed"

    def app_subscription(self, repo: str) -> Subscription:
        return Subscription(events=frozenset(EVENTS), permissions={grant.key: "read" for grant in EVENTS.values()})

    def permission(self, repo: str, login: str) -> Permission | None:
        return "write"

    def has_label(self, repo: str, name: str) -> bool:
        return True

    def merge_queue(self, repo: str, branch: str) -> QueuePolicy | None:
        return QueuePolicy(merge_method="SQUASH")

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

    def committed(self, parents: Sequence[str], message: str) -> bool:
        raise NotImplementedError("Not used by dry delivery")

    def run_at(self, commit: str, command: str, *, base: str, log: CheckLog | None) -> int:
        raise NotImplementedError("A dry run's pull request never conflicts with its base")

    def base_head(self, base: str) -> str:
        raise NotImplementedError("A dry run's pull request never conflicts with its base")

    def merge(self, head: str) -> list[str]:
        raise NotImplementedError("A dry run's pull request never conflicts with its base")

    def abandon_merge(self, branch: str, parent: str, base_branch: str) -> None:
        raise NotImplementedError("A dry run's pull request never conflicts with its base")

    def tree(self, head: str | None) -> str:
        return "<tree>"

    def soft_reset(self, parent: str) -> None:
        raise NotImplementedError("Not used by dry delivery")

    def tracked_path(self, base: str, path: str) -> PathKind | None:
        if self.checkout is not None:
            return LiveWorkspace(self.checkout).tracked_path("HEAD", path)
        # Without a checkout the pinned tree is empty.
        return None if repository_parts(path) is None else "absent"

    def tracked_file(self, base: str, path: str) -> bytes | None:
        return LiveWorkspace(self.checkout).tracked_file("HEAD", path) if self.checkout is not None else None

    def contains_commit(self, base: str, commit: str) -> bool:
        return False

    def review_diff(self, base: str, head: str) -> ReviewDiff:
        raise NotImplementedError("Not used by dry delivery")

    def load_issue(self, repo: str, number: int) -> Issue:
        return self.issues[number]

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

    def issue_comment_records(self, repo: str, number: int) -> list[IssueComment]:
        return list(self.comments)

    def ready_labeled_at(self, repo: str, number: int) -> datetime | None:
        return None  # The dry run's issue has no label history, so every stop on it counts.

    def comment_issue(self, repo: str, number: int, body: str, *, marker: str) -> None:
        body = marked(body, marker)
        self.commands.append(["gh", "issue", "comment", str(number), "--repo", repo, "--body", body])
        self.comments.append(IssueComment.model_validate({"body": body, "user": {"login": BOT_LOGIN}}))

    def upsert_comment(self, repo: str, number: int, text: str, facts: list[str], *, marker: str) -> None:
        self.commands.append(["gh", "pr", "comment", str(number), "--repo", repo, "--body", status_body(text, facts, marker)])

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

    def blocking(self, repo: str, number: int) -> list[IssueBlocker]:
        return [IssueBlocker(number=dependent, title="", repository_url=f"https://api.github.com/repos/{repo}",
                             html_url=f"https://github.com/{repo}/issues/{dependent}", state="open")
                for dependent, (_, blockers) in self.links.items() if number in blockers]

    def is_merged(self, repo: str, number: int, *, contains: Callable[[str], bool] | None = None) -> bool:
        return False

    def descends(self, repo: str, ancestor: str, head: str) -> bool:
        raise NotImplementedError("Not used by dry delivery")

    def ensure_label(self, repo: str, name: str, color: str) -> None:
        pass  # The dry run's issues carry any label.

    def edit_labels(self, repo: str, number: int, add: list[str], remove: list[str]) -> None:
        self.commands.append(["gh", "issue", "edit", str(number), "--repo", repo,
                              *(argument for label in add for argument in ("--add-label", label)),
                              *(argument for label in remove for argument in ("--remove-label", label))])
        labels = self.issues[number]["labels"]
        labels[:] = [label for label in labels if label not in remove] + [label for label in add if label not in labels]

    def reconcile_merge(self, receipt: MergeReceipt) -> MergeReceipt:
        raise NotImplementedError("Not used by dry delivery")
