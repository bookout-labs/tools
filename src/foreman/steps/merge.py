"""Merge: arm the reviewed checked head, observe GitHub's merge result, release dependents and report it."""

from dataclasses import dataclass
import time
from typing import NoReturn

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt

from foreman.github import (MERGE_HOLD_VARIABLE, ForgeError, HandoffChanged, VariablesUnreadable, MergeNotArmed, QueueRejected,
                            merge_command, open_dependents)
from foreman.outcomes import Halt, stopped
from foreman.readiness import MergeReceipt, MergeState, MergeWait, Observation
from foreman.review_round import ReviewRound, history
from foreman.stage_output import HumanPrompt, spec_digest
from foreman.state import DeliveryState, DeliveryUpdate, ReviewState, WorkState, pull_request_number
from foreman.steps import StageInput, StageRun, cancel, human_stop

HALTS = [Halt.merge_held, Halt.merge_not_armed, Halt.readiness_unavailable, Halt.merged_unreviewed]
# GitHub reports a failure that the owning run repairs on the same pull request.
REPAIRS = ("red", "conflict", "queue_rejected")
# GitHub sends no event when it has computed mergeability, so the step that read the PR reads it again for up to a minute.
MERGEABILITY_SECONDS, REREAD_SECONDS = 60, 5
# GitHub's answer for a PR whose mergeability it has not computed, or a policy Foreman cannot read: Ready added again reads again.
UNAVAILABLE = ("policy", "uncomputed")


@dataclass(frozen=True)
class Reviewed:
    """The reviewed pull request that each merge step decides on, and the updates the steps share."""

    state: DeliveryState
    run: StageRun

    @property
    def target(self) -> tuple[str, int | None, str, str]:
        run, state = self.state["run"], self.state
        return run.repo, run.issue, state.get("work", WorkState()).branch, state["checks"].verified_head

    @property
    def report(self) -> tuple[ReviewRound | None, int | None]:
        """The last posted report and the number of the pull request it reviewed."""
        rounds = history({"rounds": self.state["review"].review_findings}).rounds
        return (rounds[-1] if rounds else None), pull_request_number(self.state)

    @property
    def answers(self) -> list[str]:  # The human's saved answers at the head_unverified gate. The last one may cancel the run.
        return [saved["answer"] for saved in self.state["plan"].human_qa if saved["stage"] == "merge"]

    def observe(self, merge: MergeState) -> tuple[Observation | None, MergeState]:
        repo, issue, branch, head = self.target
        assert issue is not None
        deadline = time.monotonic() + MERGEABILITY_SECONDS
        try:
            while (observation := self.run.forge.readiness(repo, issue, branch, head)).outcome == "uncomputed" and (
                    left := deadline - time.monotonic()) > 0:
                time.sleep(min(REREAD_SECONDS, left))
        except ForgeError:
            return None, merge
        return observation, merge.model_copy(update={"observation": observation.model_dump(mode="json")})

    @staticmethod
    def wait(pause: MergeWait | HumanPrompt) -> NoReturn:
        """Pause for a GitHub event or the human's answer. The runner restarts the stage with the answer saved, so a wake reads GitHub once."""
        interrupt(pause.model_dump())
        raise AssertionError("A paused merge stage restarts instead of resuming.")

    @staticmethod
    def halt(merge: MergeState, code: Halt, detail: str) -> DeliveryUpdate:
        return {**stopped(code, detail, "merge"), "merge": merge}

    def repair(self, merge: MergeState, detail: str, moved: str | None = None, *, conflict: bool = False) -> DeliveryUpdate:
        """Implement repairs the owned pull request with GitHub's evidence, after fetching a moved owned head. On a confirmed
        conflict with the base, the evidence tells prepare to make the base merge."""
        work, review = self.state.get("work", WorkState()), self.state["review"]
        checks = self.state["checks"].for_attempt().model_copy(update={"verify": "fail", "failure_output": f"GitHub: {detail}"})
        # A repaired head needs a fresh arm, so the old receipt is never reconciled again.
        return {"stop": "repair", "merge": merge.model_copy(update={"status": "not_armed", "receipt": None}), "checks": checks,
                "work": work.moved_to(moved, conflict=conflict),
                "review": ReviewState(review_findings=review.review_findings, settled_rulings=review.settled_rulings,
                                      review_run_id=review.review_run_id)}


