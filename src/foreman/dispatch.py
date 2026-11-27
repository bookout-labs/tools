"""Durably reserve and reconcile paid worker invocations."""

from collections.abc import Callable
from dataclasses import dataclass, replace
from decimal import Decimal
from functools import cached_property
import operator
from typing import Annotated, NotRequired, Protocol
from uuid import uuid4

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from pydantic_core import to_jsonable_python
from typing_extensions import TypedDict

from foreman._raw import is_dict, is_list
from foreman.adapters import Outcome
from foreman.budget import Remaining, budget_stop, cut_off, dollar_admission, dollar_detail, work_tokens
from foreman.config import ModelDependencyMissing, ModelSpec
from foreman.contracts import InvalidOutput as InvalidOutput
from foreman.github import ForgeError
from foreman.outcomes import Halt
from foreman.redaction import redact
from foreman.stage_output import Attempt, AttemptRecord, AttemptUsage, DispatchPlanRecord, Reservation, spec_digest
from foreman.state import DeliveryState, StartSettings
from foreman.stages import STAGES
from foreman.usd import BudgetUsd, usd_text
from foreman.worker import AttemptJournal, WorkerResult
from foreman.workspace import WorkState, Workspace, WorkspaceError, WorktreeState


class Handler(Protocol):
    def __call__(self, node: str, model: ModelSpec | None, state: DeliveryState, instruction: str, /) -> Outcome: ...


class DispatchInput(TypedDict):
    caller: DeliveryState
    stage: str


class Repair(TypedDict):
    """The one repair turn (item 1). The session is empty once the repair call stopped on quota, so no call resumes it."""
    model: str
    session: str
    error: str


class DispatchResult(TypedDict, total=False):
    attempts: Annotated[list[AttemptRecord], operator.add]
    output: dict[str, object]
    halt: Halt
    detail: str
    plan: DispatchPlanRecord
    fingerprint: str
    session: str  # The native session of an accepted builder answer, which the builder keeps for the PR (D5).
    sessions: dict[str, str]  # The builder conversations its quota-stopped calls left on the current tree, by model, which a halt keeps (D5).


class DispatchState(DispatchInput, DispatchResult):
    index: NotRequired[int]
    reservation: NotRequired[Reservation | None]
    repair: NotRequired[Repair | None]  # None once a cut-off repair turn gave the call back its own (reconcile).


@dataclass(frozen=True)
class DispatchRun:
    handler: Handler
    workspace: Workspace
    journal: AttemptJournal | None
    commit: str
    edits: bool
    prepare: Callable[[], DispatchPlanRecord]
    validate: Callable[[DispatchPlanRecord, str, dict[str, object]], None]  # The stage's schema and own checks of an answer.
    start: StartSettings  # The login and caps of the run's start, for every call, in-flight or new.
    owned: str = ""

    @cached_property
    def plan(self) -> DispatchPlanRecord:
        return self.prepare()

    def fingerprint(self, tree: WorktreeState | None = None) -> str:
        if not self.edits:
            return ""
        tree = tree or self.workspace.inspect()
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

    @staticmethod
    def repairing(state: DispatchState, model: ModelSpec) -> Repair | None:
        """The repair turn this model's call is, if any."""
        return repair if (repair := state.get("repair")) and repair["model"] == model.name and repair["session"] else None

    def resume(self, state: DispatchState, reservation: Reservation, model: ModelSpec) -> DispatchPlanRecord:
        """The session a call resumes: its repair turn's, or the builder's conversation when the checkout matches the one
        it saved (D5), unless this dispatch charged a cut-off call, which drops it. Every other call starts fresh."""
        if repair := self.repairing(state, model):
            return {**self.plan, "resume_session": repair["session"], "repair": repair["error"]}
        if (self.edits and "repair" not in state and all(attempt["outcome"] != "interrupted" for attempt in state.get("attempts", []))
                and (saved := state["caller"].get("builder_session")) and (saved["model"], saved["fingerprint"]) == (model.name, reservation["fingerprint"])):
            return {**self.plan, "resume_session": saved["session"]}
        return {**self.plan}

    def launch(self, state: DispatchState, reservation: Reservation, model: ModelSpec) -> WorkerResult | dict[str, object]:
        """Run the reserved call once, or return the halt that prevents it."""
        stage, plan = state["stage"], state.get("plan")
        assert plan is not None
        try:
            if self.moved(plan):
                return {"halt": Halt.git_failed, "detail": "The pull request or its diff changed during dispatch."}
            worker_plan: DispatchPlanRecord = {**self.resume(state, reservation, model), "budget_usd": reservation["dollar_allowance"]}
            worker_state, _ = STAGES[stage].project({**state["caller"], "dispatch_plan": worker_plan}, self.workspace)
            if reservation["fingerprint"] != self.fingerprint():
                return {"halt": Halt.dirty_worktree, "detail": f"The checkout {self.workspace.root} differs from the tree saved on the reservation of a call never sent. Restore it to that tree, then add Ready."}
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
        if model.family == "claude" and (usage is None or usage.total_cost_usd is None):
            record["dollar_charge"] = reservation["dollar_allowance"]
        if outcome is None:
            caps = "FOREMAN_MAX_TOKENS and FOREMAN_MAX_BUDGET_USD are" if "dollar_charge" in record else "FOREMAN_MAX_TOKENS is"
            record["detail"] = (f"usage_unknown: {state['stage']} {model.name} was interrupted, so its spend is unknown and it was charged "
                                f"in full, the allowance saved on its reservation. Ready makes the call again once {caps} raised.")
        elif "work_charge" in record or "dollar_charge" in record:
            record["detail"] = f"usage_unknown: {state['stage']} {model.name} did not report usage; charged the remaining allowance."
        return record


