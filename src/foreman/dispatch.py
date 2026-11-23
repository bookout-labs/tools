"""Durably reserve and reconcile paid worker invocations."""

from collections.abc import Callable
from dataclasses import dataclass, replace
from decimal import Decimal
from functools import cached_property
import operator
from typing import Annotated, Literal, NotRequired, Protocol
from uuid import uuid4

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from langgraph.types import interrupt
from pydantic_core import to_jsonable_python
from typing_extensions import TypedDict

from foreman._raw import is_dict, is_list
from foreman.adapters import Outcome
from foreman.budget import Remaining, budget_stop, dollar_admission, dollar_detail, work_tokens
from foreman.config import ModelDependencyMissing, ModelSpec
from foreman.contracts import Contract, InvalidOutput as InvalidOutput
from foreman.github import ForgeError
from foreman.outcomes import Halt
from foreman.redaction import redact
from foreman.stage_output import Attempt, AttemptRecord, AttemptUsage, DispatchPlanRecord, Reservation, spec_digest
from foreman.state import DeliveryState
from foreman.stages import STAGES
from foreman.usd import BudgetUsd, usd_text
from foreman.worker import AttemptJournal, WorkerResult
from foreman.workspace import WorkState, Workspace, WorkspaceError


class Handler(Protocol):
    def __call__(self, node: str, model: ModelSpec | None, state: DeliveryState, instruction: str, /) -> Outcome: ...


class QuotaPause(Contract):
    reason: Literal["quota_exhausted"] = "quota_exhausted"
    detail: str
    stage: str
    attempts: list[AttemptRecord]


class DispatchInput(TypedDict):
    caller: DeliveryState
    stage: str


class DispatchResult(TypedDict, total=False):
    attempts: Annotated[list[AttemptRecord], operator.add]
    output: dict[str, object]
    halt: Halt
    detail: str
    plan: DispatchPlanRecord


class DispatchState(DispatchInput, DispatchResult):
    index: NotRequired[int]
    reservation: NotRequired[Reservation | None]
    fingerprint: NotRequired[str]
    sessions: NotRequired[dict[str, str]]


