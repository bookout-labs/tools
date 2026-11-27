"""Review: assess the full current pull request, post one exact-head report and route on its severity."""

from typing import Literal

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from pydantic import TypeAdapter

from foreman.contracts import InvalidOutput
from foreman.dispatch import DISPATCH
from foreman.github import CommandTimedOut, ForgeError, PullRequest, ReviewComment
from foreman.outcomes import Halt, stopped
from foreman.review_round import history
from foreman.stage_output import Finding, VerifyState
from foreman.state import DeliveryState, DeliveryUpdate
from foreman.steps import StageInput, StageRun, human_stop
from foreman.workspace import ContainerUnavailable, ReviewScope, TimedOut, WorkspaceError, WorktreeInspectionFailed

HALTS = [Halt.review_cap, Halt.review_not_posted, Halt.env_unavailable, Halt.git_failed, Halt.publication_unreconciled]
# Foreman's own advisory finding, which names each changed file outside the plan's file list and the tests.
OUTSIDE_PLAN = "foreman-outside-plan"
# The checked evidence no longer names the reviewed head, so Implement re-establishes it.
HEAD_MOVED = "head_moved"


def inspect(state: DeliveryState, run: StageRun) -> DeliveryUpdate:
    if (stop := human_stop(state, run, "review")).get("stop"):
        return stop
    review = state["review"]
    try:
        rounds = history({"rounds": review.review_findings}).rounds
    except InvalidOutput as exc:
        return stopped(Halt.invalid_output, str(exc), "review")
    head = state["checks"].verified_head
    target = (state.get("dispatch_plan") or {}).get("target")
    last = rounds[-1] if rounds else None
    if (last and last.complete and not review.review_receipt and target and last.reviewed_head == head
            and (target["headRefOid"], target["baseRefOid"]) == (head, last.base)):
        # A resumed round keeps the assessment it reached for this head and base, and posts it.
        return {"stop": ""}
    try:
        if not head:
            raise WorktreeInspectionFailed("The run did not record a verified HEAD.")
        worktree = run.workspace.inspect()
        if worktree.head != head:
            return {"stop": HEAD_MOVED, "checks": state["checks"].for_attempt()}
        if not worktree.clean or worktree.branch is None:
            raise WorktreeInspectionFailed("The worktree changed after verification.")
        # The reviewer runs its commands in the run's container, built again when the run has none, as Verify does.
        run.workspace.prepare_container(base=state["run"].base_sha, log=run.check_log)
    except ContainerUnavailable as exc:
        return stopped(Halt.env_unavailable, str(exc), "review")
    except WorkspaceError as exc:
        return stopped(Halt.git_failed, str(exc), "review")

    def settle(current: DeliveryState, answer: str) -> DeliveryUpdate:
        # The human's ruling on a disputed scope or rule settles it for the fresh review of this head.
        current_review = current["review"]
        past = history({"rounds": current_review.review_findings, "settled": current_review.settled_rulings})
        rulings = [ruling.model_dump() for ruling in past.settled] + [{"id": f"design:{len(past.rounds)}", "text": answer}]
        return {"review": current_review.model_copy(update={"settled_rulings": rulings})}

    def bound(current: DeliveryState) -> list[object]:
        # The question binds the verified head and the findings of the round that asked it (item 18).
        asked = history({"rounds": current["review"].review_findings}).rounds[-1]
        return [current["checks"].verified_head, [[finding.id, finding.location] for finding in asked.findings]]

    # One round is three fresh sessions, one per fixed question, that post together as one report (item 17).
    update: DeliveryUpdate = {}
    current: DeliveryState = state
    while True:
        answered, _ = run.consult(DISPATCH, "review", current, bound, answered=settle)
        # The round enters the path once, as the stage it is, however many sessions it took.
        attempts, path = [*update.get("attempts", []), *answered.pop("attempts", [])], update.get("path") or answered.pop("path", [])
        answered.pop("path", None)
        update = {**update, **answered, "attempts": attempts, "path": path}
        if update.get("halt") or update.get("stop") == "cancelled":
            return update
        current = {**current, **update, "attempts": [*state.get("attempts", []), *attempts]}
        if history({"rounds": current["review"].review_findings}).rounds[-1].complete:
            # The round's own result replaces the route an earlier round left.
            return {**update, "stop": ""}


