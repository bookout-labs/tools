"""The delivery graph. Edges live here. Models live in models.toml."""

from uuid import uuid4
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from functools import cached_property
from pathlib import Path
from typing import Literal, Protocol
import os
import re
import textwrap

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from langgraph.types import interrupt
from pydantic import TypeAdapter

from foreman.config import Config, ModelSpec, family, load_config
from foreman.dry_run import DryRun, ScriptHandler
from foreman.dispatch import DispatchRun, Handler, InvalidOutput, compile_dispatch
from foreman.github import (
    MERGE_HOLD_VARIABLE, Forge, ForgeError, MergeHoldUnreadable, MergeNotArmed, HandoffChanged, QueueRejected, PullRequest, ReviewComment, merge_command,
)
from foreman.skill_rules import SkillMissing, SkillSource, export_skills, stage_methods
from foreman.redaction import redact
from foreman.stage_output import (
    PlanState, CandidateSlice, SpecAuthor, SpecCandidate, SpecTarget, SpecCheckOutput, SpecRejection, WorkKind, DispatchPlan, DispatchPlanRecord, AttemptRecord, Contract, SliceRecord, StageOutput, PULL_REQUEST_LINES, StoredSlice, load_stored_slice, parse_output, spec_digest,
)
from foreman.stages import STAGES, Acceptance, InvalidSlicePlan, clean_slices, oversized_pr
from foreman.spec_check import SpecHistory, accept as accept_spec_check
from foreman.state import ReviewState, ChecksState, RunState, SCHEMA_VERSION, checkpoint_serializer, DeliveryState, DeliveryUpdate, DeliveryResult
from foreman.readiness import MergeReceipt, MergeState
from foreman.outcomes import Halt, READINESS_HALTS, halted
from foreman.review_round import ReviewRound, history
from foreman.usd import BudgetUsd, usd_text
from foreman.check_output import CheckOutput
from foreman.worker import AttemptJournal
from foreman.workspace import WorkState, ImplementationRejected, WorkspaceError, CheckLog, ReviewScope, Workspace, WorktreeInspectionFailed, head_mismatch_detail

GRAPH_NAME = "delivery"
DEFAULT_TOKEN_CAP = 20_000_000
PACKAGE = Path(__file__).parent
MODEL_STAGES = tuple(name for name, stage in STAGES.items() if stage.kind == "model" and name != "spec_check")
REQUIRED_NODES = (*MODEL_STAGES, "verify")
COAUTHOR_EMAILS = {"gpt": "noreply@openai.com", "claude": "noreply@anthropic.com", "grok": "noreply@x.ai"}
CHECK_START_FAILURES = {126: "command is not executable", 127: "command not found"}


class HumanPrompt(Contract):
    stage: Literal["triage", "spec", "spec_check", "pr_size", "review"]
    question: str
    revision: str | None = None

    @property
    def answer_marker(self) -> str:
        assert self.revision is not None
        return "foreman answer " + "-".join(self.revision[i:i + 8] for i in range(0, len(self.revision), 8))


@dataclass(frozen=True)
class EnvironmentProbe:
    workspace: Workspace
    command: str
    log: CheckLog | None = None

    @cached_property
    def error(self) -> str | None:
        return failed_preflight(self.workspace, self.command, log=self.log)


@dataclass(frozen=True)
class LiveRun:
    forge: Forge
    workspace: Workspace
    check_log: CheckLog | None = None
    environment: EnvironmentProbe | None = None
    skills: SkillSource = export_skills
    human_login: str = field(default_factory=lambda: os.environ.get("FOREMAN_HUMAN_LOGIN", ""))
    journal: AttemptJournal | None = None


def spec_target(run: RunState) -> SpecTarget:
    assert run.issue is not None
    return {"repo": run.repo.lower(), "issue": run.issue, "base": run.base_sha}


def spec_cleared(plan: PlanState) -> bool:
    return plan.spec_candidate is not None and SpecHistory.model_validate(plan.spec_history).clears(plan.spec_candidate["revision"])


def spec_effect(state: DeliveryState, run: LiveRun) -> DeliveryUpdate:
    planning = state["plan"]
    raw = planning.spec_candidate
    target = state["run"]
    if raw is None or raw.get("target") != spec_target(target) or not spec_cleared(planning):
        return halted(Halt.invalid_output, "The saved checker verdict does not clear the current repository-bound plan.", None)
    if run.human_login and target.issue is not None:
        revision = raw["revision"]
        for ruling in human_comments(run, target):
            if ruling == f"foreman stop {revision}":
                return {"stop": "cancelled"}
            if ruling in {f"foreman approve {revision}", f"we're good {revision}"}:
                planning = planning.model_copy(update={"human_approval": revision})
    return {"plan": planning}


def human_comments(run: LiveRun, target: RunState) -> list[str]:
    if not run.human_login or target.issue is None:
        return []
    return [(comment.body or "").strip() for comment in run.forge.issue_comment_records(target.repo, target.issue)
            if comment.user is not None and comment.user.login.casefold() == run.human_login.casefold()]


def run_tokens(attempts: list[AttemptRecord]) -> int:
    return sum(usage["input"] + usage["output"] + (usage["cache_read"] or 0) + (usage["cache_creation"] or 0)
               for attempt in attempts if (usage := attempt["usage"]) is not None)


class PullRequestSizeDecision(StrEnum):
    SPLIT = "split"
    OVERRIDE = "override"


_DELIVERY_STATE = TypeAdapter(DeliveryResult)


def review_count(state: DeliveryState | DeliveryResult) -> int:
    return sum(round.severity is not None for round in history({"rounds": state["review"].review_findings}).rounds)


def read_delivery_state(raw: object) -> DeliveryResult:
    return _DELIVERY_STATE.validate_python(raw, strict=True)


