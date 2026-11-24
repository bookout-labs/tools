"""Implement: publish exactly the tree that passed the target repository's checks."""

from collections.abc import Callable
import re
import textwrap

from langgraph.func import task
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from foreman.check_output import CheckOutput
from foreman.config import family
from foreman.dispatch import DISPATCH, DispatchResult
from foreman.github import ForgeError, PublishedPullRequest
from foreman.outcomes import Halt, halted, stopped
from foreman.redaction import redact
from foreman.stage_output import ReviewState
from foreman.state import DeliveryState, DeliveryUpdate
from foreman.steps import StageInput, StageRun, human_stop
from foreman.workspace import ImplementationRejected, WorkState, WorkspaceError

HALTS = [Halt.dirty_worktree, Halt.env_unavailable, Halt.git_failed, Halt.implementation_rejected, Halt.verify_cap,
         Halt.publish_failed, Halt.publication_model_missing, Halt.publication_unreconciled]
COAUTHOR_EMAILS = {"gpt": "noreply@openai.com", "claude": "noreply@anthropic.com", "grok": "noreply@x.ai"}
CHECK_START_FAILURES = {126: "command is not executable", 127: "command not found"}
STEPS = ("prepare", "implement", "check", "commit", "publish")


def prepare(state: DeliveryState, run: StageRun) -> DeliveryUpdate:
    if (stop := human_stop(state, run, "prepare")).get("stop"):
        return stop
    work, checks, target = state.get("work", WorkState()), state["checks"], state["run"]
    workspace = run.workspace
    try:
        if work.attempt_head and not (work.head_sha and work.published_head == work.head_sha):
            if work.built and work.candidate is None:
                # After a rejected reconciliation the operator curates the checkout, and edit reconciles it again.
                return {"path": ["prepare"]}
            # A resumed attempt keeps the edits it owns.
            worktree = workspace.inspect()
            owned = worktree.clean or run.dispatch("implement", state, work.owned).fingerprint() == work.owned
            descends = worktree.head == work.attempt_head or workspace.contains_commit(worktree.head, work.attempt_head)
            if worktree.branch != work.branch or not descends or not owned:
                return stopped(Halt.dirty_worktree, f"The checkout differs from the saved attempt on {work.branch}. It is preserved.", "prepare")
            return {"path": ["prepare"]}
        if not workspace.inspect().clean:
            return stopped(Halt.dirty_worktree, "Unowned edits exist before this attempt.", "prepare")
        if checks.preflight and (exit_code := workspace.run(checks.preflight, log=run.check_log)):
            return stopped(Halt.env_unavailable, f"Preflight {checks.preflight!r} exited with {exit_code}", "prepare")
        plan = state["plan"]
        assert plan.slice is not None
        title = redact(str(plan.slice["title"]))
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60].rstrip("-") or "change"
        branch = work.branch or f"{target.issue or '<issue>'}-{slug}"
        workspace.create_branch(branch, target.base_branch, pinned=target.base_sha)
        if work.repair_head:
            workspace.refresh(branch, work.repair_head)
        ready = workspace.inspect()
        if not work.branch and not work.repair_head and ready.head != target.base_sha:
            return stopped(Halt.git_failed, "The new build branch must start at the pinned base.", "prepare")
    except (WorkspaceError, ForgeError) as exc:
        return stopped(Halt.git_failed, str(exc), "prepare")
    return {"work": WorkState(branch=branch, base=target.base_branch, attempt_head=ready.head),
            "checks": checks.for_attempt(), "path": ["prepare"]}


