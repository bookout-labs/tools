"""The delivery graph. Edges live here. Models live in models.toml."""

from decimal import Decimal
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

from foreman.adapters import Outcome
from foreman.budget import CostUnknown, Exhausted, Remaining, claude_spend, dollar_admission, work_tokens
from foreman.config import Config, ConfigError, ModelDependencyMissing, ModelSpec, family, load_config
from foreman.dry_run import DryRun, ScriptHandler
from foreman.dispatch import Handler, InvalidOutput, clean_outcome
from foreman.github import (
    MERGE_HOLD_VARIABLE, Forge, ForgeError, MergeHoldUnreadable, MergeNotArmed, HandoffChanged, QueueRejected, PullRequest, ReviewComment, merge_command,
)
from foreman.skill_rules import SkillMissing, SkillSource, export_skills, stage_methods
from foreman.redaction import redact
from foreman.stage_output import (
    PlanState, CandidateSlice, SpecAuthor, SpecCandidate, SpecTarget, SpecCheckOutput, SpecRejection, WorkKind, PendingDispatch, DispatchPlan, Attempt, AttemptRecord, AttemptUsage, Contract, SliceRecord, StageOutput, PULL_REQUEST_LINES, StoredSlice, parse_output, spec_digest,
)
from foreman.stages import STAGES, Acceptance, InvalidSlicePlan, clean_slices, oversized_pr
from foreman.spec_check import SpecHistory, accept as accept_spec_check
from foreman.state import ReviewState, ChecksState, RunState, SCHEMA_VERSION, checkpoint_serializer, DeliveryState, DeliveryUpdate, DeliveryResult
from foreman.readiness import MergeReceipt, MergeState
from foreman.outcomes import Halt, READINESS_HALTS, halted
from foreman.review_round import ReviewRound, history
from foreman.usd import BudgetUsd, usd_text
from foreman.check_output import CheckOutput
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


class QuotaPause(Contract):
    reason: Literal["quota_exhausted"] = "quota_exhausted"
    detail: str | None


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


def spec_target(run: RunState) -> SpecTarget:
    assert run.issue is not None
    return {"repo": run.repo.lower(), "issue": run.issue, "base": run.base_sha}


def spec_effect(state: DeliveryState, run: LiveRun) -> DeliveryUpdate:
    planning = state["plan"]
    raw = planning.spec_candidate
    target = state["run"]
    if raw is None or raw.get("target") != spec_target(target) or planning.spec_approval != raw["revision"]:
        return halted(Halt.invalid_output, "The current repository-bound plan has no matching checker approval.", None)
    checked = SpecHistory.model_validate(planning.spec_history)
    round_index = next((i for i in range(len(checked.rounds) - 1, -1, -1)
                        if checked.rounds[i].candidate["revision"] == raw["revision"]), None)
    if round_index is None or checked.model_copy(update={"rounds": checked.rounds[:round_index + 1]}).accounting().unresolved():
        return halted(Halt.invalid_output, "The saved checker verdict does not clear the current plan.", None)
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


def unknown_usage(stage: str, model: str | None) -> str:
    return f"{model} ran {stage} without a usage report"


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


def dollar_detail(run_state: RunState, attempts: list[AttemptRecord]) -> str:
    spend = claude_spend(attempts)
    detail = f"Run Claude spend: ${spend}." if spend is not None else "Run Claude spend: unknown."
    if (limit := run_state.max_budget_usd) is not None:
        detail += f" Dollar allowance: ${limit}."
    return detail


def dollar_admission_for(run_state: RunState, attempts: list[AttemptRecord]) -> Remaining | Exhausted | CostUnknown | None:
    limit = run_state.max_budget_usd
    return dollar_admission(BudgetUsd(Decimal(limit)), attempts) if limit is not None else None