def demonstrate(state: DeliveryState, run: StageRun) -> DeliveryUpdate:
    """Run each blocking pass (a) or (b) finding's demonstration at the reviewed head. One that does not fail leaves the
    finding advisory. Add Foreman's advisory finding for the changed files outside the plan's list and the tests."""
    review, head = state["review"], state["checks"].verified_head
    report = history({"rounds": review.review_findings}).rounds[-1]

    def demonstrated(finding: Finding) -> Finding:
        demonstration = finding.demonstration
        if (finding.review_pass not in ("a", "b") or finding.ruling_id is not None or finding.severity not in ("P0", "P1")
                or demonstration is None):
            return finding
        files = {demonstration["file"]["path"]: demonstration["file"]["content"]} if demonstration["file"] else {}
        result = run.workspace.demonstrate(demonstration["command"], files, base=state["run"].base_sha, log=run.check_log)
        # A demonstration that ran out of time did not fail, so its finding stays advisory.
        return finding.model_copy(update={"demonstration_exit": None if isinstance(result, TimedOut) else result})

    def tested(path: str) -> bool:
        # A test file, by the conventions test runners collect: a test or tests directory, or a test_ or _test name.
        *directories, name = path.split("/")
        stem = name.rsplit(".", 1)[0]
        return bool({"test", "tests"} & set(directories)) or stem.startswith("test_") or stem.endswith(("_test", ".test", ".spec"))

    try:
        if run.workspace.inspect().head != head:
            return {"stop": HEAD_MOVED, "checks": state["checks"].for_attempt()}
        findings = [demonstrated(finding) for finding in report.findings if finding.id != OUTSIDE_PLAN]
        context = run.workspace.review_context(report.base, report.reviewed_head)
    except ContainerUnavailable as exc:
        return stopped(Halt.env_unavailable, str(exc), "review")
    except WorkspaceError as exc:
        return stopped(Halt.git_failed, str(exc), "review")
    plan = state["plan"].slice
    listed = set(plan["files"]) if plan else set[str]()
    if outside := sorted(path for path in context.head.changed | context.base.changed if path not in listed and not tested(path)):
        side: Literal["head", "base"] = "head" if outside[0] in context.head.files else "base"
        findings.append(Finding(id=OUTSIDE_PLAN, kind="scope", ruling_id=None, severity="P2",
                                summary="The change touches files outside the plan's list.",
                                location={"side": side, "path": outside[0], "line": None},
                                impact="These files are not in the plan's file list.", evidence=", ".join(outside),
                                demonstration=None, cites=None))
    report = report.model_copy(update={"findings": findings})
    blockers = report.blockers
    return {"review": review.model_copy(update={
        "review": "changes" if report.severity else "approve",
        "detail": "\n".join(f"{finding.severity} {finding.summary}" for finding in blockers),
        "review_findings": [*review.review_findings[:-1], report.model_dump(mode="json")],
        "findings": [finding.record() for finding in blockers]})}


