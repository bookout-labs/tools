"""GitHub operations for delivery runs."""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING, Literal, Protocol, TypeVar
from typing_extensions import TypedDict

from pydantic import BaseModel, ConfigDict, JsonValue, PositiveInt, RootModel, ValidationError, with_config

from foreman.readiness import BranchPolicy, CheckEvidence, CheckPages, Commit, Observation, Readiness, Rules, StatusPages, QueueSnapshot, MergeReceipt
from foreman.redaction import redact
if TYPE_CHECKING:
    from foreman.stage_output import StoredSlice
from foreman.workspace import commit_sha

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


class RepositoryIssue(BaseModel, strict=True):
    number: PositiveInt
    body: str | None
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


class ReviewReceipt(TypedDict):
    id: int
    state: Literal["COMMENTED"]
    commit_id: str


class PostedReview(BaseModel, strict=True):
    id: PositiveInt
    state: str
    commit_id: str
    body: str

    def receipt(self, head: str) -> ReviewReceipt:
        if self.state != "COMMENTED" or self.commit_id.lower() != head.lower():
            raise ForgeError("Review receipt does not match the submitted COMMENT at the verified head.")
        return {"id": self.id, "state": "COMMENTED", "commit_id": self.commit_id}


class ListedReview(BaseModel, strict=True):
    id: PositiveInt
    state: str
    commit_id: str | None
    body: str


class ReviewPages(RootModel[list[list[ListedReview]]]):
    pass


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
    def post_review(self, repo: str, target: PullRequest, body: str, comments: list[ReviewComment], *, marker: str) -> ReviewReceipt: ...
    def readiness(self, repo: str, issue: int, branch: str, sha: str) -> Observation: ...
    def merge_hold(self, repo: str) -> str | None: ...
    def arm_auto_merge(self, repo: str, issue: int, branch: str, sha: str) -> MergeReceipt: ...


def pull_request_command(repo: str, head: str, base: str, title: str, body: str) -> list[str]:
    return ["gh", "pr", "create", "--repo", redact(repo), "--base", redact(base), "--head", redact(head),
            "--title", redact(" ".join(title.split())), "--body", redact(body)]


