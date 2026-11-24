"""Mirror a run's checkpoint onto its issue: one run label and one status comment.

Warp posts progress and links in the originating issue (https://docs.warp.dev/factories/integrations/github/).
Foreman has no dashboard, so the issue holds the run's state. The checkpoint decides; a label never grants authority.
"""

from dataclasses import dataclass, field

from foreman._raw import is_dict
from foreman.github import BOT_LOGIN, DOING_LABEL, DONE_LABEL, NEEDS_ANSWER_LABEL, READY_LABEL, RUN_LABELS, Forge, run_label
from foreman.outcomes import HALTS, PAUSES, Halt
from foreman.readiness import MergeState
from foreman.stage_output import HumanPrompt
from foreman.state import DeliveryResult
from foreman.workspace import commit_sha

STATUS_MARKER = "<!-- foreman status -->"
OUTCOMES = {
    "answered": "Foreman answered on this issue.",
    "children_filed": "Foreman filed this issue's slices as child issues.",
    "merged": "The reviewed pull request merged.",
    "closed": "The pull request closed without merging.",
    "cancelled": "The run was cancelled.",
    "no_change": "The issue needed no code change.",
}


@dataclass(frozen=True)
class Status:
    """The issue's run label, or None before admission, and its status comment. Facts are validated values posted verbatim."""

    label: str | None
    text: str
    facts: list[str] = field(default_factory=list[str])


def running(stage: str) -> Status:
    return Status(DOING_LABEL, f"Foreman is running {stage}.")


def _pull_request(result: DeliveryResult) -> list[str]:
    """The pull request GitHub last reported, or the one review targeted."""
    pr = (result.get("merge", MergeState()).observation or {}).get("pr")
    target = (result.get("dispatch_plan") or {}).get("target")
    number = pr.get("number") if is_dict(pr) else target["number"] if target else None
    return [f"Pull request: #{number}"] if isinstance(number, int) and not isinstance(number, bool) else []


def status_of(result: DeliveryResult, stage: str | None, prompt: HumanPrompt | None, watch_repo: str) -> Status | None:
    """The status a finished invocation leaves on the issue, or None when the issue stays unadmitted."""
    stop, detail = result["stop"], result.get("halt_detail", "")
    if stop in ("", "blocked"):
        return None  # An unfinished invocation keeps the stage's Doing, and an unadmitted issue keeps Ready.
    if (code := result.get("halt")) is not None:
        policy = HALTS[Halt(code)]
        retry = " Rerun Foreman on this issue to resume." if policy.recovery == "retry" else ""
        record = result.get("watch")
        facts = [f"Watch: {watch_repo}#{record['issue']}"] if record is not None else []
        # Before admission Foreman owns no label, so Ready stays for the rerun that admits the issue.
        label = DONE_LABEL if result["run"].base_sha else None
        return Status(label, f"Foreman halted with {code}. {policy.reason}{retry}\n\n{detail}", facts)
    if stop == "needs_human":
        # A pending question carries its reply line. A question saved without one shows its recorded text.
        question, facts = (prompt.question, [prompt.reply] if prompt.revision else []) if prompt else (detail, [])
        return Status(NEEDS_ANSWER_LABEL, f"Foreman needs an answer in {stage}.\n\n{question}", facts)
    if stop in PAUSES:
        reason = "worker capacity" if stop == "quota_exhausted" else "GitHub to merge the pull request"
        return Status(DOING_LABEL, f"Foreman is running {stage} and waits for {reason}.\n\n{detail}", _pull_request(result))
    text = OUTCOMES[stop]
    facts: list[str] = []
    if stop in ("merged", "closed"):
        facts = _pull_request(result)
    if stop == "merged":
        heads = (("Verified head", result["checks"].verified_head), ("Merge commit", result.get("merge", MergeState()).merge_commit_sha))
        facts += [f"{name}: {sha}" for name, value in heads if (sha := commit_sha(value))]
    facts += [f"Released: {issue}" for issue in result.get("released", [])]
    return Status(DONE_LABEL, text, facts)


def _run_labels(forge: Forge, repo: str, number: int) -> list[str] | None:
    """The run labels the issue shows, as GitHub names them, or None when GitHub answers for another issue."""
    issue = forge.load_issue(repo, number)
    if (issue["repo"].lower(), issue["number"]) != (repo.lower(), number):
        return None
    return [name for name in issue["labels"] if run_label(name)]


def shows(forge: Forge, repo: str, number: int, label: str) -> bool:
    """The issue carries only this run label and one Foreman status comment."""
    labels = _run_labels(forge, repo, number)
    owned = [comment for comment in forge.issue_comment_records(repo, number) if comment.user is not None
             and comment.user.login.casefold() == BOT_LOGIN.casefold() and (comment.body or "").endswith(STATUS_MARKER)]
    return labels is not None and [run_label(name) for name in labels] == [label] and len(owned) == 1


def write_status(forge: Forge, repo: str, number: int, status: Status) -> None:
    """Converge the issue on the status, writing only what differs. Other labels stay."""
    if (labels := _run_labels(forge, repo, number)) is None:
        return  # Another issue never takes this run's status.
    if status.label is None:
        if READY_LABEL not in map(run_label, labels):
            return  # A delivery that outlived its label leaves the issue alone.
    else:
        add = [] if status.label in map(run_label, labels) else [status.label]
        remove = [name for name in labels if run_label(name) != status.label]
        if add:
            forge.ensure_label(repo, status.label, RUN_LABELS[status.label])
        if add or remove:
            forge.edit_labels(repo, number, add, remove)
    forge.upsert_comment(repo, number, status.text, status.facts, marker=STATUS_MARKER)