def arm(state: DeliveryState, run: StageRun) -> DeliveryUpdate:
    if late := human_stop(state, run, "merge", Halt.readiness_unavailable, merging=True):
        # A stop ends the run here. One that came after GitHub merged the PR goes on to observe the merge.
        return {"stop": "closed_or_merged", **late, "merge": state.get("merge", MergeState())}
    pull, receipt, forge, saved_merge = Reviewed(state, run), state["review"].review_receipt, run.forge, state.get("merge", MergeState())
    (repo, issue, branch, head), (report, number) = pull.target, pull.report
    merge = MergeState(status="not_armed", receipt=saved_merge.receipt, command=merge_command(repo, "<number>", head or "<sha>"))
    if (not head or issue is None or not branch or report is None or number is None or report.severity or report.reviewed_head != head
            or not receipt or receipt["state"] != "COMMENTED" or receipt["commit_id"] != head):
        return pull.halt(merge, Halt.merge_not_armed, "Renew review and its posted receipt at the verified head.")
    if pull.answers[-1:] == ["cancel"]:  # A cancellation saved at the head_unverified gate ends the run there.
        return {"stop": "head_unverified", "merge": merge}
    if merge.receipt and (saved := MergeReceipt.model_validate(merge.receipt)).source_head == head and (saved.repo, saved.pr) == (repo, number):
        if saved_merge.status == "armed":
            # A wake restarts the stage at a confirmed arm, which observe reconciles at the current base.
            return {"stop": "already_armed", "merge": saved_merge}
        # An arm that may have landed before an interruption is adopted once and never written again.
        try:
            confirmed = forge.reconcile_merge(saved)
        except QueueRejected as exc:
            return pull.repair(merge, str(exc))
        except HandoffChanged:
            merge = merge.model_copy(update={"receipt": None})
        except MergeNotArmed as exc:
            return pull.halt(merge, Halt.merge_not_armed, str(exc))
        else:
            return {"stop": "already_armed", "merge": merge.model_copy(update={"status": "armed", "receipt": confirmed.model_dump(mode="json")})}
    try:
        hold = forge.variables(repo).get(MERGE_HOLD_VARIABLE)
    except VariablesUnreadable as exc:
        return pull.halt(merge, Halt.merge_held, f"could not read {MERGE_HOLD_VARIABLE}: {exc}")
    if hold:
        return pull.halt(merge, Halt.merge_held, f"{MERGE_HOLD_VARIABLE} is set" + (f": {hold}" if hold.strip() else "."))
    observation, merge = pull.observe(merge)
    if observation is None:
        return pull.halt(merge, Halt.readiness_unavailable, "GitHub merge readiness could not be read.")
    pr = observation.pr
    if pr.merged or observation.outcome == "closed":
        return {"stop": "closed_or_merged", "merge": merge}
    if observation.outcome == "head_changed":
        return {"stop": "head_unverified", "merge": merge}
    if observation.outcome == "retargeted" or pr.number != number:
        return pull.halt(merge, Halt.merge_not_armed, observation.detail())
    if observation.outcome in UNAVAILABLE:
        return pull.halt(merge, Halt.readiness_unavailable, observation.detail())
    # A failure goes to its repair before a moved base buys a review that the repair would discard.
    if observation.outcome in REPAIRS:
        return pull.repair(merge, observation.detail(), conflict=observation.outcome == "conflict")
    if pr.base.sha != report.base:
        # The checked head stands, but its review read an older base.
        return {"stop": "reviewable_head", "merge": merge}
    if observation.outcome == "pending":
        pull.wait(MergeWait(step="arm", detail=observation.detail()))
    try:
        armed = forge.arm_auto_merge(repo, issue, branch, head, observation)
    except QueueRejected as exc:
        return pull.repair(merge, str(exc))
    except MergeNotArmed as exc:
        return pull.halt(merge, Halt.merge_not_armed, str(exc))
    update = ({"status": "skipped", "command": merge_command(repo, "<number>", head)} if armed is None
              else {"status": "armed", "receipt": armed.model_dump(mode="json")})
    return {"stop": "armed", "merge": merge.model_copy(update=update)}


