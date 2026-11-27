"""Implement: publish exactly the tree that passed the target repository's checks."""

from collections.abc import Callable
from dataclasses import replace
import re
import textwrap

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from foreman.check_output import CheckOutput
from foreman.config import family
from foreman.contract import CONTRACT_PATH
from foreman.budget import cut_off
from foreman.dispatch import DISPATCH
from foreman.github import ForgeError, PublishedPullRequest
from foreman.outcomes import Halt, stopped
from foreman.proof import ReportUnreadable, digest, needs_tests, rejected, results, weakened
from foreman.redaction import redact
from foreman.review_round import history
from foreman.stage_output import ReviewState, VerifyState
from foreman.state import DeliveryState, DeliveryUpdate
from foreman.steps import StageInput, StageRun, follow, human_stop
from foreman.workspace import (BaseMerge, ContainerUnavailable, ImplementationRejected, TimedOut, UnrecognizedCheckout, WorkState,
                               WorkspaceError, ended)

HALTS = [Halt.dirty_worktree, Halt.env_unavailable, Halt.git_failed, Halt.implementation_rejected, Halt.failed_check_cap,
         Halt.publish_failed, Halt.publication_model_missing, Halt.publication_unreconciled, Halt.base_check_failed,
         Halt.base_protected_changed]
COAUTHOR_EMAILS = {"gpt": "noreply@openai.com", "claude": "noreply@anthropic.com", "grok": "noreply@x.ai"}
CHECK_START_FAILURES = {126: "command is not executable", 127: "command not found"}
STEPS = ("prepare", "implement_tests", "tests", "implement", "check", "commit", "publish")


