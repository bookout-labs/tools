"""The adapters and worker conversation that every stage's steps share."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from functools import wraps

from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt

from foreman.doctor import GreenChecks
from foreman.dispatch import DispatchInput, DispatchResult, DispatchRun, DispatchState
from foreman.report import Rewrite, no_rewrite
from foreman.github import BatchConfirmation, Forge, ForgeError, IssueComment, PublishedPullRequest, human_comment_records
from foreman.outcomes import Halt, stopped
from foreman.readiness import Escape, MergeState
from foreman.stage_output import AttemptRecord, DispatchStage, GateReply, GateStage, HumanAnswer, HumanPrompt, PlanState, ReviewState, spec_digest
from foreman.state import ChecksState, DeliveryFields, DeliveryState, DeliveryUpdate, RunState, StartSettings
from foreman.workspace import CheckLog, WorkState, Workspace

DispatchGraph = CompiledStateGraph[DispatchState, DispatchRun, DispatchInput, DispatchResult]
STOP, ESCAPE = "foreman stop", "foreman escape"
CANCELLATION = "Foreman cancelled this run, so it closed this pull request. The branch stays in place."
TOO_LATE = "The human's stop came after GitHub merged the pull request, too late to cancel the run."
# A wake restarts merge at arm and passes back to the step its saved wait is in, which reads GitHub again.
RESUMES = {"merge.arm": ("merge.arm", "merge.observe"), "merge.observe": ("merge.observe",)}


# The merge commit of the saved Foreman run that recorded the repository's issue as its revert, or None (triage.admit, TR8).
Reverts = Callable[[str, int], str | None]


def no_reverts(repo: str, issue: int) -> str | None:
    """A run outside the host's run store sees no recorded revert."""
    return None


def no_batch_confirmation(repo: str, parent: int, child: int | None) -> bool | None:
    return None


def resumed(step: str, steps: Sequence[str]) -> bool:
    """Whether a restart passes back through the step to the saved step it resumes, which the run's steps end with."""
    return bool(steps) and steps[-1] in RESUMES.get(step, ())


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
    dispatch: Callable[[DispatchStage, DeliveryState, str], DispatchRun]
    apply: Callable[[DispatchStage, DeliveryState, DispatchResult], DeliveryUpdate]
    rewrite: Rewrite = no_rewrite
    start: StartSettings | None = None  # The run context's login and caps (LiveRun.start).
    greens: GreenChecks | None = None  # The host's green full checks (LiveRun.greens).
    reverts: Reverts = no_reverts  # The reverts the host's saved runs recorded (LiveRun.reverts).
    batch_confirmation: BatchConfirmation = no_batch_confirmation

    def bind(self, step: Callable[[DeliveryState, "StageRun"], DeliveryUpdate]) -> Callable[[DeliveryState], DeliveryUpdate]:
        """The step as a graph node. The node keeps the step's identity, so introspection names the step as its owner.

        The node records the drawn step it starts and rewrites the run report then. A step that reads the human's stop
        rewrites it after that read instead, so nothing is written before the read (rules.reporting). A restart that passes
        back through the node to a saved step it resumes starts no step: the saved step is still the step under way, so the
        node records nothing and writes nothing (ME14)."""
        name = f"{step.__module__.rpartition('.')[2]}.{step.__name__}"
        reads_stop = human_stop.__name__ in step.__code__.co_names

        @wraps(step)
        def node(state: DeliveryState) -> DeliveryUpdate:
            if resumed(name, state.get("steps", [])):
                return step(state, replace(self, rewrite=no_rewrite))
            state = {**state, "steps": [*state.get("steps", []), name]}
            if not reads_stop:
                self.rewrite(state, None)
            return {**step(state, self), "steps": state["steps"]}
        return node

    def call(self, graph: DispatchGraph, name: DispatchStage, state: DeliveryState, owned: str = "") -> DispatchResult:
        """One dispatch for the step. Its calls' records are saved when it returns, so the run report is rewritten then."""
        result = graph.invoke({"caller": state, "stage": name}, context=self.dispatch(name, state, owned))
        self.rewrite({**state, "attempts": [*state.get("attempts", []), *result.get("attempts", [])]}, None)
        return result

    def consult(
        self, graph: DispatchGraph, name: GateStage, state: DeliveryState, scope: Callable[[DeliveryState], list[object]],
        *, owned: str = "", answered: Callable[[DeliveryState, str], DeliveryUpdate] = lambda state, answer: {},
    ) -> tuple[DeliveryUpdate, str]:
        """Dispatch a worker until it returns no question. Each question waits at the stage's gate for the human.

        The scope reads the Delivery state the worker's answer left and binds the question to its subject, so the question
        posts its reply marker and only the human's reply to that question resumes the step (rules.gates.checkpoint).
        Returns the applied update and the fingerprint of the edits the worker owns."""
        update: DeliveryUpdate = {}
        attempts: list[AttemptRecord] = []
        path: list[str] = []
        current: DeliveryState = state
        while True:
            # A resumed step replays its earlier calls from their checkpoints.
            result = self.call(graph, name, current, owned)
            applied = self.apply(name, current, result)
            attempts += applied.pop("attempts", [])
            path += applied.pop("path", [])
            update.update(applied)
            owned = result.get("fingerprint", owned)
            if halt := update.get("halt"):
                # The stage ends with the named halt.
                return {**update, "stop": halt, "attempts": attempts, "path": path}, owned
            question = update.get("human_question")
            if not question:
                return {**update, "attempts": attempts, "path": path}, owned
            current = {**current, **update}
            plan = current["plan"]
            revision = spec_digest([name, *scope(current), question])
            reply: GateReply = interrupt(HumanPrompt(stage=name, question=question, revision=revision, attempts=attempts, path=path,
                                                     models_used=update.get("models_used", {})).model_dump())
            answer = reply["answer"]
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