@dataclass(frozen=True)
class DispatchRun:
    handler: Handler
    workspace: Workspace
    journal: AttemptJournal | None
    commit: str
    edits: bool
    prepare: Callable[[], DispatchPlanRecord]

    @cached_property
    def plan(self) -> DispatchPlanRecord:
        return self.prepare()

    def fingerprint(self) -> str:
        if not self.edits:
            return ""
        tree = self.workspace.inspect()
        return spec_digest([tree.branch, tree.head, self.workspace.fingerprint()])

    def moved(self, plan: DispatchPlanRecord) -> bool:
        """Compare this process's preparation with the saved plan as JSON, since checkpoints return tuples as lists."""
        return to_jsonable_python((self.plan["target"], self.plan["scope"])) != to_jsonable_python((plan["target"], plan["scope"]))

    @staticmethod
    def clean(outcome: Outcome) -> Outcome:
        def scrub(value: object) -> object:
            if isinstance(value, str):
                return redact(value, credentials_only=True)
            if is_dict(value):
                return {key: scrub(item) for key, item in value.items()}
            if is_list(value):
                return [scrub(item) for item in value]
            return value
        return replace(outcome, output={key: scrub(value) for key, value in outcome.output.items()},
                       detail=str(scrub(outcome.detail)))

    def launch(self, state: DispatchState, reservation: Reservation, model: ModelSpec) -> WorkerResult | dict[str, object]:
        """Run the reserved call once, or return the halt that prevents it."""
        stage, plan = state["stage"], state.get("plan")
        assert plan is not None
        try:
            if self.moved(plan):
                return {"halt": Halt.git_failed, "detail": "The pull request or its diff changed during dispatch."}
            worker_plan: DispatchPlanRecord = {**self.plan, "budget_usd": reservation["dollar_allowance"]}
            if self.edits and reservation["fingerprint"] == state.get("fingerprint") and (session := state.get("sessions", {}).get(model.name)):
                worker_plan["resume_session"] = session
            worker_state, _ = STAGES[stage].project({**state["caller"], "dispatch_plan": worker_plan}, self.workspace)
            if reservation["fingerprint"] != self.fingerprint():
                return {"halt": Halt.dirty_worktree, "detail": "The worktree changed after reservation."}
        except InvalidOutput as exc:
            return {"halt": Halt.invalid_output, "detail": str(exc)}
        except (ForgeError, WorkspaceError) as exc:
            return {"halt": Halt.git_failed, "detail": str(exc)}
        if self.journal is not None:
            self.journal.send(reservation["id"])
        outcome = self.clean(self.handler(stage, model, worker_state, worker_plan["instruction"]))
        try:
            fingerprint: str | None = self.fingerprint()
        except WorkspaceError:
            fingerprint = None
        result = WorkerResult(outcome, fingerprint)
        if self.journal is not None:
            self.journal.finish(reservation["id"], result)
        return result

    def charge(self, state: DispatchState, reservation: Reservation, model: ModelSpec, outcome: Outcome | None) -> AttemptRecord:
        plan, usage = state.get("plan"), outcome.usage if outcome else None
        assert plan is not None
        record = Attempt(stage=state["stage"], model=model.name, commit=self.commit, methods=plan["methods"],
            outcome=outcome.kind if outcome else "interrupted",
            prompt_bytes=outcome.prompt_bytes if outcome else None, native_usage=outcome.native_usage if outcome else None,
            usage=AttemptUsage(input=usage.input_tokens, output=usage.output_tokens,
                               cache_read=usage.cache_read_input_tokens, cache_creation=usage.cache_creation_input_tokens,
                               cost_usd=usage.total_cost_usd) if usage is not None else None).record()
        record["attempt_id"] = reservation["id"]
        if outcome and outcome.kind in {"quota", "cli_missing"}:
            record["detail"] = f"{model.name}: {outcome.kind}. {outcome.detail}".strip()
        if record["outcome"] == "cli_missing":
            return record
        if work_tokens([record]) is None:
            record["work_charge"] = max(usage.input_tokens + usage.output_tokens if usage else 0, reservation["work_allowance"])
        if model.family == "claude" and (dollars := reservation["dollar_allowance"]) and (usage is None or usage.total_cost_usd is None):
            record["dollar_charge"] = dollars
        if "work_charge" in record or "dollar_charge" in record:
            record["detail"] = f"usage_unknown: {state['stage']} {model.name} did not report usage; charged the remaining allowance."
        return record


def reserve(state: DispatchState, runtime: Runtime[DispatchRun]) -> dict[str, object]:
    run, caller, stage = runtime.context, state["caller"], state["stage"]
    attempts, limits = caller.get("attempts", []) + state.get("attempts", []), caller["run"]
    assert (cap := limits.token_cap) is not None
    if detail := budget_stop(cap, limits.max_budget_usd, attempts):
        return {"halt": Halt.budget_exceeded, "detail": detail}
    index = state.get("index", 0)
    try:
        plan = state.get("plan") or run.plan
        if index >= len(plan["models"]):
            return {"reservation": None} if plan["models"] else {"halt": Halt.model_dependency_missing, "detail": f"No model is configured for {stage}. Fix models.toml."}
        fingerprint = run.fingerprint()
        if run.edits:
            worktree, expected = run.workspace.inspect(), caller.get("work", WorkState())
            if (worktree.branch, worktree.head) != (expected.branch, expected.attempt_head):
                return {"halt": Halt.dirty_worktree, "detail": "Restore the saved branch and parent before continuing edits."}
            if not worktree.clean and fingerprint != (saved := state.get("fingerprint")):
                detail = "The worktree edits differ from the saved attempt." if saved else "Unowned edits exist before this attempt."
                return {"halt": Halt.dirty_worktree, "detail": detail}
    except ModelDependencyMissing as exc:
        return {"halt": Halt.model_dependency_missing, "detail": str(exc)}
    except InvalidOutput as exc:
        return {"halt": Halt.invalid_output, "detail": str(exc)}
    except (ForgeError, WorkspaceError) as exc:
        return {"halt": Halt.git_failed, "detail": str(exc)}
    assert (work := work_tokens(attempts)) is not None
    dollars = dollar_admission(BudgetUsd(Decimal(limits.max_budget_usd)), attempts) if limits.max_budget_usd else None
    budget = usd_text(BudgetUsd(dollars.usd)) if isinstance(dollars, Remaining) else None
    model = plan["models"][index]
    identity = spec_digest([uuid4().hex, limits.repo.lower(), limits.issue, stage, limits.base_sha,
                            caller["plan"].spec_candidate, plan["target"], str(run.workspace.root.resolve()),
                            model, cap - work, budget, fingerprint])
    if run.journal is not None:
        run.journal.reserve(identity)
    return {"reservation": Reservation(id=identity, model=model, work_allowance=cap - work,
                                       dollar_allowance=budget, fingerprint=fingerprint),
            "index": index, "plan": plan}


