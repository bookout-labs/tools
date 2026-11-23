"""Merge: arm the reviewed checked head, observe GitHub's merge result, release dependents and report it."""

from collections.abc import Callable
from dataclasses import dataclass
from functools import wraps
from typing import Literal, NoReturn

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt

from foreman.github import (
    MERGE_HOLD_VARIABLE, RELEASE_LABEL, ForgeError, HandoffChanged, MergeHoldUnreadable, MergeNotArmed, QueueRejected, merge_command,
)
from foreman.outcomes import Halt, stopped
from foreman.readiness import MergeReceipt, MergeState, MergeWait, Observation
from foreman.review_round import ReviewRound, history
from foreman.stage_output import HumanAnswer, HumanPrompt, spec_digest
from foreman.state import DeliveryState, DeliveryUpdate, ReviewState, WorkState
from foreman.steps import StageInput, StageRun

HALTS = [Halt.merge_held, Halt.merge_not_armed, Halt.readiness_unavailable]
# GitHub reports a failure that the owning run repairs on the same pull request.
REPAIRS = ("red", "conflict", "queue_rejected")


@dataclass(frozen=True)
class Reviewed:
    """The reviewed pull request that each merge step decides on, and the updates the steps share."""

    state: DeliveryState
    run: StageRun

    @property
    def target(self) -> tuple[str, int | None, str, str]:
        state = self.state
        return state["run"].repo, state["run"].issue, state.get("work", WorkState()).branch, state["checks"].verified_head

    @property
    def report(self) -> tuple[ReviewRound | None, int | None]:
        """The last posted report and the number of the pull request it reviewed."""
        rounds = history({"rounds": self.state["review"].review_findings}).rounds
        target = (self.state.get("dispatch_plan") or {}).get("target")
        return (rounds[-1] if rounds else None), (target["number"] if target else None)

    def observe(self, merge: MergeState) -> tuple[Observation | None, MergeState]:
        repo, issue, branch, head = self.target
        assert issue is not None
        try:
            observation = self.run.forge.readiness(repo, issue, branch, head)
        except ForgeError:
            return None, merge
        return observation, merge.model_copy(update={"observation": observation.model_dump(mode="json")})

    @staticmethod
    def wait(step: Literal["arm", "observe"], detail: str) -> NoReturn:
        """Pause until a GitHub event. The runner then restarts the stage, so each wake reads GitHub once."""
        interrupt(MergeWait(step=step, detail=detail).model_dump())
        raise AssertionError("A waiting merge stage restarts instead of resuming.")

    @staticmethod
    def halt(merge: MergeState, code: Halt, detail: str) -> DeliveryUpdate:
        return {**stopped(code, detail, "merge"), "merge": merge}

    def repair(self, merge: MergeState, detail: str, moved: str | None = None) -> DeliveryUpdate:
        """Implement repairs the owned pull request with GitHub's evidence, after fetching a moved owned head."""
        work, review = self.state.get("work", WorkState()), self.state["review"]
        checks = self.state["checks"].for_attempt().model_copy(update={"verify": "fail", "failure_output": f"GitHub: {detail}"})
        # A repaired head needs a fresh arm, so the old receipt is never reconciled again.
        return {"stop": "repair", "merge": merge.model_copy(update={"status": "not_armed", "receipt": None}), "checks": checks,
                "work": WorkState(branch=work.branch, base=work.base, repair_head=moved),
                "review": ReviewState(review_findings=review.review_findings, settled_rulings=review.settled_rulings,
                                      review_run_id=review.review_run_id)}


