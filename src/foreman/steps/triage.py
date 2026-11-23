"""Triage: admit one issue against its pinned repository and route it to research or planning."""

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from foreman.contracts import InvalidOutput
from foreman.dispatch import DISPATCH
from foreman.github import ForgeError, unmet_prerequisites
from foreman.outcomes import Halt, stopped
from foreman.stage_output import load_stored_slice
from foreman.state import DeliveryState, DeliveryUpdate
from foreman.steps import StageInput, StageRun
from foreman.workspace import WorkspaceError

HALTS = [Halt.missing_slice]
ROUTES = ("question", "plan", "supplied_slice", "cancelled", "halted")


def admit(state: DeliveryState, run: StageRun) -> DeliveryUpdate:
    target = state["run"]
    if target.base_sha:
        # A resumed stage keeps the identity and base it admitted.
        return {}
    repo, number = target.repo, target.issue
    assert number is not None
    forge, workspace = run.forge, run.workspace
    try:
        issue = forge.load_issue(repo, number)
        if (issue["repo"].lower(), issue["number"]) != (repo.lower(), number):
            return stopped(Halt.invalid_output, "foreign_repository: issue identity differs from the requested assignment.", "admit")
        plan = state["plan"].model_copy(update={"issue_kind": "slice" if "slice" in issue["labels"] else "parent"})
        item = None
        if plan.issue_kind == "slice":
            try:
                item = load_stored_slice(issue["body"])
            except InvalidOutput as exc:
                return stopped(Halt.missing_slice, str(exc), "admit")
            plan = plan.model_copy(update={"slice": item.record(), "work_kind": item.work_kind})
        workspace.validate_origin(issue["repo"])
        if detail := unmet_prerequisites(forge, repo, forge.blockers(repo, number), item):
            # The issue stays unadmitted. A later Ready recheck admits it once its prerequisites merge.
            return {"stop": "blocked", "halt_detail": detail, "path": ["admit"]}
        base = forge.default_branch(repo)
        pinned = workspace.pin_base(base)
    except (ForgeError, WorkspaceError) as exc:
        return stopped(Halt.git_failed, str(exc), "admit")
    request = issue["title"] + ("\n\n" + issue["body"] if issue["body"] else "")
    if target.request:
        request += f"\n\nOperator context:\n{target.request}"
    return {"run": target.model_copy(update={"repo": issue["repo"], "request": request, "base_branch": base, "base_sha": pinned}),
            "plan": plan}


def route(state: DeliveryState, run: StageRun) -> DeliveryUpdate:
    if state["plan"].triage:
        return {}
    target = state["run"]
    update, _ = run.consult(DISPATCH, "triage", state, lambda plan: [target.repo.lower(), target.issue, target.base_sha])
    plan = update.get("plan", state["plan"])
    if not (update.get("halt") or update.get("stop")):
        # A rejected supplied slice is repaired by drafting. A valid one is validated even when the issue also asks.
        if plan.spec_rejection:
            update["plan"] = plan.model_copy(update={"triage": "plan"})
        elif plan.slice is not None:
            update["plan"] = plan.model_copy(update={"triage": "supplied_slice"})
    return update


def compile_stage(run: StageRun) -> CompiledStateGraph[DeliveryState, None, StageInput, DeliveryState]:
    def outcome(state: DeliveryState) -> str:
        if state.get("halt") or state.get("stop") == "blocked":
            return "halted"
        return "cancelled" if state.get("stop") == "cancelled" else state["plan"].triage

    declarations: dict[str, object] = {"halts": [halt.value for halt in HALTS], "retries": {}}
    return (StateGraph[DeliveryState, None, StageInput, DeliveryState](DeliveryState, input_schema=StageInput, output_schema=DeliveryState)
            .add_node("admit", run.bind(admit))
            .add_node("route", run.bind(route))
            .add_edge(START, "admit")
            .add_conditional_edges("admit", lambda state: "halted" if outcome(state) == "halted" else "admitted",
                                   {"admitted": "route", "halted": END})
            .add_conditional_edges("route", outcome, dict.fromkeys(ROUTES, END))
            .compile().with_config({"metadata": {"contract_id": "triage", "stage_declarations": declarations}}))