def post(state: DeliveryState, run: StageRun) -> DeliveryUpdate:
    review = state["review"]
    if review.review_receipt:
        return {}
    assert "dispatch_plan" in state and review.review_run_id
    plan = state["dispatch_plan"]
    assert plan["target"] is not None and plan["scope"] is not None
    rounds = history({"rounds": review.review_findings}).rounds
    report, target, repo = rounds[-1], PullRequest.model_validate(plan["target"]), state["run"].repo
    if report.reviewed_head != state["checks"].verified_head or (report.reviewed_head, report.base) != (target.headRefOid, target.baseRefOid):
        return {"stop": HEAD_MOVED, "checks": state["checks"].for_attempt()}
    scope = TypeAdapter(ReviewScope).validate_python(plan["scope"])
    body = [f"Review route: {report.severity or 'none'}. Head: {report.reviewed_head}. Base ref: {report.base}."]
    verify = state.get("verify", VerifyState())
    if verify.outcome == "no_skill" and verify.tested_head == report.reviewed_head:
        body.append("Verify: the repository has no verify skill, so the change was not driven before review.")
    for review_pass, provenance in report.provenance.items():
        reason = provenance.reason.replace("_", " ") if provenance.reason else "not needed"
        body.append(f"Pass ({review_pass}) reviewer {provenance.reviewer}; implemented by {', '.join(provenance.contributors)}; "
                    f"same-family fallback: {reason}.")
    if scope.inline is None:
        body.append("The review patch could not be parsed, so every finding is listed here instead of inline.")
    comments: list[ReviewComment] = []
    for finding in sorted(report.findings, key=lambda item: item.severity):
        location = finding.location
        path, line = location["path"], location["line"]
        label = (f"Advisory, settled by {finding.ruling_id}" if finding.ruling_id else finding.blocking_severity
                 or f"Advisory {finding.severity}")
        message = f"[{label}, pass ({finding.review_pass})] {finding.summary}\n\nImpact: {finding.impact}\n\nEvidence: {finding.evidence}"
        if location["side"] == "head" and line is not None and scope.inline is not None and (path, line) in scope.inline:
            comments.append({"path": path, "line": line, "side": "RIGHT", "body": message})
        else:
            body.append(f"{location['side']} {f'{path}:{line}' if line is not None else path} {message}")
    marker = f"{review.review_run_id}:{len(rounds)}:{report.base}:{report.reviewed_head}"
    try:
        # The forge rechecks the PR, head and base, and adopts the report that carries this marker before it writes another.
        receipt = run.forge.post_review(repo, target, "\n\n".join(body), comments, marker=marker)
    except CommandTimedOut as exc:
        return stopped(Halt.publication_unreconciled, f"The review may have posted ({exc}). Rerun to adopt it by its marker.", "review")
    except ForgeError as exc:
        return stopped(Halt.review_not_posted, str(exc), "review")
    if receipt["state"] != "COMMENTED" or receipt["commit_id"] != report.reviewed_head:
        return stopped(Halt.review_not_posted, "The review receipt does not match the submitted COMMENT.", "review")
    return {"review": review.model_copy(update={"review_receipt": receipt})}


def route(state: DeliveryState, run: StageRun) -> DeliveryUpdate:
    review = state["review"]
    past = history({"rounds": review.review_findings})
    severity = past.rounds[-1].severity if past.rounds else None
    if severity and (blocking := past.blocking) >= run.max_rounds:
        return stopped(Halt.review_cap, f"Review requested changes after {blocking} rounds. Last review: {review.detail}", "review")
    return {"stop": severity or "none"}


def compile_stage(run: StageRun) -> CompiledStateGraph[DeliveryState, None, StageInput, DeliveryState]:
    def ended(state: DeliveryState) -> str | None:
        if state.get("halt"):
            return "halted"
        stop = state.get("stop")
        return stop if stop in {HEAD_MOVED, "cancelled"} else None

    declarations: dict[str, object] = {"halts": [halt.value for halt in HALTS],
                                       "retries": {"blocking_rounds": "src/foreman/models.toml#max_review_rounds"}}
    return (StateGraph[DeliveryState, None, StageInput, DeliveryState](DeliveryState, input_schema=StageInput, output_schema=DeliveryState)
            .add_node("inspect", run.bind(inspect))
            .add_node("demonstrate", run.bind(demonstrate))
            .add_node("post", run.bind(post))
            .add_node("route", run.bind(route))
            .add_edge(START, "inspect")
            .add_conditional_edges("inspect", lambda state: ended(state) or "assessed",
                                   {"assessed": "demonstrate", HEAD_MOVED: END, "cancelled": END, "halted": END})
            .add_conditional_edges("demonstrate", lambda state: ended(state) or "demonstrated",
                                   {"demonstrated": "post", HEAD_MOVED: END, "halted": END})
            .add_conditional_edges("post", lambda state: ended(state) or "posted", {"posted": "route", HEAD_MOVED: END, "halted": END})
            .add_conditional_edges("route", lambda state: "halted" if state.get("halt") else state.get("stop", ""),
                                   {"P0": END, "P1": END, "none": END, "halted": END})
            .compile().with_config({"metadata": {"contract_id": "review", "stage_declarations": declarations}}))
