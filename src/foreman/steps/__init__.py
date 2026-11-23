"""The adapters and worker conversation that every stage's steps share."""

from collections.abc import Callable
from dataclasses import dataclass
from functools import wraps

from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt

from foreman.dispatch import DispatchInput, DispatchResult, DispatchRun, DispatchState
from foreman.github import Forge
from foreman.stage_output import AttemptRecord, GateStage, HumanAnswer, HumanPrompt, PlanState, ReviewState, spec_digest
from foreman.state import ChecksState, DeliveryFields, DeliveryState, DeliveryUpdate, RunState
from foreman.workspace import CheckLog, Workspace

DispatchGraph = CompiledStateGraph[DispatchState, DispatchRun, DispatchInput, DispatchResult]


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
    dispatch: Callable[[GateStage, DeliveryState, str], DispatchRun]
    apply: Callable[[GateStage, DeliveryState, DispatchResult], DeliveryUpdate]

    def bind(self, step: Callable[[DeliveryState, "StageRun"], DeliveryUpdate]) -> Callable[[DeliveryState], DeliveryUpdate]:
        """The step as a graph node. The node keeps the step's identity, so introspection names the step as its owner."""
        @wraps(step)
        def node(state: DeliveryState) -> DeliveryUpdate:
            return step(state, self)
        return node

    def consult(
        self, graph: DispatchGraph, name: GateStage, state: DeliveryState, scope: Callable[[PlanState], list[object]] | None,
        *, owned: str = "", answered: Callable[[DeliveryState, str], DeliveryUpdate] = lambda state, answer: {},
    ) -> tuple[DeliveryUpdate, str]:
        """Dispatch a worker until it returns no question. Each question waits at the stage's gate for the human.

        The scope binds the question to its subject, so only an answer to that question resumes the step.
        Returns the applied update and the fingerprint of the edits the worker owns."""
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
            question = update.get("human_question")
            if halt := update.get("halt"):
                # The stage ends with the named halt.
                return {**update, "stop": halt, "attempts": attempts, "path": path}, owned
            if not question:
                return {**update, "attempts": attempts, "path": path}, owned
            plan = update.get("plan", state["plan"])
            revision = spec_digest([name, *scope(plan), question]) if scope else None
            answer = interrupt(HumanPrompt(stage=name, question=question, revision=revision, attempts=attempts, path=path,
                                           models_used=update.get("models_used", {})).model_dump())
            if answer == "cancel":
                return {**update, "stop": "cancelled", "human_question": "", "attempts": attempts, "path": path}, owned
            asked: HumanAnswer = {"stage": name, "question": question, "answer": answer}
            if revision:
                asked["revision"] = revision
            if name == "spec_check" and plan.spec_candidate is not None:
                asked["candidate"] = plan.spec_candidate["revision"]
            update.update(human_question="", human_answers=[*update.get("human_answers", state.get("human_answers", [])), answer],
                          plan=plan.model_copy(update={"human_qa": [*plan.human_qa, asked]}))
            current = {**current, **update, "attempts": [*state.get("attempts", []), *attempts]}
            update.update(answered(current, answer))
            current = {**current, **update}