def edit(state: DeliveryState, run: StageRun) -> DeliveryUpdate:
    assert "work" in state
    work, checks = state["work"], state["checks"]
    workspace = run.workspace
    update: DeliveryUpdate = {}

    def finish(result: DeliveryUpdate) -> DeliveryUpdate:
        # The path names the builder call and the check run, not the halt's step label.
        return {**update, "work": work, "checks": checks, **result, "path": update.get("path", [])}

    @task
    def route(current: DeliveryState, result: DispatchResult) -> tuple[DeliveryUpdate, str]:
        """Route the builder's result by its tree and its no_change evidence (rules.routing).

        A task, so a step resumed after an answer replays each earlier decision instead of reading the live checkout."""
        owned, evidence = result.get("fingerprint", ""), str(result.get("output", {}).get("no_change") or "").strip()
        try:
            tree = workspace.inspect()
            # Reconciliation decides what the builder produced, so it confirms a tree that looks unedited.
            clean = (tree.clean and (tree.branch, tree.head) == (work.branch, work.attempt_head)
                     and workspace.reconcile(work.attempt_head, work.branch, work.base) is None)
            if not clean and not evidence:
                return {}, owned
            findings = ", ".join(str(finding.get("id")) for finding in state["review"].findings)
            first = not findings and not checks.verify_failures and work.attempt_head == state["run"].base_sha
            if clean and evidence and first:
                return {"stop": "no_change", "halt_detail": evidence}, owned
            if clean and evidence:
                reason = "The builder reported no_change on a repair round" + (f"; unresolved findings: {findings}." if findings else ".")
            elif clean:
                reason = "The builder edited nothing and gave no no_change evidence."
            else:
                reason = "The builder edited the tree and also reported no_change."
                workspace.reconcile(work.attempt_head, work.branch, work.base)  # Only edits Foreman could adopt reach the gate.
                if tree.head != work.attempt_head:
                    # The answered call edits on the attempt parent, so the worker's commits become edits it owns.
                    workspace.soft_reset(work.attempt_head)
                    owned = run.dispatch("implement", current, "").fingerprint()
        except WorkspaceError as exc:
            # The builder's result stays owned but unbuilt, so a resumed edit asks the builder again and routes its result.
            code = Halt.implementation_rejected if isinstance(exc, ImplementationRejected) else Halt.git_failed
            return {**halted(code, str(exc), None), "work": work.model_copy(update={"owned": owned})}, owned
        return {"human_question": f"{reason} Builder report: {current.get('implementation', '')}"}, owned

    if not work.built or checks.verify == "fail":
        scope: list[object] = [state["run"].issue, work.attempt_head]
        consulted, owned = run.consult(DISPATCH, "implement", state, lambda _: scope, owned=work.owned,
                                       route=lambda current, result: route(current, result).result())
        update.update(consulted)
        if update.get("stop"):
            return update
        if run.dispatch("implement", state, "").fingerprint() != owned:
            return finish(stopped(Halt.dirty_worktree, "The worktree changed after the builder returned. It is preserved.", "implement"))
        work = work.model_copy(update={"built": True, "candidate": None, "owned": owned})
        checks = checks.model_copy(update={"verify": "", "failure_output": "", "check_exit_code": None, "checked_tree": ""})
    try:
        candidate = work.candidate
        if candidate is None:
            candidate = workspace.reconcile(work.attempt_head, work.branch, work.base)
            if candidate is None:
                # The builder returned edits that are gone, so a resumed edit asks the builder again.
                work = work.model_copy(update={"built": False})
                return finish(stopped(Halt.implementation_rejected, "The checkout has none of the edits the builder returned. "
                                      "Restore them, or rerun to build again.", "implement"))
            work = work.model_copy(update={"candidate": candidate, "owned": run.dispatch("implement", state, "").fingerprint()})
        tree = workspace.tree(None)
        if tree != candidate["size"]["head"]:
            return finish(stopped(Halt.dirty_worktree, "The edits changed after reconciliation. They are preserved.", "check"))
        if checks.verify == "pass" and checks.checked_tree == tree:
            return finish({})
        update["path"] = [*update.get("path", []), "check"]
        output = CheckOutput(run.check_log)
        exit_code = workspace.run(checks.check, log=output)
        detail = ""
        if reason := CHECK_START_FAILURES.get(exit_code):
            detail = f"Check {checks.check!r} exited with {exit_code} ({reason})"
        elif exit_code and checks.preflight and (preflight := workspace.run(checks.preflight, log=run.check_log)):
            detail = f"Preflight {checks.preflight!r} exited with {preflight}"
        stable = workspace.tree(None) == tree
        work = work.model_copy(update={"owned": run.dispatch("implement", state, "").fingerprint()})
    except ImplementationRejected as exc:
        return finish(stopped(Halt.implementation_rejected, str(exc), "implement"))
    except WorkspaceError as exc:
        return finish(stopped(Halt.git_failed, str(exc), "check" if work.candidate else "implement"))
    if detail:
        checks = checks.model_copy(update={"check_exit_code": exit_code, "failure_output": output.text})
        return finish(stopped(Halt.env_unavailable, detail, "check"))
    if exit_code == 0 and stable:
        checks = checks.model_copy(update={"verify": "pass", "check_exit_code": 0, "checked_tree": tree})
        return finish({"pr_size": candidate["size"]})
    failure = output.text if exit_code else "The check passed but changed the candidate tree. Make the check leave the tree unchanged."
    checks = checks.model_copy(update={"verify": "fail", "check_exit_code": exit_code, "failure_output": failure,
                                       "verify_failures": checks.verify_failures + 1})
    if checks.verify_failures >= run.max_rounds:
        detail = f"Verification failed after {checks.verify_failures} rounds. Check {checks.check or '(none)'} failed."
        return finish(stopped(Halt.verify_cap, detail, "check"))
    if candidate["disposition"] == "adopted":
        # The next round edits on the attempt parent, so worker commits become edits.
        try:
            workspace.soft_reset(work.attempt_head)
            work = work.model_copy(update={"owned": run.dispatch("implement", state, "").fingerprint()})
        except WorkspaceError as exc:
            return finish(stopped(Halt.git_failed, str(exc), "check"))
    return finish({})