def merge_command(repo: str, pull_request: str, sha: str) -> list[str]:
    return ["gh", "pr", "merge", pull_request, "--repo", repo, "--auto", "--match-head-commit", sha]


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
        return [
            OpenIssue(number=issue.number, body=issue.body or "")
            for page in pages.root for issue in page if "pull_request" not in issue.model_fields_set
        ]

    def issue_comments(self, repo: str, number: int) -> list[str]:
        pages = self._read(IssueCommentPages, [
            "gh", "api", f"repos/{repo}/issues/{number}/comments?per_page=100", "--paginate", "--slurp",
        ])
        return [comment.body or "" for page in pages.root for comment in page]

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

    def post_review(self, repo: str, target: PullRequest, body: str, comments: list[ReviewComment], *, marker: str) -> ReviewReceipt:
        current = self._view_pr(repo, str(target.number))
        if (current.state != "OPEN" or current.baseRefName != target.baseRefName
                or current.baseRefOid != target.baseRefOid or current.headRefOid != target.headRefOid):
            raise ForgeError("The pull request changed during review.")
        endpoint = f"repos/{redact(repo)}/pulls/{target.number}/reviews"
        tag = f"<!-- foreman-review:{marker} -->"
        pages = self._read(ReviewPages, ["gh", "api", endpoint, "--paginate", "--slurp"])
        for page in pages.root:
            for review in page:
                if tag in review.body:
                    try:
                        posted = PostedReview.model_validate(review.model_dump())
                    except ValidationError as exc:
                        raise ForgeError("Marked review has an invalid receipt.") from exc
                    return posted.receipt(target.headRefOid)
        payload = {
            "commit_id": target.headRefOid, "event": "COMMENT", "body": redact(body) + "\n\n" + tag,
            "comments": [{**comment, "body": redact(comment["body"]), "path": redact(comment["path"])} for comment in comments],
        }
        posted = self._read(PostedReview,
            ["gh", "api", endpoint, "--method", "POST",
             "-H", "X-GitHub-Api-Version: 2022-11-28", "--input", "-"],
            body=json.dumps(payload),
        )
        return posted.receipt(target.headRefOid)

    def merge_hold(self, repo: str) -> str | None:
        try:
            variables = self._read(ActionsVariables, ["gh", "variable", "list", "--repo", repo, "--json", "name,value"])
        except ForgeError as exc:
            raise MergeHoldUnreadable(str(exc)) from exc
        return next((variable.value for variable in variables.root if variable.name == MERGE_HOLD_VARIABLE), None)

    def readiness(self, repo: str, issue: int, branch: str, sha: str) -> Observation:
        target = self.review_target(repo, issue, branch, sha)
        endpoint = f"repos/{repo}"
        pr = self._read(Readiness, ["gh", "api", f"{endpoint}/pulls/{target.number}"])
        def observed(outcome: Literal["ready", "conflict", "pending", "red", "policy"], recovery: str,
                     checks: list[CheckEvidence] | None = None) -> Observation:
            return Observation(repo=repo, pr=pr, checks=checks or [], outcome=outcome, recovery=recovery)
        if (pr.number != target.number or pr.head.ref != branch or pr.head.sha.lower() != sha.lower() or pr.base.sha != target.baseRefOid
                or pr.base.ref != target.baseRefName or pr.state != "open" or pr.draft):
            return observed("pending", "PR changed or is draft. Renew verification and review.")
        if pr.mergeable is False:
            return observed("conflict", "Request a merge-base repair round, then renew verification and review.")
        if pr.mergeable is None or pr.mergeable_state == "unknown":
            return observed("pending", "Resume when GitHub has computed mergeability.")
        rules = self._read(Rules, ["gh", "api", f"{endpoint}/rules/branches/{pr.base.ref}"])
        legacy = self._read(BranchPolicy, ["gh", "api", f"{endpoint}/branches/{pr.base.ref}"])
        requirements = [check for rule in rules.root if rule.type == "required_status_checks"
                        for check in rule.parameters.required_status_checks]
        classic = legacy.protection.required_status_checks
        if set(classic.contexts) - {check.context for check in classic.checks}:
            return observed("policy", "Legacy policy lacks required app identity. Restore readable policy.")
        requirements.extend(classic.checks)
        if not requirements:
            return observed("policy", "Effective required-check policy is empty. Restore readable policy.")
        commits = [pr.head.sha]
        if pr.merge_commit_sha:
            merge = self._read(Commit, ["gh", "api", f"{endpoint}/commits/{pr.merge_commit_sha}"])
            if {parent.sha for parent in merge.parents} == {pr.head.sha, pr.base.sha}:
                commits.insert(0, pr.merge_commit_sha)
        evidence: list[CheckEvidence] = []
        for commit in commits:
            runs = self._read(CheckPages, ["gh", "api", f"{endpoint}/commits/{commit}/check-runs?filter=latest&per_page=100", "--paginate", "--slurp"])
            statuses = self._read(StatusPages, ["gh", "api", f"{endpoint}/commits/{commit}/statuses?per_page=100", "--paginate", "--slurp"])
            for page in runs.root:
                evidence.extend(CheckEvidence(context=run.name, app=run.app.id, run=run.id,
                    tested_commit=commit, state=(run.conclusion or "pending") if run.status == "completed" else "pending")
                    for run in page.check_runs if run.head_sha == commit)
            evidence.extend(CheckEvidence(context=status.context, app=None, run=status.id,
                tested_commit=commit, state=status.state) for page in statuses.root for status in page)
        effective: list[CheckEvidence] = []
        for required in requirements:
            candidates = [check for check in evidence if check.context == required.context
                          and (required.integration_id in (None, -1) or check.app == required.integration_id)]
            if len({check.app for check in candidates}) > 1:
                return observed("policy", "Ambiguous required-check app identity. Bind the policy to an app.", evidence)
            tested = next((commit for commit in commits if any(c.tested_commit == commit for c in candidates)), pr.head.sha)
            latest = [c for c in candidates if c.tested_commit == tested]
            effective.append(max(latest, key=lambda c: c.run or 0) if latest else CheckEvidence(
                context=required.context, app=required.integration_id, run=None, tested_commit=tested, state="missing"))
        current = self._read(Readiness, ["gh", "api", f"{endpoint}/pulls/{target.number}"])
        if current != pr:
            return observed("pending", "PR changed during observation. Resume with fresh evidence.", effective)
        if any(c.state not in ("success", "pending", "missing") for c in effective):
            return observed("red", "Repair failing required checks, then resume.", effective)
        if any(c.state != "success" for c in effective):
            return observed("pending", "Wait for required checks, then resume.", effective)
        return observed("ready", "Eligible for merge arming.", effective)

    def arm_auto_merge(self, repo: str, issue: int, branch: str, sha: str) -> MergeReceipt:
        try:
            return self._arm_auto_merge(repo, issue, branch, sha)
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

    def _queue_snapshot(self, repo: str, number: int) -> QueueSnapshot:
        owner, name = repo.split("/", 1)
        query = """query($owner:String!, $name:String!, $number:Int!) {
          repository(owner:$owner, name:$name) { pullRequest(number:$number) {
            number state headRefOid baseRefOid baseRefName isDraft
            autoMergeRequest { enabledAt mergeMethod }
            mergeQueueEntry { id state headCommit { oid } }
          } }
        }"""
        return self._read(QueueSnapshot, ["gh", "api", "graphql", "-f", f"query={query}",
            "-f", f"owner={owner}", "-f", f"name={name}", "-F", f"number={number}",
            "--jq", ".data.repository.pullRequest"])

    def _arm_auto_merge(self, repo: str, issue: int, branch: str, sha: str) -> MergeReceipt:
        head = commit_sha(sha)
        if head is None:
            raise MergeNotArmed("Merge arming requires a full 40-hex commit SHA.")
        pr = self.review_target(repo, issue, branch, head)
        rules = self._read(Rules, ["gh", "api", f"repos/{repo}/rules/branches/{pr.baseRefName}"])
        policies = [rule.parameters for rule in rules.root if rule.type == "merge_queue"]
        methods = {policy.merge_method for policy in policies}
        if len(methods) != 1 or next(iter(methods)) not in {"MERGE", "REBASE", "SQUASH"}:
            raise MergeNotArmed("Effective merge queue policy or method is unavailable. Restore readable queue policy.")
        method = next(iter(methods))
        assert method in ("MERGE", "REBASE", "SQUASH")

        def receipt(snapshot: QueueSnapshot) -> MergeReceipt | None:
            if (snapshot.number != pr.number or snapshot.headRefOid.lower() != head
                    or snapshot.baseRefOid != pr.baseRefOid or snapshot.baseRefName != pr.baseRefName
                    or snapshot.state != "OPEN" or snapshot.isDraft):
                raise MergeNotArmed("PR changed during merge handoff. Renew readiness, verification and review.")
            entry = snapshot.mergeQueueEntry
            request = snapshot.autoMergeRequest
            if entry is not None:
                if entry.headCommit.oid.lower() != head:
                    raise MergeNotArmed("Queue entry belongs to another head. Renew readiness and resume.")
                if entry.state not in {"QUEUED", "AWAITING_CHECKS", "MERGEABLE"}:
                    raise MergeNotArmed(f"Queue rejected or cannot accept the PR: {entry.state}. Repair and resume.")
                identity, status = entry.id, entry.state
            elif request is not None and request.mergeMethod == method:
                identity, status = request.enabledAt, "AUTO_MERGE_ENABLED"
            else:
                return None
            return MergeReceipt(repo=repo, pr=pr.number, source_head=head, base=pr.baseRefOid,
                                method=method, queue_policy=policies[0], identity=identity, status=status)

        if existing := receipt(self._queue_snapshot(repo, pr.number)):
            return existing
        timeout: CommandTimedOut | None = None
        try:
            self._run_command(merge_command(redact(repo), str(pr.number), head))
        except CommandTimedOut as exc:
            timeout = exc
        confirmed = receipt(self._queue_snapshot(repo, pr.number))
        if confirmed is None:
            raise MergeNotArmed("Merge handoff was not confirmed. Inspect queue admission and resume.") from timeout
        return confirmed

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

    def _read(self, model: type[Model], command: list[str], *, timeout: float | None = 30, body: str | None = None) -> Model:
        output = self._run_command(command, timeout=timeout, body=body)
        try:
            return model.model_validate_json(output)
        except ValidationError as exc:
            raise ForgeError(f"Invalid {model.__name__} response ({exc}).") from exc
