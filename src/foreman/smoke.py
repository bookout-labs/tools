"""Pure smoke verdicts over a final delivery checkpoint."""

from dataclasses import dataclass
from typing import Literal

from pydantic import ValidationError

from foreman.outcomes import Halt
from foreman.readiness import MergeReceipt, MergeState
from foreman.state import DeliveryResult
from foreman.workspace import WorkState


@dataclass(frozen=True)
class SmokeVerdict:
    exit_code: Literal[0, 1, 78]
    condition: str
    attempt: str


def smoke_verdict(state: DeliveryResult) -> SmokeVerdict:
    """Prefer contract defects to unavailable prerequisites, including after fallback.

    A recorded ``ran`` outcome carries the adapter's identity check. The model
    label alone is not identity evidence, and Grok ledger aliases are valid.
    """
    attempts = state.get("attempts", [])
    last = "checkpoint"
    unavailable: SmokeVerdict | None = None
    for index, attempt in enumerate(attempts, 1):
        last = f"{index} {attempt['stage']} {attempt['model'] or 'no model'}"
        outcome = attempt["outcome"]
        if outcome in {"quota", "cli_missing"}:
            if unavailable is None:
                unavailable = SmokeVerdict(78, f"attempt.outcome={outcome}", last)
        elif outcome != "ran":
            return SmokeVerdict(1, f"attempt.outcome={outcome}", last)
        else:
            if not attempt["model"]:
                return SmokeVerdict(1, "attempt.model_missing", last)
            provenance = attempt.get("provenance")
            if provenance and provenance["same_family"] and (
                provenance["reason"] != "preceding_candidates_unavailable" or unavailable is None
            ):
                return SmokeVerdict(1, "attempt.fallback", last)
            usage = attempt["usage"]
            if usage is None or not any(usage[key] for key in ("input", "output", "cache_read", "cache_creation")):
                return SmokeVerdict(1, "attempt.usage_unknown", last)
            if (attempt["model"] or "").startswith("claude-") and usage["cost_usd"] is None:
                return SmokeVerdict(1, "attempt.usage_unknown", last)

    halt, stop = state.get("halt"), state.get("stop", "")
    unavailable_halt = halt in {Halt.env_unavailable, Halt.worker_unavailable}
    if halt and not unavailable_halt:
        return SmokeVerdict(1, f"halt={halt}", last)
    path = state["path"]
    if not path or (not unavailable_halt and path[-1:] != ["merge"]):
        return SmokeVerdict(1, "terminal.merge", last)
    # The synthetic pull request ends queued, waiting for GitHub to report the merge, or merged.
    if stop not in ({halt} if unavailable_halt else {"merge_pending", "merged"}):
        return SmokeVerdict(1, f"stop={stop or 'missing'}", last)

    checks = state["checks"]
    head = checks.verified_head
    work = state.get("work", WorkState())
    merge = state.get("merge", MergeState())
    review = state["review"]
    review_receipt = review.review_receipt
    receipt: MergeReceipt | None = None
    if merge.receipt is not None:
        try:
            receipt = MergeReceipt.model_validate(merge.receipt)
        except ValidationError:
            return SmokeVerdict(1, "merge.receipt.invalid", last)
    if merge.observed_head and head and merge.observed_head != head:
        return SmokeVerdict(1, "merge.observed_head", last)
    if checks.verify == "fail" or (checks.check_exit_code not in (None, 0) and halt not in {Halt.env_unavailable}):
        return SmokeVerdict(1, "checks.verify", last)
    if receipt and receipt.repo != state["run"].repo:
        return SmokeVerdict(1, "merge.receipt.repo", last)
    if receipt and receipt.status == "UNMERGEABLE":
        return SmokeVerdict(1, "merge.receipt.rejected", last)
    if unavailable_halt:
        if merge.status == "armed":
            return SmokeVerdict(1, "merge.armed_with_halt", last)
        return unavailable or SmokeVerdict(78, f"halt={halt or stop}", last)

    for condition, passed in (
        ("attempts.nonempty", bool(attempts)),
        ("checks.verify", checks.verify == "pass" and checks.check_exit_code == 0),
        ("verified_head", bool(head)),
        ("published_head", work.published_head == head),
        ("reviewed_head", review.reviewed_head == head),
        ("review_receipt", review_receipt is not None and review_receipt["commit_id"] == head),
        ("merge.receipt", receipt is not None and receipt.source_head == head),
        ("merge.armed", merge.status == ("merged" if stop == "merged" else "armed")),
    ):
        if not passed:
            return SmokeVerdict(1, condition, last)
    return unavailable or SmokeVerdict(0, "pass", last)