def commit(state: DeliveryState, run: StageRun) -> DeliveryUpdate:
    assert "work" in state
    work, checks = state["work"], state["checks"]
    plan = state["plan"]
    assert plan.slice is not None
    subject = f"{state['run'].issue or '<issue>'}: {' '.join(redact(str(plan.slice['title'])).split())}"
    model = state.get("models_used", {}).get("implement")
    if not model:
        return stopped(Halt.publication_model_missing, "Publication needs the implement model.", "commit")
    message = f"{textwrap.shorten(subject, width=72, placeholder='')}\n\nCo-Authored-By: {redact(model)} <{COAUTHOR_EMAILS[family(model)]}>"
    workspace = run.workspace
    try:
        observed = workspace.inspect()
        if observed.branch != work.branch:
            return stopped(Halt.git_failed, "Restore the delivery branch before committing.", "commit")
        candidate = work.candidate
        assert candidate is not None
        if candidate["disposition"] == "adopted" and observed.head == candidate["head"] and observed.clean:
            # Worker commits become edits on the attempt parent, so Foreman commits exactly the checked tree.
            workspace.soft_reset(work.attempt_head)
            observed = workspace.inspect()
        if observed.head == work.attempt_head:
            if observed.clean or workspace.tree(None) != checks.checked_tree:
                return stopped(Halt.dirty_worktree, "The edits differ from the checked tree. They are preserved.", "commit")
            workspace.commit(message)
            observed = workspace.inspect()
        elif not (observed.clean and workspace.committed(work.attempt_head, message)):
            return stopped(Halt.dirty_worktree, "HEAD differs from the expected checked commit. It is preserved.", "commit")
        if not observed.clean or workspace.tree(observed.head) != checks.checked_tree:
            return stopped(Halt.dirty_worktree, "The commit differs from the checked tree. It is preserved.", "commit")
    except WorkspaceError as exc:
        return stopped(Halt.git_failed, str(exc), "commit")
    return {"work": work.model_copy(update={"head_sha": observed.head}),
            "checks": checks.model_copy(update={"verified_head": observed.head}), "path": ["commit"]}