def reserve(state: DispatchState, runtime: Runtime[DispatchRun]) -> dict[str, object]:
    run, caller, stage = runtime.context, state["caller"], state["stage"]
    attempts, limits, caps = caller.get("attempts", []) + state.get("attempts", []), caller["run"], run.start
    if detail := budget_stop(caps.max_tokens, caps.max_budget_usd, attempts):
        return {"halt": Halt.budget_exceeded, "detail": detail}
    index = state.get("index", 0)
    try:
        plan = state.get("plan") or run.plan
        if index >= len(plan["models"]):
            return {"reservation": None} if plan["models"] else {"halt": Halt.model_dependency_missing, "detail": f"No model is configured for {stage}. Fix models.toml."}
        fingerprint = run.fingerprint()
        # A checkout this dispatch or the attempt already owns continues as it is, worker commits included.
        if run.edits and fingerprint not in (saved := {state.get("fingerprint"), run.owned} - {None, ""}):
            worktree = run.workspace.inspect()
            if detail := caller.get("work", WorkState()).misplaced(run.workspace.root, worktree):
                return {"halt": Halt.dirty_worktree, "detail": detail}
            if not worktree.clean and not cut_off(attempts, stage):
                detail = "The worktree edits differ from the saved attempt." if saved else "Unowned edits exist before this attempt."
                return {"halt": Halt.dirty_worktree, "detail": detail}
    except ModelDependencyMissing as exc:
        return {"halt": Halt.model_dependency_missing, "detail": str(exc)}
    except InvalidOutput as exc:
        return {"halt": Halt.invalid_output, "detail": str(exc)}
    except (ForgeError, WorkspaceError) as exc:
        return {"halt": Halt.git_failed, "detail": str(exc)}
    assert (work := work_tokens(attempts)) is not None
    dollars = dollar_admission(BudgetUsd(Decimal(caps.max_budget_usd)), attempts)
    assert isinstance(dollars, Remaining)  # budget_stop stopped every other admission.
    budget = usd_text(BudgetUsd(dollars.usd))
    model = plan["models"][index]
    identity = spec_digest([uuid4().hex, limits.repo.lower(), limits.issue, stage, limits.base_sha,
                            caller["plan"].spec_candidate, plan["target"], str(run.workspace.root.resolve()),
                            model, caps.max_tokens - work, budget, fingerprint])
    if run.journal is not None:
        run.journal.reserve(identity)
    return {"reservation": Reservation(id=identity, model=model, work_allowance=caps.max_tokens - work,
                                       dollar_allowance=budget, fingerprint=fingerprint),
            "index": index, "plan": plan}