def prepare(state: DeliveryState, run: StageRun) -> DeliveryUpdate:
    if (stop := human_stop(state, run, "prepare")).get("stop"):
        return stop
    work, checks, target = state.get("work", WorkState()), state["checks"], state["run"]
    if checks.verify_failures >= run.max_rounds:
        # A failed Verify criterion counted the round that reached the cap, and Implement owns it (:530, :541).
        failed = "; ".join(item["criterion"] for item in state.get("verify", VerifyState()).criteria if item["result"] == "failed")
        return stopped(Halt.failed_check_cap, f"The checks failed {checks.verify_failures} times. Verify last failed: {failed}", "prepare")
    workspace = run.workspace
    try:
        if work.attempt_head and not (work.head_sha and work.published_head == work.head_sha):
            if work.built and work.candidate is None:
                # After a rejected reconciliation the operator curates the checkout, and edit reconciles it again.
                return {"path": ["prepare"]}
            # A resumed attempt keeps the edits it owns, and those of its call that was cut off, which dispatch tests and makes again.
            if cut_off(state.get("attempts", []), "implement"):
                return {"path": ["prepare"]}
            worktree = workspace.inspect()
            owned = worktree.clean or run.dispatch("implement", state, work.owned).fingerprint() == work.owned
            descends = worktree.head == work.attempt_head or workspace.contains_commit(worktree.head, work.attempt_head)
            if worktree.branch != work.branch or not descends or not owned:
                return stopped(Halt.dirty_worktree, f"The checkout differs from the saved attempt on {work.branch}. It is preserved.", "prepare")
            return {"path": ["prepare"]}
        if work.base_merge is not None and not work.base_merge.head and not work.attempt_head:
            # A conflict repair whose merge is unsaved: a prepare that stopped inside its base merge left it, so the re-entry
            # abandons it and merges again from the fetch (D10). Merge confirms a conflict only at the last reviewed head,
            # which is the attempt's parent.
            reviewed = history({"rounds": state["review"].review_findings}).rounds[-1].reviewed_head
            workspace.abandon_merge(work.branch, reviewed, target.base_branch)
        if not workspace.inspect().clean:
            return stopped(Halt.dirty_worktree, "Unowned edits exist before this attempt.", "prepare")
        plan = state["plan"]
        assert plan.slice is not None
        title = redact(str(plan.slice["title"]))
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60].rstrip("-") or "change"
        # The run's id is minted at its first worker call and a new run starts without one, so no earlier run used this branch.
        run_id = state["review"].review_run_id
        assert run_id
        branch = work.branch or f"{target.issue or '<issue>'}-{slug}-{run_id[:8]}"
        workspace.create_branch(branch, target.base_branch, pinned=target.base_sha)
        if work.repair_head:
            workspace.refresh(branch, work.repair_head)
        # Setup reads the checkout at the attempt parent, so the container is built once the checkout is there.
        workspace.prepare_container(base=target.base_sha, log=run.check_log)
        ready = workspace.inspect()
        if not ready.clean:
            return stopped(Halt.dirty_worktree, "The contract's setup commands changed tracked files. They are preserved.", "prepare")
        if not work.branch and not work.repair_head and ready.head != target.base_sha:
            return stopped(Halt.git_failed, "The new build branch must start at the pinned base.", "prepare")
        if checks.base_tests is None:
            # The check runs once for the candidate at the pinned base, and its report is the base's report (:525).
            output = CheckOutput(run.check_log)
            if result := workspace.run(checks.check, base=target.base_sha, log=output):
                return stopped(Halt.base_check_failed, f"Check {checks.check!r} {ended(result)} at the pinned base "
                               f"{target.base_sha}.\n\n{output.text}", "prepare")
            checks = checks.model_copy(update={"base_tests": results(workspace.root, checks.test_report)})
        attempt = WorkState(branch=branch, base=target.base_branch, attempt_head=ready.head)
        if work.base_merge is None or work.base_merge.head:
            return {"work": attempt, "checks": checks.for_attempt(), "path": ["prepare"]}
        # Merge confirmed a conflict with the base, so Foreman merges the base head into the owned branch without committing.
        # A run keeps the contract it was admitted with, and the check runs at the base head before the merge starts, so
        # either stop leaves nothing half-merged.
        head = workspace.base_head(target.base_branch)
        if workspace.tracked_file(target.base_sha, CONTRACT_PATH) != workspace.tracked_file(head, CONTRACT_PATH):
            return stopped(Halt.base_protected_changed, f"The base head {head} changed {CONTRACT_PATH} from the pinned base "
                           f"{target.base_sha}. Nothing was merged.", "prepare")
        output = CheckOutput(run.check_log)
        if result := workspace.run_at(head, checks.check, base=target.base_sha, log=output):
            return stopped(Halt.base_check_failed, f"Check {checks.check!r} {ended(result)} at the base head {head}. "
                           f"Nothing was merged.\n\n{output.text}", "prepare")
        unmerged = workspace.merge(head)
        # The fingerprint is taken after the merge starts, so a resume owns the merge instead of reading it as someone else's edits.
        attempt = attempt.model_copy(update={"base_merge": BaseMerge(head=head, unmerged=unmerged), "built": not unmerged,
                                             "owned": run.dispatch("implement", state, "").fingerprint()})
    except UnrecognizedCheckout as exc:
        return stopped(Halt.dirty_worktree, str(exc), "prepare")
    except ContainerUnavailable as exc:
        return stopped(Halt.env_unavailable, str(exc), "prepare")
    except ReportUnreadable as exc:
        return stopped(Halt.base_check_failed, str(exc), "prepare")
    except (WorkspaceError, ForgeError) as exc:
        return stopped(Halt.git_failed, str(exc), "prepare")
    if unmerged:
        # The builder resolves the named files, with GitHub's conflict as its repair evidence.
        return {"work": attempt, "checks": checks.for_attempt(), "path": ["prepare"]}
    # Git merged every file, so no builder is called. Edit checks the merged tree, and a failing check is a failed check.
    cleared = checks.for_attempt().model_copy(update={"verify": "", "failure_output": "", "check_exit_code": None})
    return {"work": attempt, "checks": cleared, "path": ["prepare"]}


def tests(state: DeliveryState, run: StageRun) -> DeliveryUpdate:
    """A verifier-role worker writes acceptance tests, and plain code proves the check collects them and they fail (IM5)."""
    assert "work" in state
    update = run.apply("implement_tests", state, run.call(DISPATCH, "implement_tests", state))
    if halt := update.get("halt"):
        return {**update, "stop": halt}
    work, checks, workspace = state["work"], update.get("checks", state["checks"]), run.workspace
    # The path names the writer's call and the check run, not the halt's step label.
    path = [*update.get("path", []), "tests"]
    output = CheckOutput(run.check_log)
    try:
        result = workspace.run(checks.check, base=state["run"].base_sha, log=output)
        # The builder's call owns the tests the writer left, which it may not edit.
        work = work.model_copy(update={"owned": run.dispatch("implement", state, "").fingerprint()})
    except ContainerUnavailable as exc:
        return {**update, **stopped(Halt.env_unavailable, str(exc), "tests"), "path": path}
    except WorkspaceError as exc:
        return {**update, **stopped(Halt.git_failed, str(exc), "tests"), "path": path}
    assert checks.base_tests is not None  # Prepare kept the base's report.
    try:
        evidence = rejected(checks.base_tests, results(workspace.root, checks.test_report), checks.test_report)
    except ReportUnreadable as exc:
        evidence = str(exc)
    if not evidence:
        accepted = checks.model_copy(update={"accepted_digest": digest(workspace.root, checks.acceptance_tests), "failure_output": ""})
        return {**update, "work": work, "checks": accepted, "path": path}
    ran = f"\n\nCheck {checks.check!r} {ended(result)} with the tests.\n\n{output.text}"
    checks = checks.model_copy(update={"verify_failures": checks.verify_failures + 1, "failure_output": evidence + ran})
    if checks.verify_failures >= run.max_rounds:
        detail = f"The acceptance tests were rejected {checks.verify_failures} times. {evidence}"
        return {**update, **stopped(Halt.failed_check_cap, detail, "tests"), "work": work, "checks": checks, "path": path}
    return {**update, "work": work, "checks": checks, "path": path}


