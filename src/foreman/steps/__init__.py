"""The adapters and worker conversation that every stage's steps share."""

from collections.abc import Callable
from dataclasses import dataclass
from functools import wraps

from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt

from foreman.dispatch import DispatchInput, DispatchResult, DispatchRun, DispatchState
from foreman.github import Forge, ForgeError, human_comment_records
from foreman.outcomes import Halt, stopped
from foreman.stage_output import AttemptRecord, DispatchStage, GateStage, HumanAnswer, HumanPrompt, PlanState, ReviewState, spec_digest
from foreman.state import ChecksState, DeliveryFields, DeliveryState, DeliveryUpdate, RunState
from foreman.workspace import CheckLog, WorkState, Workspace

DispatchGraph = CompiledStateGraph[DispatchState, DispatchRun, DispatchInput, DispatchResult]
STOP = "foreman stop"
CANCELLATION = "Foreman cancelled this run, so it closed this pull request. The branch stays in place."
TOO_LATE = "The human's stop came after GitHub merged the pull request, too late to cancel the run."


class StageInput(DeliveryFields, total=False):
    """Everything but the step path, which a stage returns as its own entries."""

    review: ReviewState
    plan: PlanState
    checks: ChecksState
    run: RunState
    stop: str


@dataclass(frozen=True)
class StageRun:
    """The live adapters and delivery callbacks that the stage steps share."""

    workspace: Workspace
    forge: Forge
    check_log: CheckLog | None
    max_rounds: int
    human_login: str
    dispatch: Callable[[DispatchStage, DeliveryState, str], DispatchRun]
    apply: Callable[[DispatchStage, DeliveryState, DispatchResult], DeliveryUpdate]

    def bind(self, step: Callable[[DeliveryState, "StageRun"], DeliveryUpdate]) -> Callable[[DeliveryState], DeliveryUpdate]:
        """The step as a graph node. The node keeps the step's identity, so introspection names the step as its owner."""
        @wraps(step)
        def node(state: DeliveryState) -> DeliveryUpdate:
            return step(state, self)
        return node

    def consult(
        self, graph: DispatchGraph, name: GateStage, state: DeliveryState, scope: Callable[[DeliveryState], list[object]],
        *, owned: str = "", answered: Callable[[DeliveryState, str], DeliveryUpdate] = lambda state, answer: {},
        route: Callable[[DeliveryState, DispatchResult], tuple[DeliveryUpdate, str]] = lambda state, result: ({}, result.get("fingerprint", "")),
    ) -> tuple[DeliveryUpdate, str]:
        """Dispatch a worker until it returns no question. Each question waits at the stage's gate for the human.

        The scope reads the Delivery state the worker's answer left and binds the question to its subject, so the question
        posts its reply marker and only the human's reply to that question resumes the step (rules.gates.checkpoint).
        Route reads a result without a question and returns the stage's stop or a question for the result that matches
        no route, with the fingerprint of the edits the worker then owns. That question opens the same gate, and its
        attempt records the routing defect for the scorers (rules.routing). A resumed step replays route, so route
        returns its first answer again. Returns the applied update and the fingerprint of the edits the worker owns."""
        update: DeliveryUpdate = {}
        attempts: list[AttemptRecord] = []
        path: list[str] = []
        current: DeliveryState = state
        while True:
            # A resumed step replays its earlier calls from their checkpoints.
            result = graph.invoke({"caller": current, "stage": name}, context=self.dispatch(name, current, owned))
            applied = self.apply(name, current, result)
            attempts += applied.pop("attempts", [])
            path += applied.pop("path", [])
            update.update(applied)
            owned = result.get("fingerprint", owned)
            routed: DeliveryUpdate = {}
            if not update.get("halt") and not update.get("human_question"):
                routed, owned = route({**current, **update}, result)
                update.update(routed)
            if halt := update.get("halt"):
                # The stage ends with the named halt.
                return {**update, "stop": halt, "attempts": attempts, "path": path}, owned
            question = update.get("human_question")
            if routed.get("human_question"):
                attempts[-1:] = [{**attempt, "detail": f"routing_defect: {question}"} for attempt in attempts[-1:]]
            if not question:
                return {**update, "attempts": attempts, "path": path}, owned
            current = {**current, **update}
            plan = current["plan"]
            revision = spec_digest([name, *scope(current), question])
            answer = interrupt(HumanPrompt(stage=name, question=question, revision=revision, attempts=attempts, path=path,
                                           models_used=update.get("models_used", {})).model_dump())
            if answer == "cancel":
                return {**update, "human_question": "", "attempts": attempts, **cancel(current, self, name), "path": path}, owned
            asked: HumanAnswer = {"stage": name, "question": question, "answer": answer, "revision": revision}
            if name == "spec_check" and plan.spec_candidate is not None:
                asked["candidate"] = plan.spec_candidate["revision"]
            update.update(human_question="", human_answers=[*update.get("human_answers", state.get("human_answers", [])), answer],
                          plan=plan.model_copy(update={"human_qa": [*plan.human_qa, asked]}))
            current = {**current, **update, "attempts": [*state.get("attempts", []), *attempts]}
            update.update(answered(current, answer))
            current = {**current, **update}