def is_stop(comment: str) -> bool:
    return comment.strip().partition("\n")[0].strip() == STOP


def is_escape(comment: str) -> bool:
    """A hand fix the human reports after the merge, read by the first-line rule a stop is read by (scorers.escape_input)."""
    return comment.strip().partition("\n")[0].strip() == ESCAPE


def human_stop(state: DeliveryState, run: StageRun, step: str, failure: Halt = Halt.git_failed, *,
               merging: bool = False) -> DeliveryUpdate:
    """Each stage's first step reads the issue for the human's stop before any paid call or write (rules.gates.continuation).

    A stop is a comment under the human's identity whose first line is exactly foreman stop. It ends the run as cancel does.
    It counts only when GitHub created it after the issue's latest Ready label event, so re-adding Ready starts clean. The
    event comes from GitHub, since release admits a dependent without a new one, and an edit to an older comment is no stop."""
    target, start = state["run"], run.start
    assert target.issue is not None and start is not None
    issue = target.issue

    def requested() -> bool:
        return stop_counts(run.forge, human_comment_records(run.forge, start.human_login, target.repo, issue), target.repo, issue)

    if not (update := cancel(state, run, step, failure, requested, merging=merging)):
        run.rewrite(state, None)
    return update


def stop_counts(forge: Forge, comments: list[IssueComment], repo: str, issue: int) -> bool:
    """Whether the human's comments hold a stop created after the issue's latest Ready label event. Raises ForgeError."""
    stops = [comment for comment in comments if is_stop(comment.body or "")]
    if not stops:
        return False
    ready = forge.ready_labeled_at(repo, issue)
    return any(ready is None or (comment.created_at is not None and comment.created_at > ready) for comment in stops)


def cancel(state: DeliveryState, run: StageRun, step: str, failure: Halt = Halt.git_failed,
           requested: Callable[[], bool] = lambda: True, *, merging: bool = False) -> DeliveryUpdate:
    """End the run cancelled when requested, as close does. Returns {} when nothing was requested.

    A stop cannot undo a merge, so in merge the run's PR is read before the stop (DQ1)."""
    try:
        pr = run_pull_request(state, run.forge) if merging else None
        if not requested():
            return {}
        return close(state, run.forge, step, pr if merging else run_pull_request(state, run.forge), merging=merging)
    except ForgeError as exc:
        return stopped(failure, f"GitHub could not settle the cancellation ({exc}).", step)


def run_pull_request(state: DeliveryState, forge: Forge) -> PublishedPullRequest | None:
    """The run's PR as GitHub reports it, or None before the run has a branch. Raises ForgeError."""
    branch = state.get("work", WorkState()).branch
    return forge.pull_request_for(state["run"].repo, branch) if branch else None


def close(state: DeliveryState, forge: Forge, step: str, pr: PublishedPullRequest | None, *, merging: bool) -> DeliveryUpdate:
    """End the run cancelled, closing its open PR, as last read, with the cancellation. The close leaves the branch, and a
    PR that is no longer open is left alone. Raises ForgeError.

    Merge alone reports a PR GitHub merged before the close landed, or that merge read merged: the update records the stop as
    too late and carries no stop, so merge goes on to observe the merge. Merge's report adds its own path entry."""
    repo, branch = state["run"].repo, state.get("work", WorkState()).branch
    if pr is not None and pr.state == "OPEN":
        forge.close_pull_request(repo, pr.number, CANCELLATION)
        pr = forge.pull_request_for(repo, branch)
    if not merging:
        return {"stop": "cancelled", "path": [step]}
    return {"halt_detail": TOO_LATE} if pr is not None and pr.state == "MERGED" else {"stop": "cancelled"}


def hand_fixes(merge: MergeState | None, forge: Forge, repo: str, head: str, moved: str) -> MergeState:
    """The run's merge record with each commit the PR's branch gained after the head Foreman published that the App did not
    make, recorded once as a hand fix when Foreman reads the moved PR (scorers.escape_input). Raises ForgeError."""
    merge = merge or MergeState()
    known = {escape.key for escape in merge.escapes}
    return merge.model_copy(update={"escapes": [*merge.escapes, *(
        Escape(kind="hand_fix", key=sha, evidence=f"Commit {sha} by {author} on the pull request's branch.")
        for sha, author in forge.foreign_commits(repo, head, moved) if sha not in known)]})


def follow(state: DeliveryState, before: str, after: str) -> DeliveryUpdate:
    """Foreman moved the checkout without changing the builder's tree, so its saved conversation moves too (D5)."""
    saved = state.get("builder_session")
    return {"builder_session": {**saved, "fingerprint": after}} if saved and saved["fingerprint"] == before else {}