def _active_slice(state: DeliveryState) -> SliceRecord:
    slice = state["plan"].slice
    if slice is None:
        raise InvalidOutput("implementation requires an active slice")
    return slice


def _last_stage(state: DeliveryState) -> str:
    stage = state["path"][-1]
    return "spec" if stage == "spec_check" else stage


def stage_text(name: str) -> str:
    prompt = STAGES[name].prompt
    assert prompt is not None
    return (PACKAGE / "stages" / prompt).read_text().strip()


def load_slice(raw: object, plan: PlanState) -> DeliveryUpdate:
    stored = parse_output(StoredSlice, raw)
    return {"plan": plan.model_copy(update={"slice": stored.record(), "work_kind": stored.work_kind})}


def stage_update(
    name: str, output: StageOutput, state: DeliveryState, model: str | None = None, *, workspace: Workspace | None = None, accepted_model: ModelSpec | None = None, config: Config | None = None, attempt: AttemptRecord | None = None,
) -> DeliveryUpdate:
    update = STAGES[name].accept(output, Acceptance(state, accepted_model or model, workspace, config, attempt))
    update["path"] = [name]
    if model:
        used = state.get("models_used", {})
        update["models_used"] = {**used, name: model}
    return update


def review_submission(output: ReviewRound, scope: ReviewScope, question: str) -> tuple[str, list[ReviewComment]]:
    body = [f"Review route: {output.severity or 'none'}. Head: {output.reviewed_head}. Base ref: {output.base}."]
    if question:
        body.append(f"Question for the human: {question}")
    provenance = output.provenance
    if provenance is not None:
        reason = provenance.reason.replace("_", " ") if provenance.reason else "not needed"
        body.append(f"Reviewer {provenance.reviewer}; implemented by {', '.join(provenance.contributors)}; same-family fallback: {reason}.")
    comments: list[ReviewComment] = []
    for finding in sorted(output.findings, key=lambda item: item.severity):
        location = finding.location
        path, line = location["path"], location["line"]
        label = f"Advisory, settled by {finding.ruling_id}" if finding.ruling_id else finding.severity
        message = f"[{label}] {finding.summary}\n\nImpact: {finding.impact}\n\nEvidence: {finding.evidence}"
        if location["side"] == "head" and line is not None and (path, line) in scope.inline:
            comments.append({"path": path, "line": line, "side": "RIGHT", "body": message})
        else:
            citation = f"{path}:{line}" if line is not None else path
            body.append(f"{location['side']} {citation} {message}")
    return "\n\n".join(body), comments


def stop_reason(state: DeliveryState) -> Halt | Literal[
    "sliced", "pr_split_required", "needs_human", "answered", "verified", "stopped", "cancelled"
]:
    review_state = state["review"]
    planning = state["plan"]
    checks = state["checks"]
    if halt := state.get("halt"):
        return halt
    if state.get("stop") == "cancelled":
        return "cancelled"
    if oversized_pr(state):
        split = planning.pr_split
        gate = split["gate"] if split else planning.spec_gate
        if gate == "ready":
            return "sliced" if planning.issue_kind == "parent" else "pr_split_required"
        if gate == "needs_human":
            return "needs_human"
    if planning.triage == "needs_human" or planning.spec_gate == "needs_human":
        return "needs_human"
    if state.get("path", [])[-1:] == ["spec_check"] and spec_cleared(planning):
        return "sliced"
    if state.get("path", [])[-1:] == ["research"]:
        return "answered"
    if checks.verify == "fail":
        return Halt.verify_cap
    if review_state.review == "changes":
        return Halt.review_cap
    if checks.verify == "pass":
        return "verified"
    return "stopped"


def failed_preflight(workspace: Workspace, command: str, *, log: CheckLog | None = None) -> str | None:
    if command and (exit_code := workspace.run(command, log=log)):
        return f"Preflight {command!r} exited with {exit_code}"
    return None


class DeliveryNode(Protocol):
    def __call__(self, state: DeliveryState, runtime: Runtime[LiveRun]) -> DeliveryUpdate: ...