def publish(state: DeliveryState, run: StageRun) -> DeliveryUpdate:
    assert "work" in state
    work, checks, target = state["work"], state["checks"], state["run"]
    branch, base, head, repo = work.branch, work.base, work.head_sha, target.repo
    plan = state["plan"]
    assert plan.slice is not None and head
    marker = f"Closes #{target.issue or '<issue>'}"
    workspace, forge = run.workspace, run.forge

    def owned(pr: PublishedPullRequest | None) -> bool:
        return (pr is not None and pr.state == "OPEN" and not pr.isCrossRepository and pr.headRefName == branch
                and pr.baseRefName == base and marker in pr.body)

    try:
        local = workspace.inspect()
    except WorkspaceError as exc:
        return stopped(Halt.publish_failed, str(exc), "publish")
    if not local.clean or local.branch != branch or local.head != head or checks.verified_head != head:
        return stopped(Halt.publish_failed, "Restore the clean delivery branch at its checked commit before publishing.", "publish")
    try:
        # A write from an interrupted attempt may have landed, so an unreadable observation stays unresolved.
        remote = workspace.remote_head(branch)
        # The attempt parent and its ancestors fast-forward. Any other remote head moved.
        moved = remote is not None and remote not in {work.attempt_head, head} and not workspace.contains_commit(head, remote)
        pr = forge.pull_request_for(repo, branch) if moved else None
    except (WorkspaceError, ForgeError) as exc:
        return stopped(Halt.publication_unreconciled, f"The remote branch or pull request could not be read ({exc}).", "publish")
    if moved:
        if not owned(pr) or pr is None or pr.headRefOid != remote:
            return stopped(Halt.publish_failed, f"The remote branch moved to {remote}, which this run does not own. "
                           "It is preserved and never force-pushed.", "publish")
        # Someone advanced the owned PR. Start a new attempt on its head without prior check or review authority.
        try:
            workspace.uncommit(work.attempt_head)
        except WorkspaceError as exc:
            return stopped(Halt.git_failed, str(exc), "publish")
        review = state["review"]
        return {"work": WorkState(branch=branch, base=base, repair_head=remote), "checks": checks.for_attempt(),
                "review": ReviewState(review_findings=review.review_findings, settled_rulings=review.settled_rulings,
                                      review_run_id=review.review_run_id), "path": ["publish"]}
    if remote != head:
        try:
            workspace.push(branch, repo)
        except WorkspaceError as exc:
            try:
                remote = workspace.remote_head(branch)
            except WorkspaceError:
                return stopped(Halt.publication_unreconciled, f"The push failed ({exc}) and the remote branch could not be read.", "publish")
            if remote != head:
                return stopped(Halt.publish_failed, str(exc), "publish")
    try:
        pr = forge.pull_request_for(repo, branch)
    except ForgeError as exc:
        return stopped(Halt.publication_unreconciled, f"The pull request could not be read ({exc}).", "publish")
    if pr is None:
        title = " ".join(redact(str(plan.slice["title"])).split())
        try:
            forge.open_pull_request(repo, branch, base, title, marker)
            error = None
        except ForgeError as exc:
            error = exc
        try:
            pr = forge.pull_request_for(repo, branch)
        except ForgeError as exc:
            return stopped(Halt.publication_unreconciled, f"The pull request could not be observed after opening it ({error or exc}).", "publish")
        if pr is None:
            if error is None:
                return stopped(Halt.publication_unreconciled, "GitHub accepted the pull request but does not show it yet.", "publish")
            return stopped(Halt.publish_failed, str(error), "publish")
    if not owned(pr):
        return stopped(Halt.publish_failed, "The branch pull request does not match this run's repository, issue, branch and base. "
                       "It is preserved.", "publish")
    if pr.headRefOid != head:
        return stopped(Halt.publication_unreconciled, f"Pull request #{pr.number} does not show the checked head {head} yet.", "publish")
    return {"work": work.model_copy(update={"published_head": head}), "path": ["publish"]}


def compile_stage(run: StageRun) -> CompiledStateGraph[DeliveryState, None, StageInput, DeliveryState]:
    def after(step: str) -> Callable[[DeliveryState], str]:
        def route(state: DeliveryState) -> str:
            if state.get("halt"):
                return "halted"
            if step == "edit":
                stop = state.get("stop")
                if stop in {"cancelled", "no_change"}:
                    return stop
                return "checked" if state["checks"].verify == "pass" else "check_failed"
            if step == "publish":
                return "published" if state.get("work", WorkState()).published_head else "owned_head_moved"
            if step == "prepare" and state.get("stop") == "cancelled":
                return "cancelled"
            return {"prepare": "prepared", "commit": "committed"}[step]
        return route

    declarations = {"halts": [halt.value for halt in HALTS],
                    "retries": {"failed_checks": "src/foreman/models.toml#max_review_rounds"}}
    return (StateGraph[DeliveryState, None, StageInput, DeliveryState](DeliveryState, input_schema=StageInput, output_schema=DeliveryState)
            .add_node("prepare", run.bind(prepare))
            .add_node("edit", run.bind(edit))
            .add_node("commit", run.bind(commit))
            .add_node("publish", run.bind(publish))
            .add_edge(START, "prepare")
            .add_conditional_edges("prepare", after("prepare"), {"prepared": "edit", "cancelled": END, "halted": END})
            .add_conditional_edges("edit", after("edit"), {"checked": "commit", "check_failed": "edit", "no_change": END,
                                                           "cancelled": END, "halted": END})
            .add_conditional_edges("commit", after("commit"), {"committed": "publish", "halted": END})
            .add_conditional_edges("publish", after("publish"), {"published": END, "owned_head_moved": "prepare", "halted": END})
            .compile().with_config({"metadata": {"contract_id": "implement", "stage_declarations": declarations}}))