def edit(state: DeliveryState, run: StageRun) -> DeliveryUpdate:
    assert "work" in state
    work, checks = state["work"], state["checks"]
    workspace = run.workspace
    update: DeliveryUpdate = {}

    def finish(result: DeliveryUpdate) -> DeliveryUpdate:
        # The path names the builder call and the check run, not the halt's step label.
        return {**update, "work": work, "checks": checks, **result, "path": update.get("path", [])}

    if not work.built or checks.verify in ("fail", "weakened"):
        try:
            # The builder's call finds the run's container, built again if a check ended at its limit removed it.
            workspace.prepare_container(base=state["run"].base_sha, log=run.check_log)
        except ContainerUnavailable as exc:
            return finish(stopped(Halt.env_unavailable, str(exc), "implement"))
        except WorkspaceError as exc:
            return finish(stopped(Halt.git_failed, str(exc), "implement"))
        scope: list[object] = [state["run"].issue, work.attempt_head]
        consulted, owned = run.consult(DISPATCH, "implement", state, lambda _: scope, owned=work.owned)
        update.update(consulted)
        if update.get("stop"):
            # A halt after the builder's call keeps the edits it returned owned, so a resumed prepare accepts only those.
            return {**update, "work": work.model_copy(update={"owned": owned})}
        if run.dispatch("implement", state, "").fingerprint() != owned:
            return finish(stopped(Halt.dirty_worktree, "The worktree changed after the builder returned. It is preserved.", "implement"))
        work = work.model_copy(update={"built": True, "candidate": None, "owned": owned})
        checks = checks.model_copy(update={"verify": "", "failure_output": "", "check_exit_code": None, "checked_tree": ""})
    try:
        candidate = work.candidate
        if candidate is None:
            candidate = workspace.reconcile(work.attempt_head, work.branch, work.base,
                                            base=work.base_merge.head if work.base_merge else None)
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
        # A check command owns nothing in the checkout, so the attempt keeps the branch and head the check ran on.
        checked = workspace.inspect()
        before = run.dispatch("implement", state, "").fingerprint(checked)
        output = CheckOutput(run.check_log)
        result = workspace.run(checks.check, base=state["run"].base_sha, log=output)
        exit_code = None if isinstance(result, TimedOut) else result
        stable = workspace.tree(None) == tree
        # The builder cannot weaken the checks, so a passing candidate is compared with the base it builds on (item 10).
        base = work.base_merge.head if work.base_merge else state["run"].base_sha
        assert state["plan"].slice is not None
        weakness = (weakened(workspace.root, checks, state["plan"].slice, workspace.changed_files(base, tree))
                    if exit_code == 0 and stable else "")
        work = work.model_copy(update={"owned": before if stable else run.dispatch("implement", state, "").fingerprint(checked)})
    except ImplementationRejected as exc:
        return finish(stopped(Halt.implementation_rejected, str(exc), "implement"))
    except ContainerUnavailable as exc:
        return finish(stopped(Halt.env_unavailable, str(exc), "check"))
    except WorkspaceError as exc:
        return finish(stopped(Halt.git_failed, str(exc), "check" if work.candidate else "implement"))
    if exit_code is not None and (reason := CHECK_START_FAILURES.get(exit_code)):
        checks = checks.model_copy(update={"check_exit_code": exit_code, "failure_output": output.text})
        return finish(stopped(Halt.env_unavailable, f"Check {checks.check!r} exited with {exit_code} ({reason})", "check"))
    if exit_code == 0 and stable and not weakness:
        checks = checks.model_copy(update={"verify": "pass", "check_exit_code": 0, "checked_tree": tree})
        return finish({"pr_size": candidate["size"]})
    # A check ended at its limit is a failed check, and Workspace removed the container it ran in.
    failure = weakness or (f"Check {checks.check!r} {ended(result)}, so Foreman ended it.\n\n{output.text}" if isinstance(result, TimedOut)
               else output.text if exit_code else "The check passed but changed the candidate tree. Make the check leave the tree unchanged.")
    checks = checks.model_copy(update={"verify": "weakened" if weakness else "fail", "check_exit_code": exit_code,
                                       "failure_output": failure, "verify_failures": checks.verify_failures + 1})
    if checks.verify_failures >= run.max_rounds:
        detail = (f"Verification failed after {checks.verify_failures} rounds. "
                  + (weakness or f"Check {checks.check or '(none)'} failed."))
        return finish(stopped(Halt.failed_check_cap, detail, "check"))
    if candidate["disposition"] == "adopted":
        # The next round edits on the attempt parent, so worker commits become edits.
        try:
            workspace.soft_reset(work.attempt_head)
            parent = replace(checked, head=work.attempt_head)
            moved, work = work.owned, work.model_copy(update={"owned": run.dispatch("implement", state, "").fingerprint(parent)})
        except WorkspaceError as exc:
            return finish(stopped(Halt.git_failed, str(exc), "check"))
        return finish(follow({**state, **update}, moved, work.owned))
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
    # After a base merge, Foreman's commit completes it with the merged base head as the second parent.
    parents = (work.attempt_head, work.base_merge.head) if work.base_merge else (work.attempt_head,)
    try:
        observed = workspace.inspect()
        if observed.branch != work.branch:
            return stopped(Halt.dirty_worktree, f"The checkout left the delivery branch {work.branch}. It is preserved.", "commit")
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
        elif not (observed.clean and workspace.committed(parents, message)):
            return stopped(Halt.dirty_worktree, "HEAD differs from the expected checked commit. It is preserved.", "commit")
        if not observed.clean or workspace.tree(observed.head) != checks.checked_tree:
            return stopped(Halt.dirty_worktree, "The commit differs from the checked tree. It is preserved.", "commit")
        committed = follow(state, work.owned, run.dispatch("implement", state, "").fingerprint(observed))
    except WorkspaceError as exc:
        return stopped(Halt.git_failed, str(exc), "commit")
    return {**committed, "work": work.model_copy(update={"head_sha": observed.head}),
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
        return stopped(Halt.git_failed, str(exc), "publish")
    if not local.clean or local.branch != branch or local.head != head:
        return stopped(Halt.dirty_worktree, f"The checkout differs from the checked commit {head} on {branch}. It is preserved.", "publish")
    if checks.verified_head != head:
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
                verify = state["checks"].verify
                return "checked" if verify == "pass" else "checks_weakened" if verify == "weakened" else "check_failed"
            if step == "publish":
                return "published" if state.get("work", WorkState()).published_head else "owned_head_moved"
            if step == "prepare" and state.get("stop") == "cancelled":
                return "cancelled"
            assert state["plan"].slice is not None
            checks = state["checks"]
            if step == "tests":
                return "tests_accepted" if checks.accepted_digest else "tests_rejected"
            if step == "prepare" and needs_tests(state["plan"].slice) and not checks.accepted_digest:
                return "needs_tests"
            return {"prepare": "prepared", "commit": "committed"}[step]
        return route

    declarations = {"halts": [halt.value for halt in HALTS],
                    "retries": {"failed_checks": "src/foreman/models.toml#max_review_rounds"}}
    return (StateGraph[DeliveryState, None, StageInput, DeliveryState](DeliveryState, input_schema=StageInput, output_schema=DeliveryState)
            .add_node("prepare", run.bind(prepare))
            .add_node("tests", run.bind(tests))
            .add_node("edit", run.bind(edit))
            .add_node("commit", run.bind(commit))
            .add_node("publish", run.bind(publish))
            .add_edge(START, "prepare")
            .add_conditional_edges("prepare", after("prepare"), {"needs_tests": "tests", "prepared": "edit", "cancelled": END,
                                                                 "halted": END})
            .add_conditional_edges("tests", after("tests"), {"tests_accepted": "edit", "tests_rejected": "tests", "halted": END})
            .add_conditional_edges("edit", after("edit"), {"checked": "commit", "check_failed": "edit", "checks_weakened": "edit",
                                                           "no_change": END,
                                                           "cancelled": END, "halted": END})
            .add_conditional_edges("commit", after("commit"), {"committed": "publish", "halted": END})
            .add_conditional_edges("publish", after("publish"), {"published": END, "owned_head_moved": "prepare", "halted": END})
            .compile().with_config({"metadata": {"contract_id": "implement", "stage_declarations": declarations}}))