def compile_delivery(
    dispatcher: Handler,
    config: Config,
    *,
    run: LiveRun,
    scratch: Path | None = None,
    checkpointer: BaseCheckpointSaver[str] | None = None,
) -> CompiledStateGraph[DeliveryState, LiveRun, DeliveryState, DeliveryResult]:
    missing = [name for name in REQUIRED_NODES if name not in config.nodes]
    if missing:
        raise ValueError(f"delivery config is missing {', '.join(missing)}")
    methods_root = run.skills(config.pstack, scratch) if scratch is not None else None
    prompts = {name: stage_text(name) for name in (*MODEL_STAGES, "spec_check")}
    plan_workspace = run.workspace

    dispatch = compile_dispatch()

    def planning_guard(state: DeliveryState, stage: str) -> DeliveryUpdate:
        try:
            return spec_effect(state, run)
        except ForgeError as exc:
            return halted(Halt.git_failed, str(exc), stage)

    def reject_spec(state: DeliveryState, revision: str, detail: str, reason: SpecRejection | None = None) -> DeliveryUpdate:
        plan = state["plan"]
        repeated = revision in plan.rejected_revisions
        rejected = plan.rejected_revisions if repeated else [*plan.rejected_revisions, revision]
        planning = plan.model_copy(update={"rejected_revisions": rejected, "spec_rejection": reason})
        update: DeliveryUpdate = {"plan": planning, "human_question": ""}
        if planning.spec_failures >= config.max_review_rounds:
            update.update(halted(Halt.spec_cap, detail, "spec"))
        elif repeated:
            update.update(halted(Halt.invalid_output, f"Revision repeats a rejected plan. {detail}", "spec"))
        return update

    def call(name: str, state: DeliveryState, *, edits: bool = False) -> DeliveryUpdate:
        review_state = state["review"]
        state = {**state, "review": review_state.model_copy(update={"review_run_id": review_state.review_run_id or uuid4().hex})}

        def prepare_plan() -> DispatchPlanRecord:
            models = STAGES[name].models(name, state, config)
            suffix, target, scope = STAGES[name].prepare(state, run.workspace, run.forge)
            methods_text, methods, read_roots = stage_methods(name, state, methods_root) if methods_root is not None else ("", [], [])
            instruction = prompts[name]
            if methods_text:
                instruction += f"\n\nApply the methods listed below. Do not start subagents.\n\n{methods_text}"
            return DispatchPlan(models=[model.name for model in models], instruction=instruction + suffix,
                                methods=methods, read_roots=read_roots, target=target,
                                scope=scope.record() if scope else None).record()

        # The nested graph inherits the parent checkpointer and the runner's synchronous durability.
        result = dispatch.invoke({"caller": state, "stage": name},
                                 context=DispatchRun(dispatcher, run.workspace, run.journal, config.pstack.commit, edits, prepare_plan))
        # The nested graph owns the paid attempts. The stage applies a result once.
        attempts = result.get("attempts", [])
        stopped: DeliveryUpdate = {"path": [name], "attempts": attempts}
        if "halt" in result:
            return {**stopped, **halted(result["halt"], result.get("detail", ""), name)}
        assert "output" in result and "plan" in result
        plan = result["plan"]
        model = attempts[-1]["model"]
        state = {**state, "dispatch_plan": plan}
        try:
            contract = STAGES[name].output
            assert contract is not None
            _, citation_context = STAGES[name].project(state, run.workspace)
            output = parse_output(contract, result["output"], context=citation_context)
            update = stage_update(name, output, state, model, workspace=plan_workspace,
                                  accepted_model=ModelSpec.parse(model) if model else None, config=config, attempt=attempts[-1])
            if isinstance(output, SpecCheckOutput):
                planning = state["plan"]
                candidate = SpecCandidate.from_record(planning.spec_candidate)
                if not output.human_question:
                    past = SpecHistory.model_validate(planning.spec_history)
                    checked = accept_spec_check(past, output, candidate)
                    planning = planning.model_copy(update={"spec_history": {
                        "rounds": checked.history.rounds, "settled": checked.history.settled,
                    }})
                    if not checked.approval:
                        detail = "\n".join(f.summary for f in checked.history.accounting().unresolved())
                        update.update(reject_spec({**state, "plan": planning}, candidate.revision, detail))
                    else:
                        update.update(plan=planning, human_question="")
        except InvalidSlicePlan as exc:
            revision = spec_digest([spec_target(state["run"]), result["output"]])
            return {**stopped, **reject_spec(state, revision, str(exc), exc.reason)}
        except InvalidOutput as exc:
            return {**stopped, **halted(Halt.invalid_output, f"{name} output from {model}: {exc}", name)}
        except WorkspaceError as exc:
            return {**stopped, **halted(Halt.git_failed, str(exc), name)}
        return {"review": state["review"], **update, "dispatch_plan": plan,
                "attempts": [*attempts[:-1], *update.get("attempts", attempts[-1:])]}

    def prerequisite_seams(item: StoredSlice, target: SpecTarget) -> set[str]:
        seams: set[str] = set()
        repo, base = target["repo"], target["base"]
        if len(item.depends_on) != len(item.blocker_numbers):
            raise InvalidSlicePlan("dangling_seam", "The filed slice does not match its planned blockers.")
        for title, number in zip(item.depends_on, item.blocker_numbers):
            if not run.forge.is_merged(repo, number, contains=lambda commit: plan_workspace.contains_commit(base, commit)):
                raise InvalidSlicePlan("dangling_seam", f"Blocker #{number} has no merge in the pinned tree.")
            issue = run.forge.load_issue(repo, number)
            if issue["repo"].lower() != repo or issue["number"] != number or "slice" not in issue["labels"]:
                raise InvalidSlicePlan("dangling_seam", f"Blocker #{number} is not a slice in the target repository.")
            blocker = load_stored_slice(issue["body"])
            if blocker.title != title or (blocker.repository and blocker.repository.lower() != repo):
                raise InvalidSlicePlan("dangling_seam", f"Blocker #{number} does not match the planned slice {title}.")
            seams.update(seam.name for seam in blocker.builds)
        return seams

    def spec(state: DeliveryState) -> DeliveryUpdate:
        plan = state["plan"]
        if plan.spec_candidate is not None and state["path"][-1:] == ["spec_check"] and not spec_cleared(plan):
            stage = "spec" if plan.spec_candidate["revision"] in plan.rejected_revisions else "spec_check"
            return call(stage, state)
        size = oversized_pr(state)
        supplied = plan.slice is not None and not plan.spec_gate and not size
        pending = [plan.slice] if supplied and plan.slice is not None else plan.slices
        if not pending or plan.spec_rejection or (size and plan.pr_split is None) or plan.spec_gate == "needs_human":
            return call("spec", state)
        target = spec_target(state["run"])
        if any((repo := item.get("repository")) and repo.lower() != target["repo"] for item in pending):
            return halted(Halt.invalid_output, "foreign_repository: slice scope differs from the admitted repository.", "spec")
        split = plan.pr_split if size else None
        scope = split["plan"] if split else plan.spec
        try:
            items = [parse_output(StoredSlice, item) for item in pending]
            filed = supplied and plan.issue_kind == "slice"
            seams = prerequisite_seams(items[0], target) if filed else ()
            slices = clean_slices(items, plan_workspace, target["base"], prerequisite_seams=seams)
            if filed:
                slices = pending
        except InvalidSlicePlan as exc:
            revision = spec_digest([target, scope, plan.work_kind, pending])
            return reject_spec(state, revision, str(exc), exc.reason)
        except InvalidOutput as exc:
            return halted(Halt.invalid_output, str(exc), "spec")
        except (ForgeError, WorkspaceError) as exc:
            return halted(Halt.git_failed, str(exc), "spec")
        stage = "triage" if supplied else "spec"
        author_name = state.get("models_used", {}).get(stage)
        provenance = pending[0].get("provenance") if supplied else None
        author = SpecAuthor.model_validate(provenance["author"]) if provenance else SpecAuthor(
            stage=stage, model=author_name or "github", family=family(author_name) if author_name else "github")
        kind: WorkKind = plan.work_kind or "feature"
        candidate = SpecCandidate(slices=[CandidateSlice.model_validate({k: v for k, v in item.items() if k != "provenance"}) for item in slices],
            spec=scope, work_kind=kind, author=author, target=target)
        single = plan.slice is not None and len(slices) == 1 and not size
        planning = candidate.update(plan if single or size else plan.model_copy(update={"slice": None}), single=single)
        if split:
            planning = planning.model_copy(update={"spec": plan.spec, "pr_split": {**split, "slices": planning.slices}})
        planning = planning.model_copy(update={"spec_gate": "ready"})
        if candidate.revision in plan.rejected_revisions:
            return reject_spec(state, candidate.revision, "The checker already rejected this scope.")
        return {"plan": planning, **call("spec_check", {**state, "plan": planning})}

    def post_review(state: DeliveryState) -> DeliveryUpdate:
        review_state = state["review"]
        assert "dispatch_plan" in state and review_state.review_run_id
        plan = state["dispatch_plan"]
        assert plan["target"] is not None and plan["scope"] is not None
        past = history({"rounds": review_state.review_findings, "settled": review_state.settled_rulings})
        report = past.rounds[-1]
        target = PullRequest.model_validate(plan["target"])
        if report.reviewed_head != state["checks"].verified_head or (
            report.reviewed_head, report.base
        ) != (target.headRefOid, target.baseRefOid):
            return halted(Halt.review_not_posted, "Review does not match the checked PR head and base.", "review_post")
        body, comments = review_submission(report, TypeAdapter(ReviewScope).validate_python(plan["scope"]),
                                           state.get("human_question", ""))
        marker = f"{review_state.review_run_id}:{len(past.rounds)}:{report.base}:{report.reviewed_head}"
        try:
            receipt = run.forge.post_review(state["run"].repo, target, body, comments, marker=marker)
        except ForgeError as exc:
            return halted(Halt.review_not_posted, str(exc), "review_post")
        if receipt["state"] != "COMMENTED" or receipt["commit_id"] != report.reviewed_head:
            return halted(Halt.review_not_posted, "The review receipt does not match the submitted COMMENT.", "review_post")
        return {"review": review_state.model_copy(update={"review_receipt": receipt})}

    def prepare(state: DeliveryState, runtime: Runtime[LiveRun]) -> DeliveryUpdate:
        clearance = planning_guard(state, "prepare")
        if clearance.get("halt") or clearance.get("stop"):
            return clearance
        active = runtime.context or run
        probe = active.environment or EnvironmentProbe(active.workspace, state["checks"].preflight, active.check_log)
        try:
            if not active.workspace.inspect().clean:
                return halted(Halt.dirty_worktree, "Unowned edits exist before this attempt.", "prepare")
            if detail := probe.error:
                return halted(Halt.env_unavailable, detail, "prepare")
        except WorkspaceError as exc:
            return halted(Halt.git_failed, str(exc), "prepare")
        title = redact(str(_active_slice(state)["title"]))
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60].rstrip("-") or "change"
        work = state.get("work", WorkState())
        run_state = state["run"]
        branch = work.branch or f"{run_state.issue or '<issue>'}-{slug}"
        try:
            base = run_state.base_branch
            run.workspace.create_branch(branch, base, pinned=run_state.base_sha)
            if repair_head := work.repair_head:
                run.workspace.refresh(branch, repair_head)
            ready = run.workspace.inspect()
            if not work.branch and not repair_head and ready.head != run_state.base_sha:
                raise WorktreeInspectionFailed("The new build branch must start at the pinned base.")
            checks = state["checks"]
            return {**clearance, "work": WorkState(branch=branch, base=base, attempt_head=ready.head),
                    "checks": checks.for_attempt()}
        except (WorkspaceError, ForgeError) as exc:
            return halted(Halt.git_failed, str(exc), "prepare")

    def implement(state: DeliveryState) -> DeliveryUpdate:
        clearance = planning_guard(state, "implement")
        if clearance.get("halt") or clearance.get("stop"):
            return clearance
        return {**clearance, **call("implement", {**state, **clearance}, edits=True)}

    def reconcile(state: DeliveryState) -> DeliveryUpdate:
        review_state = state["review"]
        assert "work" in state and state["work"].attempt_head and state["work"].branch and state["work"].base
        try:
            candidate = run.workspace.reconcile(state["work"].attempt_head, state["work"].branch,
                                                state["work"].base)
            if candidate is None:
                identifiers = [str(f.get("id")) for f in review_state.findings]
                detail = "no changes" + ("; unresolved findings: " + ", ".join(identifiers) if identifiers else "")
                return halted(Halt.failed, detail, "reconcile")
            return {"work": state["work"].model_copy(update={"candidate": candidate})}
        except ImplementationRejected as exc:
            return halted(Halt.implementation_rejected, str(exc), "reconcile")
        except WorkspaceError as exc:
            return halted(Halt.git_failed, str(exc), "reconcile")

    def commit(state: DeliveryState) -> DeliveryUpdate:
        subject = f"{state['run'].issue or '<issue>'}: {' '.join(redact(str(_active_slice(state)['title'])).split())}"
        subject = textwrap.shorten(subject, width=72, placeholder="")
        model = state.get("models_used", {}).get("implement")
        if not model:
            return halted(Halt.publication_model_missing, "Publication needs the implement model.", None)
        message = f"{subject}\n\nCo-Authored-By: {redact(model)} <{COAUTHOR_EMAILS[family(model)]}>"
        assert "work" in state and state["work"].branch and state["work"].attempt_head
        try:
            observed = run.workspace.inspect()
            if observed.branch != state["work"].branch:
                raise WorkspaceError("Restore the delivery branch before committing.")
            candidate = state["work"].candidate
            assert candidate is not None
            if candidate["disposition"] == "adopted" and observed.head == candidate["head"]:
                if not observed.clean:
                    raise WorkspaceError("HEAD differs from the reconciled worker commit.")
                run.workspace.soft_reset(state["work"].attempt_head)
                observed = run.workspace.inspect()
            if observed.head != state["work"].attempt_head:
                if not observed.clean or not run.workspace.committed(state["work"].attempt_head, message):
                    raise WorkspaceError("HEAD differs from the expected implementation commit.")
                if candidate["disposition"] == "adopted" and run.workspace.tree(observed.head) != candidate["tree"]:
                    raise WorkspaceError("Commit differs from the reconciled worker tree.")
            else:
                if observed.clean:
                    raise WorkspaceError("The checkpointed implementation edits are missing.")
                if run.workspace.tree(None) != candidate["size"]["head"]:
                    raise WorkspaceError("Implementation edits changed after reconciliation and size measurement.")
                if run.workspace.fingerprint() != candidate["fingerprint"]:
                    raise WorkspaceError("Implementation edits changed after reconciliation.")
                run.workspace.commit(message)
                observed = run.workspace.inspect()
                if not observed.clean:
                    raise WorkspaceError("The commit left uncommitted edits; restore the implementation tree.")
            if run.workspace.tree(observed.head) != candidate["size"]["head"]:
                raise WorkspaceError("Commit differs from the measured implementation tree.")
            return {"work": state["work"].model_copy(update={"head_sha": observed.head})}
        except WorkspaceError as exc:
            return halted(Halt.git_failed, str(exc), "commit")

    def publish(state: DeliveryState) -> DeliveryUpdate:
        assert "work" in state and state["work"].branch and state["work"].base
        run_state = state["run"]
        branch, base, repo = state["work"].branch, state["work"].base, run_state.repo
        title = " ".join(redact(str(_active_slice(state)["title"])).split())
        body = f"Closes #{run_state.issue or '<issue>'}"
        try:
            observed = run.workspace.inspect()
            if not observed.clean or observed.branch != branch or observed.head != state["checks"].verified_head:
                raise WorkspaceError("Restore the clean delivery branch at its verified commit before publishing.")
            run.workspace.push(branch, repo)
            run.forge.open_pull_request(repo, branch, base, title, body)
        except (WorkspaceError, ForgeError) as exc:
            return halted(Halt.publish_failed, str(exc), "publish")
        return {"path": ["publish"]}

    def verify(state: DeliveryState, runtime: Runtime[LiveRun]) -> DeliveryUpdate:
        assert "work" in state
        current = runtime.context or run
        workspace, check_log = current.workspace, current.check_log
        try:
            worktree = workspace.inspect()
        except WorktreeInspectionFailed as exc:
            return halted(Halt.dirty_worktree, str(exc), "verify")
        if not worktree.clean or worktree.head != state["work"].head_sha:
            return halted(Halt.dirty_worktree, "Restore the clean implementation commit before verification.", "verify")
        checks = state["checks"]
        command = checks.check
        output = CheckOutput(check_log)
        exit_code = workspace.run(command, log=output)
        detail = None
        if reason := CHECK_START_FAILURES.get(exit_code):
            detail = f"Check {command!r} exited with {exit_code} ({reason})"
        elif exit_code:
            detail = failed_preflight(workspace, checks.preflight, log=check_log)
        checked = checks.model_copy(update={"check_exit_code": exit_code, "failure_output": output.text if exit_code else "",
            "verify": "" if detail else "pass" if exit_code == 0 else "fail", "verified_head": worktree.head if exit_code == 0 else ""})
        if detail:
            return {**halted(Halt.env_unavailable, detail, "verify"), "checks": checked}
        update: DeliveryUpdate = {"path": ["verify"], "checks": checked}
        if exit_code:
            update["checks"] = update["checks"].model_copy(update={"verify_failures": checks.verify_failures + 1})
        return update

    def needs_human(state: DeliveryState) -> DeliveryUpdate:
        review_state = state["review"]
        stage = state["path"][-1]
        split = state["plan"].pr_split if stage == "spec" and oversized_pr(state) else None
        question = (split["question"] if split else state.get("human_question")) or "What should Foreman know to continue?"
        planning = state["plan"]
        revision = None
        if stage == "spec":
            scope = split["plan"] if split else planning.spec
            revision = spec_digest([stage, spec_target(state["run"]), scope, planning.work_kind,
                                    planning.slice, planning.slices, question])
        elif stage == "spec_check":
            assert planning.spec_candidate is not None
            revision = spec_digest([stage, planning.spec_candidate["revision"], question])
        answer = interrupt(HumanPrompt.model_validate({"stage": stage, "question": question, "revision": revision}).model_dump(exclude_none=True))
        if revision and answer == "cancel":
            return {"stop": "cancelled"}
        update: DeliveryUpdate = {"human_answers": [*state.get("human_answers", []), answer]}
        candidate = planning.spec_candidate
        update["plan"] = planning.model_copy(update={"human_qa": [
            *planning.human_qa, {"stage": stage, "question": question, "answer": answer,
                                 **({"revision": revision} if revision else {}),
                                 **({"candidate": candidate["revision"]} if revision and candidate else {})},
        ]})
        if revision:
            update["human_question"] = ""
        if stage == "review":
            past = history({"rounds": review_state.review_findings, "settled": review_state.settled_rulings})
            update.update(halt=None, halt_detail="", human_question="", review=review_state.model_copy(update={"settled_rulings": [r.model_dump() for r in past.settled] +
                          [{"id": f"design:{len(past.rounds)}", "text": answer}]}))
        return update

    def decide_pr_size(state: DeliveryState) -> DeliveryUpdate:
        planning = state["plan"]
        size = oversized_pr(state)
        assert size is not None
        split = planning.pr_split
        plan = split["plan"] if split else planning.spec
        answer = PullRequestSizeDecision(interrupt(HumanPrompt(
            stage="pr_size",
            question=(
                f"The pull request has {size['lines']} changed lines. The limit is {PULL_REQUEST_LINES}.\n"
                f"{plan}\n"
                "Use --answer split to accept the split or --answer override to review this diff."
            ),
        ).model_dump()))
        if answer == PullRequestSizeDecision.OVERRIDE:
            original = _active_slice(state).get("provenance")
            assert original is not None
            history = SpecHistory.model_validate(planning.spec_history)
            checked = next(entry for entry in reversed(history.rounds) if entry.candidate["revision"] == original["revision"])
            candidate = SpecCandidate.from_record(checked.candidate)
            restored = candidate.update(planning, single=True).model_copy(update={"pr_split": None})
            return {"pr_size_override": size, "plan": restored}
        return {}

    def size(state: DeliveryState) -> DeliveryUpdate:
        assert "work" in state
        candidate = state["work"].candidate
        assert candidate is not None
        return {"pr_size": candidate["size"]}

    def review(state: DeliveryState) -> DeliveryUpdate:
        try:
            head = state["checks"].verified_head
            if not head:
                raise WorktreeInspectionFailed("The run did not record a verified HEAD.")
            worktree = run.workspace.inspect()
            if worktree.head != head:
                return {**halted(Halt.merge_not_armed, head_mismatch_detail(worktree.head, head), "review"),
                        "merge": MergeState(status="not_armed")}
            if not worktree.clean or worktree.branch is None:
                raise WorktreeInspectionFailed("The worktree changed after verification.")
        except WorktreeInspectionFailed as exc:
            return halted(Halt.git_failed, str(exc), "review")
        return call("review", state)

    def after_triage(state: DeliveryState) -> str:
        planning = state["plan"]
        if state.get("halt"):
            return "done"
        decision = planning.triage
        if decision == "needs_human":
            return "needs_human"
        if planning.slice is not None or decision in {"implement", "spec"}:
            return "spec"
        return "research" if decision == "question" else "done"

    def after_spec(state: DeliveryState) -> str:
        planning = state["plan"]
        if state.get("halt") or state.get("stop") == "cancelled":
            return "done"
        if planning.spec_rejection:
            return "spec"
        if state.get("human_question"):
            return "needs_human"
        if not spec_cleared(planning):
            return "spec"
        if oversized_pr(state):
            return "decide_pr_size"
        return "prepare" if planning.slice is not None else "done"

    def after_review(state: DeliveryState) -> str:
        review_state = state["review"]
        if state.get("halt"):
            return "done"
        if review_state.review == "changes" and review_count(state) >= config.max_review_rounds:
            return "done"
        if state.get("human_question"):
            return "needs_human"
        return "prepare" if review_state.review == "changes" else "merge"

    def after_verify(state: DeliveryState) -> str:
        if state.get("halt"):
            return "done"
        checks = state["checks"]
        if checks.verify == "pass":
            return "publish"
        if checks.verify_failures < config.max_review_rounds:
            return "prepare"
        return "done"

    def arm_merge(state: DeliveryState) -> DeliveryUpdate:
        review_state = state["review"]
        run_state = state["run"]
        repo = run_state.repo
        head = state["checks"].verified_head
        merge = MergeState(
            status="not_armed", receipt=state.get("merge", MergeState()).receipt,
            command=merge_command(repo, "<number>", head or "<sha>"),
        )

        def merge_halted(halt: Halt, detail: str) -> DeliveryUpdate:
            return {
                **halted(halt, detail, "merge"),
                "merge": merge,
            }

        forge, workspace = run.forge, run.workspace
        if not head:
            return merge_halted(Halt.merge_not_armed, "The run did not record a verified HEAD.")
        try:
            worktree = workspace.inspect()
        except WorktreeInspectionFailed as exc:
            return merge_halted(Halt.merge_not_armed, str(exc))
        if not worktree.clean:
            return merge_halted(Halt.merge_not_armed, "The worktree has changes or Git could not inspect it.")
        if worktree.head != head:
            merge = merge.model_copy(update={"observed_head": worktree.head})
            return merge_halted(Halt.merge_not_armed, head_mismatch_detail(worktree.head, head))
        if worktree.branch is None:
            return merge_halted(Halt.merge_not_armed, "HEAD is detached. Merge requires a checked-out branch.")
        try:
            hold = forge.merge_hold(repo)
        except MergeHoldUnreadable as exc:
            return merge_halted(Halt.merge_held, f"could not read {MERGE_HOLD_VARIABLE}: {exc}")
        if hold:
            return merge_halted(Halt.merge_held, f"{MERGE_HOLD_VARIABLE} is set" + (f": {hold}" if hold.strip() else "."))
        issue = run_state.issue
        if issue is None:
            return merge_halted(Halt.merge_not_armed, "Live merge requires an issue number.")
        receipt = review_state.review_receipt
        rounds = history({"rounds": review_state.review_findings}).rounds
        report = rounds[-1] if rounds else None
        plan = state.get("dispatch_plan")
        target = plan["target"] if plan else None
        if (report is None or target is None or report.severity or state.get("human_question")
                or report.reviewed_head != head or target["headRefOid"] != head or report.base != target["baseRefOid"]
                or not receipt or receipt["state"] != "COMMENTED" or receipt["commit_id"] != head):
            return merge_halted(Halt.merge_not_armed, "Renew review and its posted receipt at the verified head.")
        if previous := merge.receipt:
            saved = MergeReceipt.model_validate(previous)
            if saved.source_head == head:
                if (saved.repo, saved.pr, saved.base, saved.base_ref) != (repo, target["number"], report.base, target["baseRefName"]):
                    merge = merge.model_copy(update={"receipt": None})
                    return merge_halted(Halt.merge_not_armed, "The saved handoff does not match the reviewed pull request and base.")
                try:
                    confirmed_receipt = forge.reconcile_merge(saved)
                except HandoffChanged as exc:
                    merge = merge.model_copy(update={"receipt": None})
                    return merge_halted(Halt.merge_not_armed, str(exc))
                except QueueRejected as exc:
                    return merge_halted(READINESS_HALTS[exc.outcome], str(exc))
                except MergeNotArmed as exc:
                    return merge_halted(Halt.merge_not_armed, str(exc))
                return {"merge": merge.model_copy(update={"status": "armed", "receipt": confirmed_receipt.model_dump(mode="json")}), "path": ["merge"]}
        try:
            observation = forge.readiness(repo, issue, worktree.branch, head)
        except ForgeError as exc:
            return merge_halted(Halt.readiness_unavailable, f"Platform/policy observation unavailable: {exc}")
        if observation.outcome != "ready":
            merge = merge.model_copy(update={"observation": observation.model_dump(mode="json")})
            return merge_halted(READINESS_HALTS[observation.outcome], observation.detail())
        if (observation.pr.number, observation.pr.head.sha, observation.pr.base.sha, observation.pr.base.ref) != (
            target["number"], head, report.base, target["baseRefName"],
        ):
            return merge_halted(Halt.merge_not_armed, "The pull request head, base or identity changed after review.")
        try:
            receipt = forge.arm_auto_merge(repo, issue, worktree.branch, head, observation)
            if receipt is None:
                return {"merge": merge.model_copy(update={"status": "skipped", "command": merge_command(repo, "<number>", head)}), "path": ["merge"]}
            merge_receipt = receipt.model_dump(mode="json")
        except QueueRejected as exc:
            return merge_halted(READINESS_HALTS[exc.outcome], str(exc))
        except MergeNotArmed as exc:
            return merge_halted(Halt.merge_not_armed, str(exc))
        if merge_receipt["status"] == "MERGED":
            return {"merge": merge.model_copy(update={"status": "armed", "receipt": merge_receipt}), "path": ["merge"]}
        try:
            confirmed = forge.readiness(repo, issue, worktree.branch, head)
        except ForgeError as exc:
            return {**merge_halted(Halt.readiness_unavailable, str(exc)), "merge": merge.model_copy(update={"status": "armed", "receipt": merge_receipt})}
        if (confirmed.pr.number, confirmed.pr.head.sha, confirmed.pr.base.sha, confirmed.pr.base.ref) != (
            observation.pr.number, observation.pr.head.sha, observation.pr.base.sha, observation.pr.base.ref,
        ):
            merge = merge.model_copy(update={"observation": confirmed.model_dump(mode="json"), "receipt": None})
            return merge_halted(Halt.merge_not_armed, "The pull request head, base or identity changed after arming.")
        update: DeliveryUpdate = {"merge": merge.model_copy(update={
            "status": "armed", "receipt": merge_receipt, "observation": confirmed.model_dump(mode="json"),
        }), "path": ["merge"]}
        if confirmed.outcome != "ready":
            update.update(halted(READINESS_HALTS[confirmed.outcome], confirmed.detail(), "merge"))
        elif confirmed.pr.state != observation.pr.state:
            update.update(halted(Halt.merge_not_armed, confirmed.detail(), "merge"))
        return update

    def finish(state: DeliveryState) -> DeliveryUpdate:
        review_state = state["review"]
        planning = state["plan"]
        checks = state["checks"]
        reason = stop_reason(state)
        if not state.get("halt"):
            rounds = checks.verify_failures if reason == Halt.verify_cap else review_count(state)
            if reason == Halt.verify_cap:
                detail = f"Verification failed after {rounds} rounds. Check {checks.check or '(none)'} failed."
                return {**halted(Halt.verify_cap, detail, "done"), "stop": str(reason)}
            if reason == Halt.review_cap:
                detail = f"Review requested changes after {rounds} rounds. Last review: {review_state.detail}"
                return {**halted(Halt.review_cap, detail, "done"), "stop": str(reason)}
            if reason == "needs_human":
                return {"stop": reason, "path": ["done"], "halt_detail":
                        state.get("human_question") or "What should Foreman know to continue?"}
        update: DeliveryUpdate = {"stop": str(reason), "path": ["done"]}
        split = planning.pr_split
        if reason == "pr_split_required":
            update["halt_detail"] = split["plan"] if split else planning.spec
        return update

    builder = StateGraph[DeliveryState, LiveRun, DeliveryState, DeliveryResult](
        DeliveryState, input_schema=DeliveryState, output_schema=DeliveryResult, context_schema=LiveRun,
    )

    def after_halt(target: str) -> Callable[[DeliveryState], str]:
        def route(state: DeliveryState) -> str:
            return "done" if state.get("halt") or state.get("stop") == "cancelled" else target
        return route

    def after_size(state: DeliveryState) -> str:
        if oversized_pr(state):
            return "decide_pr_size" if state.get("pr_size_override") else "too big: re-plan"
        return "fits"

    builder.add_node("triage", lambda state: call("triage", state))
    builder.add_node("research", lambda state: call("research", state))
    builder.add_node("spec", spec)
    builder.add_node("implement", implement)
    builder.add_node("review", review)
    builder.add_node("verify", verify)
    builder.add_node("prepare", prepare)
    builder.add_node("reconcile", reconcile)
    builder.add_node("size", size)
    builder.add_node("commit", commit)
    builder.add_node("publish", publish)
    builder.add_node("review_post", post_review)
    builder.add_node("needs_human", needs_human)

    builder.add_node("decide_pr_size", decide_pr_size)
    builder.add_node("merge", arm_merge)
    builder.add_node("done", finish)
    builder.add_conditional_edges(
        START, after_halt("triage"), {"done": "done", "triage": "triage"},
    )
    builder.add_conditional_edges(
        "triage",
        after_triage,
        {"spec": "spec", "research": "research",
         "needs_human": "needs_human", "done": "done"},
    )
    builder.add_conditional_edges(
        "spec", after_spec,
        {"spec": "spec", "prepare": "prepare", "needs_human": "needs_human", "decide_pr_size": "decide_pr_size", "done": "done"},
    )
    builder.add_conditional_edges(
        "decide_pr_size", lambda state: "done" if oversized_pr(state) else "size", {"done": "done", "size": "size"},
    )
    builder.add_conditional_edges(
        "needs_human", lambda state: "done" if state.get("halt") or state.get("stop") == "cancelled" else _last_stage(state),
        {"triage": "triage", "spec": "spec", "review": "review", "done": "done"},
    )
    builder.add_conditional_edges("prepare", after_halt("implement"), {"done": "done", "implement": "implement"})
    builder.add_conditional_edges("reconcile", after_halt("size"), {"done": "done", "size": "size"})
    builder.add_conditional_edges("size", after_size, {"fits": "commit", "too big: re-plan": "spec", "decide_pr_size": "decide_pr_size"})
    builder.add_conditional_edges("commit", after_halt("verify"), {"done": "done", "verify": "verify"})
    builder.add_conditional_edges("publish", after_halt("review"), {"done": "done", "review": "review"})
    builder.add_conditional_edges("research", lambda _: "done", {"done": "done"})
    builder.add_conditional_edges("implement", after_halt("reconcile"), {"reconcile": "reconcile", "done": "done"})
    builder.add_conditional_edges(
        "review",
        after_halt("review_post"),
        {"review_post": "review_post", "merge": "merge", "prepare": "prepare", "needs_human": "needs_human", "done": "done"},
    )
    builder.add_conditional_edges("review_post", after_review,
        {"merge": "merge", "prepare": "prepare", "needs_human": "needs_human", "done": "done"})
    builder.add_conditional_edges(
        "verify",
        after_verify,
        {"publish": "publish", "prepare": "prepare", "done": "done"},
    )
    builder.add_edge("merge", "done")
    builder.add_edge("done", END)
    # Each round visits at most all stages, each with one setup and every configured candidate,
    # plus prepare/commit/publish, prompts, merge and done.
    steps = sum(1 + max(1, len(config.nodes[name].models)) for name in (*REQUIRED_NODES, "spec")) + 12
    return builder.compile(checkpointer=checkpointer).with_config({"recursion_limit": steps * (config.max_review_rounds + 1)})


