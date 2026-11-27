"""Verify: drive the published head with the target repository's own verify skill and post the evidence for review."""

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from foreman.dispatch import DISPATCH
from foreman.github import CommandTimedOut, ForgeError, owned_comment
from foreman.outcomes import Halt, stopped
from foreman.stage_output import VerifyState, spec_digest
from foreman.state import DeliveryState, DeliveryUpdate
from foreman.steps import StageInput, StageRun, human_stop
from foreman.workspace import WorkState, WorkspaceError

HALTS = [Halt.dirty_worktree, Halt.git_failed, Halt.publish_failed, Halt.publication_unreconciled, Halt.criterion_unverified]
# The evidence no longer names the PR head, so Implement fetches the moved head and re-establishes it.
HEAD_MOVED = "head_moved"


def drive(state: DeliveryState, run: StageRun) -> DeliveryUpdate:
    if (stop := human_stop(state, run, "verify")).get("stop"):
        return stop
    work, verify = state.get("work", WorkState()), state.get("verify", VerifyState())
    head = work.published_head
    if head and verify.outcome == "driven" and verify.tested_head == head:
        # A resumed stage posts the evidence it reached for this head; post marks it stale if the head moved.
        return {"stop": ""}
    try:
        if not head or state["checks"].verified_head != head:
            raise WorkspaceError("Verify needs the published checked head.")
        before = run.workspace.inspect()
        if before.head != head:
            # As in Review, Implement checks the checkout's current head again.
            return {"stop": HEAD_MOVED, "checks": state["checks"].for_attempt()}
        if not before.clean or before.branch != work.branch:
            return stopped(Halt.dirty_worktree, "Verify needs the clean delivery branch at the published head.", "verify")
        pr = run.forge.pull_request_for(state["run"].repo, work.branch)
        if pr is None:
            raise ForgeError("The delivery branch has no pull request.")
    except (WorkspaceError, ForgeError) as exc:
        return stopped(Halt.git_failed, str(exc), "verify")
    if pr.headRefOid != head:
        return {"stop": HEAD_MOVED, "work": work.moved_to(pr.headRefOid), "checks": state["checks"].for_attempt()}
    result = run.call(DISPATCH, "verify", state)
    update = run.apply("verify", state, result)
    if update.get("halt"):
        return {**update, "stop": update.get("halt") or ""}
    # Evidence from a drive that left the checkout changed or unreadable is never saved, so a resume can't post it.
    unchecked: DeliveryUpdate = {**update}
    unchecked.pop("verify", None)
    try:
        after = run.workspace.inspect()
    except WorkspaceError as exc:
        return {**unchecked, **stopped(Halt.git_failed, str(exc), "verify")}
    if after != before:
        return {**unchecked, **stopped(Halt.dirty_worktree, "The verifier changed the checkout. It is kept as evidence.", "verify")}
    return {**update, "stop": ""}


def post(state: DeliveryState, run: StageRun) -> DeliveryUpdate:
    verify, work, repo = state.get("verify", VerifyState()), state.get("work", WorkState()), state["run"].repo
    try:
        pr = run.forge.pull_request_for(repo, work.branch)
    except ForgeError as exc:
        return stopped(Halt.git_failed, str(exc), "verify")
    if pr is None or pr.state != "OPEN":
        return stopped(Halt.publish_failed, "The pull request is closed or missing, so the evidence was not posted.", "verify")
    stale = pr.headRefOid != verify.tested_head
    results = "\n".join(f"- {item['result']}: {item['criterion']}\n  Observed: {item['observation']}\n  Evidence: {item['evidence']}"
                        for item in verify.criteria)
    lead = "Verify evidence, stale: the PR head moved after this drive." if stale else "Verify evidence."
    # Keyed by the run, PR, tested head and evidence, never by staleness, so a retry adopts the comment it posted.
    marker = (f"<!-- foreman-verify:{state['review'].review_run_id}:{pr.number}:{verify.tested_head}:"
              f"{spec_digest(verify.criteria)} -->")
    try:
        run.forge.issue_comment_records(repo, pr.number)
    except ForgeError as exc:
        return stopped(Halt.git_failed, f"The PR comments could not be read before posting: {exc}", "verify")
    try:
        run.forge.upsert_comment(repo, pr.number, f"{lead}\n\n{results}", [f"Tested head: {verify.tested_head}."], marker=marker)
    except CommandTimedOut as exc:
        return stopped(Halt.publication_unreconciled, f"The evidence may have posted ({exc}). Rerun to adopt it by its marker.", "verify")
    except ForgeError as exc:
        # A rejection counts only when a re-read confirms the comment is absent.
        try:
            landed = owned_comment(run.forge.issue_comment_records(repo, pr.number), marker) is not None
        except ForgeError as unread:
            return stopped(Halt.publication_unreconciled, f"The evidence post failed ({exc}) and could not be re-read ({unread}).", "verify")
        if not landed:
            return stopped(Halt.publish_failed, str(exc), "verify")
    posted = verify.model_copy(update={"outcome": "posted", "stale": stale})
    if stale:
        return {"stop": HEAD_MOVED, "work": work.moved_to(pr.headRefOid), "checks": state["checks"].for_attempt(), "verify": posted}
    # The verdict routes (verify A): a failed criterion goes back to Implement and counts once toward the failed-check cap, and
    # a drivable criterion that is unverified stops the run. Missing proof is unverified, never passed.
    checks, plan = state["checks"], state["plan"]
    assert plan.slice is not None
    if any(item["result"] == "failed" for item in verify.criteria):
        return {"stop": "failed", "verify": posted, "checks": checks.model_copy(update={"verify_failures": checks.verify_failures + 1})}
    reported, blocked = {item["criterion"]: item for item in verify.criteria}, list[str]()
    for criterion in plan.slice["criteria"]:
        proof, item = criterion["proof"], reported.get(criterion["text"])
        # A criterion the plan marked not drivable, with its reason, does not block.
        if (proof["kind"] == "drive" and proof["not_drivable"]) or (item is not None and item["result"] == "passed"):
            continue
        blocked.append(f"{criterion['text']}: " + (f"{item['observation']} Blocker: {item['evidence']}" if item is not None
                                                   else "the verifier reported no result for it."))
    if blocked:
        return {**stopped(Halt.criterion_unverified, "Verify could not prove each drivable criterion at "
                          f"{verify.tested_head}.\n" + "\n".join(f"- {line}" for line in blocked), "verify"), "verify": posted}
    return {"stop": "posted", "verify": posted}


def compile_stage(run: StageRun) -> CompiledStateGraph[DeliveryState, None, StageInput, DeliveryState]:
    def ended(state: DeliveryState, success: str) -> str:
        return "halted" if state.get("halt") else state.get("stop") or success

    declarations: dict[str, object] = {"halts": [halt.value for halt in HALTS], "retries": {}}
    return (StateGraph[DeliveryState, None, StageInput, DeliveryState](DeliveryState, input_schema=StageInput, output_schema=DeliveryState)
            .add_node("drive", run.bind(drive))
            .add_node("post", run.bind(post))
            .add_edge(START, "drive")
            .add_conditional_edges("drive", lambda state: ended(state, "driven"),
                                   {"driven": "post", HEAD_MOVED: END, "cancelled": END, "halted": END})
            .add_conditional_edges("post", lambda state: "unverified" if state.get("halt") == Halt.criterion_unverified else ended(state, "posted"),
                                   {"posted": END, "failed": END, "unverified": END, HEAD_MOVED: END, "halted": END})
            .compile().with_config({"metadata": {"contract_id": "verify", "stage_declarations": declarations}}))