def reconcile(state: DispatchState, runtime: Runtime[DispatchRun]) -> dict[str, object]:
    stage, run, caps = state["stage"], runtime.context, runtime.context.start
    if (reservation := state.get("reservation")) is None:
        # Every qualified model answered quota or has no CLI. Ready makes the call again; Foreman never waits for capacity.
        tried = "; ".join(a.get("detail") or "" for a in state.get("attempts", []) if a["outcome"] in {"quota", "cli_missing"})
        return {"halt": Halt.worker_unavailable, "detail": f"No qualified worker available for {stage}. {tried}".strip()}
    model, plan = ModelSpec.parse(reservation["model"]), state.get("plan")
    assert plan is not None
    evidence = run.journal.read(reservation["id"]) if run.journal is not None else True
    if evidence is True:
        evidence = run.launch(state, reservation, model)
        if not isinstance(evidence, WorkerResult):
            return {**evidence, "reservation": None}
    outcome = evidence.outcome if isinstance(evidence, WorkerResult) else None
    record = run.charge(state, reservation, model, outcome)
    # A returned call keeps the fingerprint of what the worker left, so even a halt hands the caller its edits.
    returned = {"fingerprint": evidence.fingerprint} if isinstance(evidence, WorkerResult) and evidence.fingerprint is not None else {}
    # Held sessions saw the reserved tree. A call that changed it ends them all, whatever it answered (D5).
    moved = returned.get("fingerprint", reservation["fingerprint"]) != reservation["fingerprint"]
    held = {} if moved else state.get("sessions", {})
    update: dict[str, object] = {"attempts": [record], "reservation": None, **returned, **({"sessions": held} if moved else {})}
    if outcome and outcome.kind == "failed":  # Invalid output stays stopped, charged like any attempt.
        return {**update, "halt": Halt.invalid_output, "detail": f"{stage} output from {model.name}: {outcome.detail}"}
    attempts = state["caller"].get("attempts", []) + state.get("attempts", []) + [record]
    if outcome and outcome.kind == "budget_exhausted":
        parts = (f"{stage} {model.name} exhausted its dollar budget.", dollar_detail(caps.max_budget_usd, attempts),
                 record.get("detail"), outcome.detail)
        return {**update, "halt": Halt.budget_exceeded, "detail": " ".join(part for part in parts if part)}
    if detail := budget_stop(caps.max_tokens, caps.max_budget_usd, attempts):
        return {**update, "halt": Halt.budget_exceeded, "detail": detail}
    if not isinstance(evidence, WorkerResult) or outcome is None:
        # Charged once and with room left, the cut-off call is made again here as a new call, never as its repair turn.
        return {**update, "sessions": {}, **({"repair": None} if run.repairing(state, model) else {})}
    try:
        if evidence.fingerprint is None:
            return {**update, "halt": Halt.git_failed, "detail": "Could not read the worker's resulting worktree."}
        if evidence.fingerprint != run.fingerprint():
            return {**update, "halt": Halt.dirty_worktree, "detail": "The worktree differs from the saved result."}
        if outcome.kind in {"quota", "cli_missing"}:
            sessions = dict(held)
            if repair := run.repairing(state, model):
                # A stopped repair call is never resumed: the next call starts fresh from the original instruction.
                return {**update, "index": state.get("index", 0) + 1, "sessions": sessions, "repair": {**repair, "session": ""}}
            if run.edits and outcome.session_id:
                sessions[model.name] = outcome.session_id
            return {**update, "index": state.get("index", 0) + 1, "sessions": sessions}
        run.validate(plan, model.name, outcome.output)
    except InvalidOutput as exc:
        if state.get("repair") is not None or not outcome.session_id:
            return {**update, "halt": Halt.invalid_output, "detail": f"{stage} output from {model.name}: {exc}"}
        sessions = {name: session for name, session in held.items() if name != model.name}
        return {**update, "sessions": sessions, "repair": Repair(model=model.name, session=outcome.session_id, error=str(exc))}
    except WorkspaceError as exc:
        return {**update, "halt": Halt.git_failed, "detail": str(exc)}
    return {**update, "output": outcome.output, **({"session": outcome.session_id or ""} if run.edits else {})}


def compile_dispatch() -> CompiledStateGraph[DispatchState, DispatchRun, DispatchInput, DispatchResult]:
    def after_reconcile(state: DispatchState) -> str:
        # Only the reconcile that grants the repair leaves a live repair session. Its call ends it.
        repair = "repair_turn" if (granted := state.get("repair")) and granted["session"] else "eligible_fallback"
        return "halted" if "halt" in state else "accepted" if "output" in state else repair

    return (StateGraph(DispatchState, input_schema=DispatchInput, output_schema=DispatchResult, context_schema=DispatchRun)
            .add_node("reserve", reserve)
            .add_node("reconcile", reconcile)
            .add_edge(START, "reserve")
            .add_conditional_edges("reserve", lambda state: "halted" if "halt" in state else "admitted", {"admitted": "reconcile", "halted": END})
            .add_conditional_edges("reconcile", after_reconcile, {"eligible_fallback": "reserve", "repair_turn": "reserve", "accepted": END, "halted": END})
            .compile().with_config({"metadata": {"contract_id": "dispatch", "stage_declarations": DECLARATIONS}}))


DECLARATIONS = {"halts": [Halt.budget_exceeded, Halt.worker_unavailable, Halt.invalid_output, Halt.git_failed, Halt.model_dependency_missing, Halt.dirty_worktree],
                "retries": {"provider_fallback": "eligible configured model order and remaining allowance", "quota_resume": "Ready added again once a qualified model is available, and remaining allowance",
                            "invalid_answer": "one repair turn in the same session with the exact validation error", "budget_resume": "Ready added again after the operator raises the budget cap"}}
# Calling steps hold this one compiled graph, so introspection finds it nested under each caller.
DISPATCH = compile_dispatch()