def run_delivery(
    dispatcher: Handler,
    *,
    repo: str,
    request: str,
    check: str = "",
    config: Config | None = None,
    slice: object = None,
    scratch: Path | None = None,
    max_tokens: int = DEFAULT_TOKEN_CAP,
    max_budget_usd: BudgetUsd | None = None,
    skills: SkillSource = export_skills,
    commands: list[list[str]] | None = None,
) -> DeliveryResult:
    dry = DryRun(commands=commands if commands is not None else [])
    state: DeliveryState = {
        "schema_version": SCHEMA_VERSION,
        "run": RunState(issue=dry.issue_placeholder, repo=repo, request=request, token_cap=max_tokens,
                        base_branch=dry.default_branch(repo), base_sha=dry.pin_base(dry.default_branch(repo)),
                        max_budget_usd=usd_text(max_budget_usd) if max_budget_usd is not None else None),
        "checks": ChecksState(check=check),
        "plan": PlanState(issue_kind="parent" if slice is None else "slice"),
        "models_used": {},
        "path": [],
        "review": ReviewState(),
    }
    if slice is not None:
        state.update(load_slice(slice, state["plan"]))
    config = config if config is not None else load_config()
    if scratch is not None:
        state["pstack_pin"] = config.pstack.model_dump()
    try:
        saver = InMemorySaver(serde=checkpoint_serializer())
        graph = compile_delivery(dispatcher, config, run=LiveRun(forge=dry, workspace=dry, skills=skills), scratch=scratch, checkpointer=saver)
    except SkillMissing as exc:
        return read_delivery_state({**state, **halted(Halt.skill_missing, str(exc), "done"), "stop": exc.halt,
                                    "skill_reason": exc.reason})
    from foreman.runner import snapshot_result

    thread_id = uuid4().hex
    graph.invoke(state, {"configurable": {"thread_id": thread_id}}, version="v2", durability="sync")
    return snapshot_result(graph.get_state({"configurable": {"thread_id": thread_id}}))


def diagram() -> str:
    dry = DryRun()
    graph = compile_delivery(ScriptHandler(), load_config(), run=LiveRun(forge=dry, workspace=dry))
    return graph.get_graph().draw_mermaid()
