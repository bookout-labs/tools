"""Triage: admit one issue against its pinned repository and route it to research or planning."""

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from foreman.contracts import InvalidOutput
from foreman.dispatch import DISPATCH
from foreman.doctor import NotReady, admitted
from foreman.github import BOT_LOGIN, READY_LABEL, ForgeError, unmet_prerequisites
from foreman.outcomes import Halt, stopped
from foreman.stage_output import load_stored_slice, merge_marker
from foreman.state import DeliveryState, DeliveryUpdate
from foreman.steps import StageInput, StageRun, human_stop
from foreman.workspace import WorkspaceError

HALTS = [Halt.missing_slice, Halt.skill_missing, Halt.invalid_output, Halt.git_failed, Halt.not_ready]
ROUTES = ("question", "plan", "supplied_slice", "cancelled", "halted")


def admit(state: DeliveryState, run: StageRun) -> DeliveryUpdate:
    target = state["run"]
    # The runner already ran doctor's GitHub requirements and read the login this stop read counts from.
    if (stop := human_stop(state, run, "admit")).get("stop"):
        return stop
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
        if READY_LABEL not in issue["labels"]:
            # Only an issue that carries Ready is admitted, so a delivery that outlived its label leaves it unadmitted.
            return {"stop": "blocked", "halt_detail": f"#{number} does not carry the {READY_LABEL} label.", "path": ["admit"]}
        plan = state["plan"].model_copy(update={"issue_kind": "slice" if "slice" in issue["labels"] else "parent"})
        item = None
        if plan.issue_kind == "slice":
            try:
                item = load_stored_slice(issue["body"])
            except InvalidOutput as exc:
                return stopped(Halt.missing_slice, str(exc), "admit")
            # Only merge.revert files a revert: the App's issue, marked for the merge commit a saved run recorded it for (TR8).
            if item.kind == "revert" and not (issue["author"].casefold() == BOT_LOGIN.casefold() and (sha := run.reverts(repo, number))
                                              and sha == item.merge_commit_sha and merge_marker("revert", repo, sha) in issue["body"]):
                return stopped(Halt.invalid_output, f"#{number} carries a revert slice that no Foreman run filed for its merge commit.", "admit")
            plan = plan.model_copy(update={"slice": item.record(), "work_kind": item.work_kind})
        workspace.validate_origin(issue["repo"])
        if detail := unmet_prerequisites(forge, repo, forge.blockers(repo, number), item):
            # The issue stays unadmitted. A later Ready recheck admits it once its prerequisites merge.
            return {"stop": "blocked", "halt_detail": detail, "path": ["admit"]}
        base = forge.default_branch(repo)
        pinned = workspace.pin_base(base)
        # The run's check and verify skill come from the contract at its pinned base, whatever its branch later does.
        assert run.greens is not None
        pinned_contract = admitted(workspace, forge, repo, pinned, run.greens, run.check_log)
        # Each related repository is pinned once, here, so a resumed run reads the same commits.
        related = [workspace.check_out_related(entry.repository, forge.default_branch(entry.repository))
                   for entry in pinned_contract.related_repositories]
    except NotReady as exc:
        return stopped(Halt.not_ready, exc.halt_detail(), "admit")
    except (ForgeError, WorkspaceError) as exc:
        return stopped(Halt.git_failed, str(exc), "admit")
    issue_text = issue["title"] + ("\n\n" + issue["body"] if issue["body"] else "")
    request = issue_text
    if target.request:
        request += f"\n\nOperator context:\n{target.request}"
    return {"run": target.model_copy(update={"repo": issue["repo"], "request": request, "issue_text_length": len(issue_text),
                                             "base_branch": base, "base_sha": pinned}),
            "checks": state["checks"].model_copy(update={"check": pinned_contract.check, "verify_skill": pinned_contract.verify_skill,
                                                         "test_report": pinned_contract.reports, "mutation": pinned_contract.mutation.command,
                                                         "sample_cap": pinned_contract.mutation.sample_cap}), "plan": plan,
            "related": related}


def route(state: DeliveryState, run: StageRun) -> DeliveryUpdate:
    # A resume passes a triage that chose its route. A question it asked is no route, so the call is made again.
    if state["plan"].triage in ROUTES:
        return {}
    target = state["run"]
    update, _ = run.consult(DISPATCH, "triage", state, lambda plan: [target.repo.lower(), target.issue, target.base_sha])
    plan = update.get("plan", state["plan"])
    if not (update.get("halt") or update.get("stop")):
        # Triage only classifies. A slice the issue itself carries is validated even when the issue also asks, and a rejected
        # one is repaired by drafting.
        if plan.spec_rejection:
            update["plan"] = plan.model_copy(update={"triage": "plan"})
        elif plan.slice is not None:
            update["plan"] = plan.model_copy(update={"triage": "supplied_slice"})
    return update


def compile_stage(run: StageRun) -> CompiledStateGraph[DeliveryState, None, StageInput, DeliveryState]:
    def outcome(state: DeliveryState) -> str:
        if state.get("halt") == Halt.not_ready:
            return Halt.not_ready
        if state.get("halt") or state.get("stop") == "blocked":
            return "halted"
        return "cancelled" if state.get("stop") == "cancelled" else state["plan"].triage

    declarations: dict[str, object] = {"halts": [halt.value for halt in HALTS], "retries": {}}
    return (StateGraph[DeliveryState, None, StageInput, DeliveryState](DeliveryState, input_schema=StageInput, output_schema=DeliveryState)
            .add_node("admit", run.bind(admit))
            .add_node("route", run.bind(route))
            .add_edge(START, "admit")
            .add_conditional_edges("admit", lambda state: ended if (ended := outcome(state)) in ("not_ready", "cancelled", "halted") else "admitted",
                                   {"admitted": "route", "not_ready": END, "cancelled": END, "halted": END})
            .add_conditional_edges("route", outcome, dict.fromkeys(ROUTES, END))
            .compile().with_config({"metadata": {"contract_id": "triage", "stage_declarations": declarations}}))