def arm(state: DeliveryState, run: StageRun) -> DeliveryUpdate:
    pull, review, forge = Reviewed(state, run), state["review"], run.forge
    (repo, issue, branch, head), (report, number) = pull.target, pull.report
    merge = MergeState(status="not_armed", receipt=state.get("merge", MergeState()).receipt,
                       command=merge_command(repo, "<number>", head or "<sha>"))
    receipt = review.review_receipt
    if (not head or issue is None or not branch or report is None or number is None or report.severity or report.reviewed_head != head
            or not receipt or receipt["state"] != "COMMENTED" or receipt["commit_id"] != head):
        return pull.halt(merge, Halt.merge_not_armed, "Renew review and its posted receipt at the verified head.")
    try:
        hold = forge.merge_hold(repo)
    except MergeHoldUnreadable as exc:
        return pull.halt(merge, Halt.merge_held, f"could not read {MERGE_HOLD_VARIABLE}: {exc}")
    if hold:
        return pull.halt(merge, Halt.merge_held, f"{MERGE_HOLD_VARIABLE} is set" + (f": {hold}" if hold.strip() else "."))
    if merge.receipt and (saved := MergeReceipt.model_validate(merge.receipt)).source_head == head and (saved.repo, saved.pr) == (repo, number):
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
    if observation.outcome == "policy":
        return pull.halt(merge, Halt.readiness_unavailable, observation.detail())
    # A failure goes to its repair before a moved base buys a review that the repair would discard.
    if observation.outcome in REPAIRS:
        return pull.repair(merge, observation.detail())
    if pr.base.sha != report.base:
        # The checked head stands, but its review read an older base.
        return {"stop": "reviewable_head", "merge": merge}
    if observation.outcome == "pending":
        pull.wait("arm", observation.detail())
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
    pull, forge, merge, plan = Reviewed(state, run), run.forge, state.get("merge", MergeState()), state["plan"]
    (repo, _, _, head), (_, number) = pull.target, pull.report
    observation, merge = pull.observe(merge)
    if observation is None:
        return pull.halt(merge, Halt.readiness_unavailable, "GitHub could not report the pull request.")
    pr = observation.pr
    if pr.merged:
        if (pr.number, pr.head.sha) != (number, head):
            return pull.halt(merge.model_copy(update={"observed_head": pr.head.sha}), Halt.merge_not_armed,
                             f"GitHub merged PR #{pr.number} at {pr.head.sha}, not the reviewed head {head}. No delivery credit or dependent release.")
        return {"stop": "merged", "merge": merge.model_copy(update={"status": "merged", "merge_commit_sha": pr.merge_commit_sha or ""})}
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
        # Only Git ancestry from the checked head adopts a revision, so the human restores it or cancels.
        question = (f"PR #{pr.number} moved to {pr.head.sha}, which does not descend from the checked head {head}. "
                    "Make the PR head descend from it, or cancel.")
        # Each saved answer opens a new question, so a reply already given never answers the next one.
        revision = spec_digest(["merge", repo, pr.number, pr.head.sha, *(saved["answer"] for saved in plan.human_qa if saved["stage"] == "merge")])
        answer = interrupt(HumanPrompt(stage="merge", question=question, revision=revision, path=["merge"]).model_dump())
        if answer == "cancel":
            return {"stop": "cancelled", "merge": merge}
        # The evidence is saved bound to the PR and observed head, and Foreman reads GitHub again.
        plan = plan.model_copy(update={"human_qa": [*plan.human_qa, HumanAnswer(stage="merge", question=question, answer=answer, revision=revision)]})
        return {"plan": plan, **observe({**state, "plan": plan, "merge": merge}, run)}
    if observation.outcome in REPAIRS:
        return pull.repair(merge, observation.detail())
    if observation.outcome == "policy":
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
    pull.wait("observe", f"PR #{pr.number} at {head}: {status}. Waiting for GitHub to report the merge.")


def release(state: DeliveryState, run: StageRun) -> DeliveryUpdate:
    repo, issue, forge = state["run"].repo, state["run"].issue, run.forge
    assert issue is not None
    try:
        parent = forge.parent_issue(repo, issue)
        for sibling in forge.sub_issues(repo, parent.number) if parent else []:
            if sibling.number == issue or sibling.state != "open":
                continue
            blockers = forge.blockers(repo, sibling.number)
            others = [blocker for blocker in blockers if not (blocker.in_repo(repo) and blocker.number == issue)]
            # As triage admits, a dependent starts once every other prerequisite closed, in-repo by a merge. Its label makes replays no-ops.
            if (len(others) < len(blockers) and all(
                    blocker.state == "closed" and (not blocker.in_repo(repo) or forge.is_merged(repo, blocker.number)) for blocker in others)
                    and RELEASE_LABEL not in forge.load_issue(repo, sibling.number)["labels"]):
                forge.add_label(repo, sibling.number, RELEASE_LABEL)
    except ForgeError as exc:
        return Reviewed.halt(state.get("merge", MergeState()), Halt.readiness_unavailable, f"Dependent release could not read or label GitHub: {exc}")
    return {"stop": "released"}


def report(state: DeliveryState, run: StageRun) -> DeliveryUpdate:
    # Released dependents follow a confirmed merge, which is the run's typed result.
    return {} if state.get("halt") else {"path": ["merge"], "stop": "merged"} if state.get("stop") == "released" else {"path": ["merge"]}


def compile_stage(run: StageRun) -> CompiledStateGraph[DeliveryState, None, StageInput, DeliveryState]:
    def bind(step: Callable[[DeliveryState, StageRun], DeliveryUpdate]) -> Callable[[DeliveryState], DeliveryUpdate]:
        # The bound node keeps the step's identity, so introspection names the step as its owner.
        @wraps(step)
        def node(state: DeliveryState) -> DeliveryUpdate:
            return step(state, run)
        return node

    def route(state: DeliveryState) -> str:
        return "halted" if state.get("halt") else state.get("stop", "")

    declarations: dict[str, object] = {"halts": [halt.value for halt in HALTS], "retries": {}}
    return (StateGraph[DeliveryState, None, StageInput, DeliveryState](DeliveryState, input_schema=StageInput, output_schema=DeliveryState)
            .add_node("arm", bind(arm))
            .add_node("observe", bind(observe))
            .add_node("release", bind(release))
            .add_node("report", bind(report))
            .add_edge(START, "arm")
            .add_conditional_edges("arm", route, {
                "halted": "report", "reviewable_head": "report", "repair": "report",
                "armed": "observe", "already_armed": "observe", "closed_or_merged": "observe", "head_unverified": "observe"})
            .add_conditional_edges("observe", route, {
                "halted": "report", "cancelled": "report", "closed": "report", "reviewable_head": "report", "repair": "report",
                "merged": "release"})
            .add_conditional_edges("release", route, {"released": "report", "halted": "report"})
            .add_edge("report", END)
            .compile().with_config({"metadata": {"contract_id": "merge", "stage_declarations": declarations}}))
