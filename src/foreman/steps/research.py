"""Research: answer the issue's question from the pinned base and confirm one answer comment."""

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from foreman.dispatch import DISPATCH
from foreman.github import BOT_LOGIN, ForgeError
from foreman.outcomes import Halt, stopped
from foreman.stage_output import spec_digest
from foreman.state import DeliveryState, DeliveryUpdate
from foreman.steps import StageInput, StageRun

HALTS = [Halt.git_failed, Halt.publication_unreconciled]


def answer(state: DeliveryState, run: StageRun) -> DeliveryUpdate:
    if state.get("research"):
        return {}
    target = state["run"]
    update, _ = run.consult(DISPATCH, "research", state, lambda plan: [target.repo.lower(), target.issue, target.base_sha])
    return update


def post(state: DeliveryState, run: StageRun) -> DeliveryUpdate:
    target, text = state["run"], state.get("research", "")
    repo, issue = target.repo, target.issue
    assert issue is not None
    marker = f"<!-- foreman answer {spec_digest([repo.lower(), issue, target.base_sha, text])} -->"

    def posted() -> bool:
        return any(comment.user is not None and comment.user.login == BOT_LOGIN and marker in (comment.body or "")
                   for comment in run.forge.issue_comment_records(repo, issue))

    try:
        # An interrupted attempt may have posted, so the saved marker is read before any write.
        if posted():
            return {"stop": "answered"}
    except ForgeError as exc:
        return stopped(Halt.git_failed, f"The issue comments could not be read ({exc}).", "post")
    try:
        run.forge.comment_issue(repo, issue, f"{text}\n\n{marker}")
        error = None
    except ForgeError as exc:
        error = exc
    try:
        if posted():
            return {"stop": "answered"}
    except ForgeError as exc:
        return stopped(Halt.publication_unreconciled, f"The answer could not be confirmed ({error or exc}).", "post")
    return stopped(Halt.publication_unreconciled, f"GitHub does not show the App's answer comment ({error or 'not listed yet'}).", "post")


def compile_stage(run: StageRun) -> CompiledStateGraph[DeliveryState, None, StageInput, DeliveryState]:
    def after_answer(state: DeliveryState) -> str:
        return "halted" if state.get("halt") else "cancelled" if state.get("stop") == "cancelled" else "answer"

    declarations: dict[str, object] = {"halts": [halt.value for halt in HALTS], "retries": {}}
    return (StateGraph[DeliveryState, None, StageInput, DeliveryState](DeliveryState, input_schema=StageInput, output_schema=DeliveryState)
            .add_node("answer", run.bind(answer))
            .add_node("post", run.bind(post))
            .add_edge(START, "answer")
            .add_conditional_edges("answer", after_answer, {"answer": "post", "cancelled": END, "halted": END})
            .add_conditional_edges("post", lambda state: "halted" if state.get("halt") else "answered",
                                   {"answered": END, "halted": END})
            .compile().with_config({"metadata": {"contract_id": "research", "stage_declarations": declarations}}))
