"""Mirror a run's checkpoint onto its issue: one run label and one status comment.

Warp posts progress and links in the originating issue (https://docs.warp.dev/factories/integrations/github/).
Foreman has no dashboard, so the issue holds the run's state. The checkpoint decides; a label never grants authority.
"""

from dataclasses import dataclass, field
from typing import Literal

from foreman._raw import is_dict
from foreman.github import BOT_LOGIN, DOING_LABEL, DONE_LABEL, NEEDS_ANSWER_LABEL, READY_LABEL, RUN_LABELS, STOPPED_LABEL, Forge, run_label
from foreman.outcomes import HALTS, PAUSES, Halt
from foreman.readiness import MergeState
from foreman.stage_output import HumanPrompt, VerifyState
from foreman.state import DeliveryResult
from foreman.workspace import commit_sha

STATUS_MARKER = "<!-- foreman status -->"
# GitHub refuses a comment body over 65,536 characters. The run report is never cut, so the run's state above it is.
EVIDENCE_LIMIT = 16_000
OUTCOMES = {
    "answered": "Foreman answered on this issue.",
    "children_filed": "Foreman filed this issue's slices as child issues.",
    "merged": "The reviewed pull request merged.",
    "reverted": "The production check failed for the merge, so Foreman filed a Ready revert issue and a regression issue, and released no dependents.",
    "closed": "The pull request closed without merging.",
    "cancelled": "The run was cancelled.",
    "no_change": "The issue needed no code change.",
}


@dataclass(frozen=True)
class Status:
    """The issue's run label, or None before admission, and its status comment: the run's state, then the run report of
    an admitted run. Facts are validated values posted verbatim."""

    label: str | None
    text: str
    facts: list[str] = field(default_factory=list[str])
    report: str = ""


def running(stage: str, report: str = "") -> Status:
    return Status(DOING_LABEL, f"Foreman is running {stage}.", report=report)


def refused(code: Halt, detail: str, *, resumed: bool) -> Status:
    """The runner's report of a start it refused before the graph. It is no halt, so the run label stays as it is."""
    return Status(None, f"Foreman did not {'resume' if resumed else 'start'} this run ({code}).\n\n{detail}")


def not_continued(failed: str, answer: str, refused_app: bool, held: Literal["halted", "gate"], current: Status) -> Status:
    """The runner's report of a wake whose read, or close of the PR, failed, above the run's state, which nothing changed.
    What it says next follows from the run's saved state, so a wake carries nothing from its delivery (rules.gates.continuation)."""
    fix = "Fix the Foreman GitHub App's access to this repository." if refused_app else "No setting needs to change."
    again = {"halted": "Post the stop again.", "gate": "Post the stop or the answer again."}[held]
    report = f"Foreman did not continue this run (git_failed).\n\n{failed} failed because GitHub {answer}. {fix} {again}"
    return Status(current.label, f"{report}\n\n{current.text}", current.facts, current.report)


def _pull_request(result: DeliveryResult) -> list[str]:
    """The pull request GitHub last reported, or the one review targeted."""
    pr = (result.get("merge", MergeState()).observation or {}).get("pr")
    target = (result.get("dispatch_plan") or {}).get("target")
    number = pr.get("number") if is_dict(pr) else target["number"] if target else None
    return [f"Pull request: #{number}"] if isinstance(number, int) and not isinstance(number, bool) else []


KINDS = {"hand_fix": "Hand fix", "revert": "Revert", "production_check": "Failed production check"}


def _escapes(result: DeliveryResult) -> str:
    """Each escape recorded on the run, with its evidence (rules.reporting)."""
    escapes = result.get("merge", MergeState()).escapes
    return "".join(f"\n\nEscape: {KINDS[escape.kind]} ({escape.key}).\n\n{escape.evidence}" for escape in escapes)


def _scores(result: DeliveryResult) -> str:
    """Each scorer's grade with its spend, which is outside the run's budget (rules.reporting)."""
    return "".join(f"\n\nScore: {score['scorer']} {score['label'] or 'unknown'}"
                   + (f" by {score['model']}" if score["model"] else "")
                   + f". Scorer spend: {'unknown' if score['spend_usd'] is None else '$' + score['spend_usd']}, outside the run's budget."
                   + f"\n\n{score['reason']}" for score in result.get("scores", []))


def status_of(result: DeliveryResult, stage: str | None, prompt: HumanPrompt | None) -> Status | None:
    """The status a finished invocation leaves on the issue, or None when the issue stays unadmitted."""
    stop, detail = result["stop"], result.get("halt_detail", "")
    if stop in ("", "blocked"):
        return None  # An unfinished invocation keeps the stage's Doing, and an unadmitted issue keeps Ready.
    if (code := result.get("halt")) is not None:
        policy = HALTS[Halt(code)]
        retry = " Rerun Foreman on this issue to resume." if policy.recovery == "retry" else ""
        # Before admission Foreman owns no label, so Ready stays for the rerun that admits the issue.
        label = STOPPED_LABEL if result["run"].base_sha else None
        return Status(label, f"Foreman halted with {code}. {policy.reason}{retry}\n\n{detail}{_escapes(result)}{_scores(result)}")
    if stop == "needs_human":
        # A pending question carries its reply line. A question saved without one shows its recorded text.
        question, facts = (prompt.question, [prompt.reply] if prompt.revision else []) if prompt else (detail, [])
        return Status(NEEDS_ANSWER_LABEL, f"Foreman needs an answer in {stage}.\n\n{question}", facts)
    if stop in PAUSES:
        return Status(DOING_LABEL, f"Foreman is running {stage} and waits for GitHub to merge the pull request.\n\n{detail}",
                      _pull_request(result))
    text = OUTCOMES[stop]
    facts: list[str] = []
    merge = result.get("merge", MergeState())
    if stop in ("merged", "reverted", "closed"):
        facts = _pull_request(result)
    if stop in ("merged", "reverted"):
        heads = (("Verified head", result["checks"].verified_head), ("Merge commit", merge.merge_commit_sha))
        facts += [f"{name}: {sha}" for name, value in heads if (sha := commit_sha(value))]
        if evidence := result.get("verify", VerifyState()).evidence_url:
            facts.append(f"Verify evidence: {evidence}")
    if stop == "reverted":
        facts += [f"{name}: #{number}" for name, number in (("Revert issue", merge.revert_issue), ("Regression issue", merge.regression_issue))]
    facts += [f"Released: {issue}" for issue in result.get("released", [])]
    return Status(DONE_LABEL, text + _escapes(result) + _scores(result), facts)


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
    text = status.text
    if len(text) > EVIDENCE_LIMIT:
        text = f"{text[:EVIDENCE_LIMIT]}\n\nForeman cut this text at {EVIDENCE_LIMIT} characters."
    forge.upsert_comment(repo, number, text, [*status.facts, *filter(None, [status.report])], marker=STATUS_MARKER)