def observe(state: DeliveryState, run: StageRun) -> DeliveryUpdate:
    pull, forge, merge = Reviewed(state, run), run.forge, state.get("merge", MergeState())
    (repo, _, _, head), (_, number) = pull.target, pull.report
    # A cancel answer from the head_unverified question. One that came after GitHub merged the PR is recorded on the merge.
    late: DeliveryUpdate = cancel(state, run, "merge", Halt.readiness_unavailable, merging=True) if pull.answers[-1:] == ["cancel"] else {}
    if late.get("stop"):
        return {**late, "merge": merge}
    observation, merge = pull.observe(merge)
    if observation is None:
        return pull.halt(merge, Halt.readiness_unavailable, "GitHub could not report the pull request.")
    pr = observation.pr
    if pr.merged:
        if (pr.number, pr.head.sha) != (number, head):
            return pull.halt(merge.model_copy(update={"observed_head": pr.head.sha}), Halt.merged_unreviewed,
                             f"GitHub merged PR #{pr.number} at {pr.head.sha}, not the reviewed head {head}. No delivery credit or dependent release.")
        return {**late, "stop": "merged", "merge": merge.model_copy(update={"status": "merged", "merge_commit_sha": pr.merge_commit_sha or ""})}
    if observation.outcome == "closed":
        return {"stop": "closed", "merge": merge.model_copy(update={"status": "closed"})}
    if observation.outcome == "retargeted" or pr.number != number:
        return pull.halt(merge, Halt.merge_not_armed, observation.detail())
    if observation.outcome == "head_changed":
        try:
            owned = forge.descends(repo, head, pr.head.sha)
        except ForgeError as exc:
            return pull.halt(merge, Halt.readiness_unavailable, f"GitHub could not compare {pr.head.sha} with {head}: {exc}")
        if owned:
            return pull.repair(merge, f"The owned pull request moved to {pr.head.sha}.", pr.head.sha)
        # Only Git ancestry from the checked head adopts a revision, so the human restores it or cancels. The runner saves each
        # answer bound to its question, so a saved answer opens a new question and never answers it.
        question = (f"PR #{pr.number} moved to {pr.head.sha}, which does not descend from the checked head {head}. "
                    "Make the PR head descend from it, or cancel.")
        revision = spec_digest(["merge", repo, pr.number, pr.head.sha, *pull.answers])
        pull.wait(HumanPrompt(stage="merge", question=question, revision=revision, path=["merge"]))
    if observation.outcome in REPAIRS:
        return pull.repair(merge, observation.detail(), conflict=observation.outcome == "conflict")
    if observation.outcome in UNAVAILABLE:
        return pull.halt(merge, Halt.readiness_unavailable, observation.detail())
    if merge.receipt is None:
        if merge.status != "skipped":
            return pull.halt(merge, Halt.merge_not_armed, f"PR #{pr.number} is open and not armed.")
        status = "dry run, so Foreman did not arm it"
    else:
        # The queue tests the head on top of the latest base, so the head and queue entry bind the receipt now.
        try:
            status = forge.reconcile_merge(MergeReceipt.model_validate(merge.receipt).model_copy(update={"base": pr.base.sha})).status
        except QueueRejected as exc:
            return pull.repair(merge, str(exc))
        except HandoffChanged as exc:
            status = str(exc)
        except MergeNotArmed as exc:
            return pull.halt(merge, Halt.readiness_unavailable, str(exc))
    pull.wait(MergeWait(step="observe", detail=f"PR #{pr.number} at {head}: {status}. Waiting for GitHub to report the merge."))


def release(state: DeliveryState, run: StageRun) -> DeliveryUpdate:
    repo, issue = state["run"].repo, state["run"].issue
    assert issue is not None
    try:
        released = open_dependents(run.forge, repo, issue)
    except ForgeError as exc:
        return Reviewed.halt(state.get("merge", MergeState()), Halt.readiness_unavailable, f"Dependent release could not read GitHub: {exc}")
    # The receiver starts each one through triage.admit, so release prints nothing for a person to start.
    return {"released": released, "stop": "released"}


def report(state: DeliveryState, run: StageRun) -> DeliveryUpdate:
    if state.get("halt"):
        return {}
    # Released dependents follow a confirmed merge, which is the run's typed result.
    return {"path": ["merge"], "stop": "merged"} if state.get("stop") == "released" else {"path": ["merge"]}


def compile_stage(run: StageRun) -> CompiledStateGraph[DeliveryState, None, StageInput, DeliveryState]:
    def route(state: DeliveryState) -> str:
        return "halted" if state.get("halt") else state.get("stop", "")

    declarations: dict[str, object] = {"halts": [halt.value for halt in HALTS], "retries": {}}
    return (StateGraph[DeliveryState, None, StageInput, DeliveryState](DeliveryState, input_schema=StageInput, output_schema=DeliveryState)
            .add_node("arm", run.bind(arm))
            .add_node("observe", run.bind(observe))
            .add_node("release", run.bind(release))
            .add_node("report", run.bind(report))
            .add_edge(START, "arm")
            .add_conditional_edges("arm", route, {
                "halted": "report", "cancelled": "report", "reviewable_head": "report", "repair": "report",
                "armed": "observe", "already_armed": "observe", "closed_or_merged": "observe", "head_unverified": "observe"})
            .add_conditional_edges("observe", route, {
                "halted": "report", "cancelled": "report", "closed": "report", "reviewable_head": "report", "repair": "report",
                "merged": "release"})
            .add_conditional_edges("release", route, {"released": "report", "halted": "report"})
            .add_edge("report", END)
            .compile().with_config({"metadata": {"contract_id": "merge", "stage_declarations": declarations}}))