def reconcile(state: DispatchState, runtime: Runtime[DispatchRun]) -> dict[str, object]:
    stage, run, limits = state["stage"], runtime.context, state["caller"]["run"]
    if (reservation := state.get("reservation")) is None:
        tried = "; ".join(a.get("detail") or "" for a in state.get("attempts", []) if a["outcome"] in {"quota", "cli_missing"})
        interrupt(QuotaPause(stage=stage, attempts=state.get("attempts", []), detail=f"No qualified worker available for {stage}. {tried}".strip()).model_dump())
        return {"index": 0}
    model = ModelSpec.parse(reservation["model"])
    evidence = run.journal.read(reservation["id"]) if run.journal is not None else True
    if evidence is True:
        evidence = run.launch(state, reservation, model)
        if not isinstance(evidence, WorkerResult):
            return {**evidence, "reservation": None}
    outcome = evidence.outcome if isinstance(evidence, WorkerResult) else None
    record = run.charge(state, reservation, model, outcome)
    update: dict[str, object] = {"attempts": [record], "reservation": None}
    if outcome and outcome.kind == "failed":  # Invalid output stays stopped, charged like any attempt.
        return {**update, "halt": Halt.invalid_output, "detail": f"{stage} output from {model.name}: {outcome.detail}"}
    attempts = state["caller"].get("attempts", []) + state.get("attempts", []) + [record]
    if outcome and outcome.kind == "budget_exhausted":
        parts = (f"{stage} {model.name} exhausted its dollar budget.", dollar_detail(limits.max_budget_usd, attempts),
                 record.get("detail"), outcome.detail)
        return {**update, "halt": Halt.budget_exceeded, "detail": " ".join(part for part in parts if part)}
    assert limits.token_cap is not None
    if (detail := budget_stop(limits.token_cap, limits.max_budget_usd, attempts)) or outcome is None:
        return {**update, "halt": Halt.budget_exceeded, "detail": detail or record.get("detail") or ""}
    assert isinstance(evidence, WorkerResult)
    try:
        if evidence.fingerprint is None:
            return {**update, "halt": Halt.git_failed, "detail": "Could not read the worker's resulting worktree."}
        if evidence.fingerprint != run.fingerprint():
            return {**update, "halt": Halt.dirty_worktree, "detail": "The worktree differs from the saved result."}
        if outcome.kind in {"quota", "cli_missing"}:
            sessions = dict(state.get("sessions", {})) if evidence.fingerprint == reservation["fingerprint"] else {}
            if run.edits and outcome.session_id:
                sessions[model.name] = outcome.session_id
            return {**update, "index": state.get("index", 0) + 1, "fingerprint": evidence.fingerprint, "sessions": sessions}
    except WorkspaceError as exc:
        return {**update, "halt": Halt.git_failed, "detail": str(exc)}
    return {**update, "output": outcome.output}


def compile_dispatch() -> CompiledStateGraph[DispatchState, DispatchRun, DispatchInput, DispatchResult]:
    def after_reserve(state: DispatchState) -> str:
        return "halted" if "halt" in state else "admitted"

    def after_reconcile(state: DispatchState) -> str:
        return "halted" if "halt" in state else "accepted" if "output" in state else "eligible_fallback"

    return (StateGraph(DispatchState, input_schema=DispatchInput, output_schema=DispatchResult, context_schema=DispatchRun)
            .add_node("reserve", reserve)
            .add_node("reconcile", reconcile)
            .add_edge(START, "reserve")
            .add_conditional_edges("reserve", after_reserve, {"admitted": "reconcile", "halted": END})
            .add_conditional_edges("reconcile", after_reconcile, {"eligible_fallback": "reserve", "accepted": END, "halted": END})
            .compile())