def human_stop(state: DeliveryState, run: StageRun, step: str, failure: Halt = Halt.git_failed, *,
               merging: bool = False) -> DeliveryUpdate:
    """Each stage's first step reads the issue for the human's stop before any paid call or write (rules.gates.continuation).

    A stop is a comment under the human's identity whose first line is exactly foreman stop. It ends the run as cancel does.
    It counts only when GitHub created it after the issue's latest Ready label event, so re-adding Ready starts clean. The
    event comes from GitHub, since release admits a dependent without a new one, and an edit to an older comment is no stop."""
    target = state["run"]
    assert target.issue is not None
    issue = target.issue

    def requested() -> bool:
        stops = [comment for comment in human_comment_records(run.forge, run.human_login, target.repo, issue)
                 if (comment.body or "").strip().partition("\n")[0].strip() == STOP]
        if not stops:
            return False
        ready = run.forge.ready_labeled_at(target.repo, issue)
        return any(ready is None or (comment.created_at is not None and comment.created_at > ready) for comment in stops)

    return cancel(state, run, step, failure, requested, merging=merging)


def cancel(state: DeliveryState, run: StageRun, step: str, failure: Halt = Halt.git_failed,
           requested: Callable[[], bool] = lambda: True, *, merging: bool = False) -> DeliveryUpdate:
    """End the run cancelled when requested, closing the run's open PR with the cancellation. The close leaves the branch,
    and a PR that is no longer open is left alone. Returns {} when nothing was requested.

    A stop cannot undo a merge, so the run's PR is read first (DQ1). Merge alone reports a PR GitHub merged before the close
    landed: the update records the stop as too late and carries no stop, so merge goes on to observe the merge. Merge's
    report adds its own path entry."""
    repo, branch = state["run"].repo, state.get("work", WorkState()).branch
    try:
        pr = run.forge.pull_request_for(repo, branch) if branch else None
        if not requested():
            return {}
        if pr is not None and pr.state == "OPEN":
            run.forge.close_pull_request(repo, pr.number, CANCELLATION)
            pr = run.forge.pull_request_for(repo, branch)
    except ForgeError as exc:
        return stopped(failure, f"GitHub could not settle the cancellation ({exc}).", step)
    if not merging:
        return {"stop": "cancelled", "path": [step]}
    return {"halt_detail": TOO_LATE} if pr is not None and pr.state == "MERGED" else {"stop": "cancelled"}


def follow(state: DeliveryState, before: str, after: str) -> DeliveryUpdate:
    """Foreman moved the checkout without changing the builder's tree, so its saved conversation moves too (D5)."""
    saved = state.get("builder_session")
    return {"builder_session": {**saved, "fingerprint": after}} if saved and saved["fingerprint"] == before else {}