def admission_stop(
    state: DeliveryState, attempts: list[AttemptRecord], stage: str,
    admission: Remaining | Exhausted | CostUnknown | None, work: int | None,
) -> DeliveryUpdate | None:
    run_state = state["run"]
    assert run_state.token_cap is not None
    if work is None:
        return halted(Halt.usage_unknown, "A launched attempt has unknown work usage; the work cap cannot be enforced.", stage)
    if work >= run_state.token_cap:
        last = attempts[-1]
        detail = (f"Run used {work} work tokens; cap is {run_state.token_cap}. "
                  f"Last attempt: {last['stage']} {last['model']} used {work_tokens([last])} work tokens. "
                  f"Measured work overshoot: {work - run_state.token_cap}. An in-flight attempt can overshoot the cap.")
        return halted(Halt.budget_exceeded, detail, stage)
    match admission:
        case CostUnknown():
            detail = "A Claude attempt ran without a dollar cost. The allowance cannot be enforced."
            return halted(Halt.usage_unknown, f"{detail} {dollar_detail(run_state, attempts)}", stage)
        case Exhausted():
            return halted(Halt.budget_exceeded, dollar_detail(run_state, attempts), stage)
        case Remaining() | None:
            return None


def dispatch_budget(admission: Remaining | Exhausted | CostUnknown | None) -> str | None:
    return usd_text(BudgetUsd(admission.usd)) if isinstance(admission, Remaining) else None


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
    if planning.spec_approval and state.get("path", [])[-1:] == ["spec_check"]:
        return "sliced"
    if planning.triage == "question":
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

    def implementation_guard(state: DeliveryState, active: LiveRun, stage: str) -> DeliveryUpdate | None:
        try:
            before = active.workspace.inspect()
            work = state.get("work", WorkState())
            if stage != "prepare" and (before.branch != work.branch or before.head != work.attempt_head):
                return halted(Halt.dirty_worktree, "Restore the attempt's branch and starting HEAD before resuming edits.", stage)
            partial = work.partial_edits if stage != "prepare" else ""
            if not before.clean:
                if not partial:
                    return halted(Halt.dirty_worktree, "Unowned edits exist before this attempt; save them before rerunning.", stage)
                if active.workspace.fingerprint() != partial:
                    return halted(Halt.dirty_worktree, "Edits changed after the quota stop; restore the run's partial edits.", stage)
        except WorkspaceError as exc:
            return halted(Halt.git_failed, str(exc), stage)
        probe = active.environment or EnvironmentProbe(
            active.workspace, state["checks"].preflight, active.check_log,
        )
        if detail := probe.error:
            return halted(Halt.env_unavailable, detail, stage)
        return None

    def planning_guard(state: DeliveryState, stage: str) -> DeliveryUpdate:
        try:
            return spec_effect(state, run)
        except ForgeError as exc:
            return halted(Halt.git_failed, str(exc), stage)

    def reject_spec(state: DeliveryState, revision: str, detail: str, reason: SpecRejection | None = None) -> DeliveryUpdate:
        plan = state["plan"]
        rejected = list(dict.fromkeys([*plan.rejected_revisions, revision]))
        update: DeliveryUpdate = {"plan": plan.model_copy(update={"rejected_revisions": rejected, "spec_failures": len(rejected),
            "spec_rejection": reason, "spec_approval": None}), "human_question": ""}
        if len(rejected) >= config.max_review_rounds:
            update.update(halted(Halt.spec_cap, detail, "spec"))
        return update

    def reserve(name: str, state: DeliveryState, runtime: Runtime[LiveRun], guard: Callable[[DeliveryState, LiveRun, str], DeliveryUpdate | None] | None = None) -> DeliveryUpdate:
        review_state = state["review"]
        admission = dollar_admission_for(state["run"], state.get("attempts", []))
        if stop := admission_stop(state, state.get("attempts", []), name, admission, work_tokens(state.get("attempts", []))):
            return stop
        current_run = runtime.context or run
        if guard is not None:
            if stop := guard(state, current_run, name):
                return stop
        try:
            models = STAGES[name].models(name, state, config)
        except ModelDependencyMissing as exc:
            return halted(Halt.model_dependency_missing, str(exc), name)
        except ConfigError as exc:
            return halted(Halt.model_family_exhausted, str(exc), name)
        instruction = prompts[name]
        methods_text, methods, read_roots = stage_methods(name, state, methods_root) if methods_root is not None else ("", [], [])
        if methods_text:
            instruction = f"{instruction}\n\nApply the methods listed below. Do not start subagents.\n\n{methods_text}"
        try:
            suffix, target, scope = STAGES[name].prepare(state, run.workspace, run.forge)
        except InvalidOutput as exc:
            return halted(Halt.invalid_output, str(exc), name)
        except (ForgeError, WorktreeInspectionFailed) as exc:
            return halted(Halt.review_unavailable, str(exc), name)
        instruction += suffix
        return {"dispatch_plan": DispatchPlan(
            models=[model.name for model in models], instruction=instruction, methods=methods, read_roots=read_roots,
            budget_usd=dispatch_budget(admission),
            target=target,
            scope=scope.record() if scope else None,
        ).record(), "pending_dispatch": PendingDispatch(stage=name, index=0, quota_model=None).record(),
            "review": review_state.model_copy(update={"review_run_id": review_state.review_run_id or uuid4().hex})}

    def bind(name: str, *, prepare_dispatch: DeliveryNode | None = None,
             guard: Callable[[DeliveryState, LiveRun, str], DeliveryUpdate | None] | None = None) -> DeliveryNode:
        def node(state: DeliveryState, runtime: Runtime[LiveRun]) -> DeliveryUpdate:
            return prepare_dispatch(state, runtime) if prepare_dispatch is not None else reserve(name, state, runtime, guard)
        return node

    def call() -> DeliveryNode:
        def node(state: DeliveryState, runtime: Runtime[LiveRun]) -> DeliveryUpdate:
            assert "pending_dispatch" in state and "dispatch_plan" in state
            pending, plan = state["pending_dispatch"], state["dispatch_plan"]
            assert pending is not None
            name = pending["stage"]
            models, methods = plan["models"], plan["methods"]
            try:
                worker_state, citation_context = STAGES[name].project(state, run.workspace)
            except WorktreeInspectionFailed as exc:
                return {"pending_dispatch": None, **halted(Halt.review_unavailable, str(exc), name)}
            accepted_model = ModelSpec.parse(models[pending["index"]]) if models else None
            outcome = clean_outcome(dispatcher(name, accepted_model, worker_state, plan["instruction"]))
            model = accepted_model.name if accepted_model else None
            attempts = [Attempt(
                stage=name, model=model, commit=config.pstack.commit, methods=methods, outcome=outcome.kind,
                prompt_bytes=outcome.prompt_bytes, native_usage=outcome.native_usage,
                usage=None if outcome.usage is None else AttemptUsage(
                    input=outcome.usage.input_tokens, output=outcome.usage.output_tokens,
                    cache_read=outcome.usage.cache_read_input_tokens,
                    cache_creation=outcome.usage.cache_creation_input_tokens,
                    cost_usd=outcome.usage.total_cost_usd,
                ),
            ).record()]
            stopped: DeliveryUpdate = {"path": [name], "attempts": attempts, "pending_dispatch": None}
            attempt_history = state.get("attempts", []) + attempts
            work = work_tokens(attempt_history)
            if work is None:
                return {**stopped, **halted(Halt.usage_unknown, unknown_usage(name, model) + " (unknown work charge)", name)}
            if outcome.kind == "budget_exhausted":
                return {**stopped, **halted(Halt.budget_exceeded, f"{name} {model} exhausted its dollar budget. {dollar_detail(state['run'], attempt_history)} {outcome.detail}".strip(), name)}
            admission = dollar_admission_for(state["run"], attempt_history)
            if stop := admission_stop(state, attempt_history, name, admission, work):
                return {**stopped, **stop}
            index = pending["index"] + 1
            unavailable = STAGES[name].unavailable(state, outcome, model)
            if outcome.kind in {"quota", "cli_missing"} and index < len(models):
                retry: DeliveryUpdate = {"attempts": attempts,
                    "dispatch_plan": {**plan, "budget_usd": dispatch_budget(admission)}, "pending_dispatch": PendingDispatch(
                    stage=name, index=index,
                    quota_model=model if outcome.kind == "quota" else pending["quota_model"],
                ).record()}
                if unavailable:
                    retry["halt_detail"] = unavailable[1]
                return retry
            if unavailable:
                return {**stopped, **halted(*unavailable, name)}
            if outcome.kind == "cli_missing" and pending["quota_model"]:
                outcome, model = Outcome("quota"), pending["quota_model"]
            if outcome.kind == "failed":
                return {**stopped, **halted(Halt.invalid_output, f"{name} output from {model or 'no model'}: {outcome.detail}", name)}
            if outcome.kind == "cli_missing":
                return {**stopped, **halted(Halt.cli_missing, outcome.detail, name)}
            if outcome.kind == "quota":
                return {**stopped, **halted(Halt.quota_exhausted, f"{name} quota exhausted: {model}", name)}
            try:
                contract = STAGES[name].output
                assert contract is not None
                output = parse_output(contract, outcome.output, context=citation_context)
                update = stage_update(name, output, state, model, workspace=plan_workspace,
                                      accepted_model=accepted_model, config=config, attempt=attempts[0])
                if isinstance(output, SpecCheckOutput):
                    planning = state["plan"]
                    candidate = SpecCandidate.from_record(planning.spec_candidate)
                    if output.findings or not output.human_question:
                        past = SpecHistory.model_validate(planning.spec_history)
                        checked = accept_spec_check(past, output, candidate)
                        planning = planning.model_copy(update={"spec_history": checked.history.model_dump(mode="json"),
                            "spec_approval": checked.approval.revision if checked.approval else None})
                        if not checked.approval:
                            detail = "\n".join(f.summary for f in checked.history.accounting().unresolved())
                            update.update(reject_spec({**state, "plan": planning}, candidate.revision, detail))
                        else:
                            update.update(plan=planning, human_question="")
                update.update(attempts=update.get("attempts", attempts), pending_dispatch=None)
            except InvalidSlicePlan as exc:
                revision = spec_digest([spec_target(state["run"]), outcome.output])
                return {**stopped, **reject_spec(state, revision, str(exc), exc.reason)}
            except InvalidOutput as exc:
                return {**stopped, **halted(Halt.invalid_output, f"{name} output from {model or 'no model'}: {exc}", name)}
            except WorkspaceError as exc:
                return {**stopped, **halted(Halt.git_failed, str(exc), name)}

            return update

        return node

    def spec(state: DeliveryState, runtime: Runtime[LiveRun]) -> DeliveryUpdate:
        plan = state["plan"]
        if plan.spec_candidate is not None and state["path"][-1:] == ["spec_check"] and not plan.spec_approval:
            if (plan.spec_candidate["revision"] not in plan.rejected_revisions):
                return reserve("spec_check", state, runtime)
            return reserve("spec", state, runtime)
        size = oversized_pr(state)
        supplied = plan.slice is not None and not plan.spec_gate and not size
        pending = [plan.slice] if supplied and plan.slice is not None else plan.slices
        if not pending or plan.spec_rejection or (size and plan.pr_split is None) or plan.spec_gate == "needs_human":
            return reserve("spec", state, runtime)
        target = spec_target(state["run"])
        if any((repo := item.get("repository")) and repo.lower() != target["repo"] for item in pending):
            return halted(Halt.invalid_output, "foreign_repository: slice scope differs from the admitted repository.", "spec")
        split = plan.pr_split if size else None
        scope = split["plan"] if split else plan.spec
        try:
            slices = clean_slices([parse_output(StoredSlice, item) for item in pending], plan_workspace, target["base"])
        except InvalidSlicePlan as exc:
            revision = spec_digest([target, scope, plan.work_kind, pending])
            return reject_spec(state, revision, str(exc), exc.reason)
        except WorkspaceError as exc:
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
            return reserve("spec", {**state, "plan": planning}, runtime)
        return {**reserve("spec_check", {**state, "plan": planning}, runtime), "plan": planning}

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
        if stop := implementation_guard(state, runtime.context or run, "prepare"):
            return stop
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

    dispatch_implement = call()

    def implement(state: DeliveryState, runtime: Runtime[LiveRun]) -> DeliveryUpdate:
        assert "work" in state and state["work"].branch and state["work"].attempt_head
        assert "pending_dispatch" in state and state["pending_dispatch"] is not None and "dispatch_plan" in state
        update: DeliveryUpdate = {}
        try:
            clearance = planning_guard(state, "implement")
            if clearance.get("halt") or clearance.get("stop"):
                return {**clearance, "pending_dispatch": None}
            update = {**clearance, **dispatch_implement(state, runtime)}
            if update.get("pending_dispatch"):
                return update
            return {**update, "work": state["work"].model_copy(update={"partial_edits": run.workspace.fingerprint() if update.get("halt") == Halt.quota_exhausted else ""})}
        except WorkspaceError as exc:
            return {**update, **halted(Halt.git_failed, str(exc), "implement")}

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
        if stage in {"spec", "spec_check"}:
            revision = spec_digest([spec_target(state["run"]), planning.spec_candidate, planning.spec, question])
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

    def quota_wait(state: DeliveryState) -> DeliveryUpdate:
        interrupt(QuotaPause(detail=state.get("halt_detail")).model_dump())
        return {"halt": None, "halt_detail": ""}

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
            restored = candidate.update(planning, single=True).model_copy(update={"pr_split": None, "spec_approval": candidate.revision})
            return {"pr_size_override": size, "plan": restored}
        return {}

    def size(state: DeliveryState) -> DeliveryUpdate:
        assert "work" in state
        candidate = state["work"].candidate
        assert candidate is not None
        return {"pr_size": candidate["size"]}

    dispatch_review = bind("review")

    def review(state: DeliveryState, runtime: Runtime[LiveRun]) -> DeliveryUpdate:
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
            return halted(Halt.review_unavailable, str(exc), "review")
        return dispatch_review(state, runtime)

    def after_triage(state: DeliveryState) -> str:
        planning = state["plan"]
        if state.get("halt"):
            return "done"
        decision = planning.triage
        if decision == "needs_human":
            return "needs_human"
        if decision == "question":
            return "research"
        return "spec" if decision in {"implement", "spec"} else "done"

    def after_spec(state: DeliveryState) -> str:
        planning = state["plan"]
        if state.get("halt") or state.get("stop") == "cancelled":
            return "done"
        if planning.spec_rejection:
            return "spec"
        if state.get("human_question"):
            return "needs_human"
        if planning.spec_approval is None:
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

    def add_agent_edges(stage: str, route: Callable[[DeliveryState], str], targets: dict[str, str], *, call_only: tuple[str, ...] = ()) -> None:
        def next_stage(state: DeliveryState) -> str:
            return "quota_wait" if state.get("halt") == Halt.quota_exhausted else route(state)

        def after_setup(state: DeliveryState) -> str:
            return "call" if state.get("pending_dispatch") and not state.get("halt") else next_stage(state)
        mapped: dict[str, str] = {**targets, "quota_wait": "quota_wait", "call": stage + "_call"}
        builder.add_conditional_edges(stage, after_setup, {key: value for key, value in mapped.items() if key not in call_only})
        builder.add_conditional_edges(stage + "_call", after_setup, mapped)

    graph_nodes: dict[str, DeliveryNode] = {"spec": bind("spec", prepare_dispatch=spec),
        "implement": bind("implement", guard=implementation_guard), "review": review}
    for name in MODEL_STAGES:
        builder.add_node(name, graph_nodes[name] if name in graph_nodes else bind(name))
        builder.add_node(name + "_call", implement if name == "implement" else call())
    builder.add_node("verify", verify)
    builder.add_node("prepare", prepare)
    builder.add_node("reconcile", reconcile)
    builder.add_node("size", size)
    builder.add_node("commit", commit)
    builder.add_node("publish", publish)
    builder.add_node("review_post", post_review)
    builder.add_node("needs_human", needs_human)
    builder.add_node("quota_wait", quota_wait)

    builder.add_node("decide_pr_size", decide_pr_size)
    builder.add_node("merge", arm_merge)
    builder.add_node("done", finish)
    builder.add_conditional_edges(
        START, after_halt("triage"), {"done": "done", "triage": "triage"},
    )
    add_agent_edges(
        "triage",
        after_triage,
        {"spec": "spec", "research": "research",
         "needs_human": "needs_human", "done": "done"},
    )
    add_agent_edges(
        "spec", after_spec,
        {"spec": "spec", "prepare": "prepare", "needs_human": "needs_human", "decide_pr_size": "decide_pr_size", "done": "done"},
        call_only=("prepare",),
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
    builder.add_conditional_edges(
        "quota_wait", _last_stage, {name: name for name in MODEL_STAGES},
    )
    add_agent_edges("research", lambda _: "done", {"done": "done"})
    add_agent_edges("implement", after_halt("reconcile"), {"reconcile": "reconcile", "done": "done"})
    add_agent_edges(
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
    graph.invoke(state, {"configurable": {"thread_id": thread_id}}, version="v2")
    return snapshot_result(graph.get_state({"configurable": {"thread_id": thread_id}}))


def diagram() -> str:
    dry = DryRun()
    graph = compile_delivery(ScriptHandler(), load_config(), run=LiveRun(forge=dry, workspace=dry))
    return graph.get_graph().draw_mermaid()
