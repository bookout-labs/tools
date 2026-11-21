"""The delivery graph. Edges live here. Models live in models.toml."""

from decimal import Decimal
import json
from uuid import uuid4
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from functools import cached_property
from graphlib import CycleError, TopologicalSorter
from pathlib import Path
from tomllib import TOMLDecodeError
from typing import Literal, Protocol, cast
import re
import textwrap

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from langgraph.types import interrupt
from pydantic import TypeAdapter, ValidationError

from foreman.adapters import Outcome
from foreman.budget import CostUnknown, Exhausted, Remaining, claude_spend, dollar_admission
from foreman.config import Config, ConfigError, ModelDependencyMissing, ModelSpec, contributors_for, family, load_config, order_for
from foreman.dry_run import DryRun, ScriptHandler
from foreman.dispatch import Handler, InvalidOutput, clean_outcome
from foreman.fixture_manifest import MANIFEST_PATH, fact_record_path, recorded_evidence
from foreman.github import (
    MERGE_HOLD_VARIABLE, Forge, ForgeError, MergeHoldUnreadable, MergeNotArmed, HandoffChanged, QueueRejected, PullRequest, ReviewComment, merge_command,
)
from foreman.proof import browser_proof_label, browser_required, formal_proof_label, formal_required
from foreman.skill_rules import SkillMissing, SkillSource, export_skills, stage_methods
from foreman.redaction import redact
from foreman.stage_output import (
    ReviewSelection, CandidateSlice, SpecAuthor, SpecCandidate, PendingDispatch, DispatchPlan, Attempt, AttemptRecord, AttemptUsage, Contract, UpdateContext, SpecOutput, SliceRecord, StoredToolFact, GraphCheck as GraphEvidence, StageOutput, PULL_REQUEST_LINES, ReviewOutput, STAGE_OUTPUTS, Slice, StoredSlice, VerifyOutput, parse_output,
)
from foreman.state import RunState, SCHEMA_VERSION, checkpoint_serializer, DeliveryFields, DeliveryState, DeliveryUpdate, DeliveryResult, SpecRejection, SpecRejectionKind
from foreman.readiness import MergeReceipt, MergeState
from foreman.outcomes import Halt, READINESS_HALTS
from foreman.review_round import AcceptedReview, GraphCheck, ReviewHistory, accept, history
from foreman.usd import BudgetUsd, usd_text
from foreman.workspace import WorkState, ImplementationRejected, WorkspaceError, CheckLog, PullRequestSize, ReviewScope, Workspace, WorktreeInspectionFailed, head_mismatch_detail

GRAPH_NAME = "delivery"
DEFAULT_TOKEN_CAP = 20_000_000
PACKAGE = Path(__file__).parent
MODEL_STAGES = ("triage", "research", "spec", "implement", "review")
REQUIRED_NODES = (*MODEL_STAGES, "verify")
COAUTHOR_EMAILS = {"gpt": "noreply@openai.com", "claude": "noreply@anthropic.com", "grok": "noreply@x.ai"}
CHECK_START_FAILURES = {126: "command is not executable", 127: "command not found"}


class HumanPrompt(Contract):
    stage: Literal["triage", "spec", "pr_size", "review"]
    question: str


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


def unknown_usage(stage: str, model: str | None) -> str:
    return f"{model} ran {stage} without a usage report"


def run_tokens(attempts: list[AttemptRecord]) -> int:
    return sum(usage["input"] + usage["output"] + usage["cache_read"] + usage["cache_creation"]
               for attempt in attempts if (usage := attempt["usage"]) is not None)


class InvalidSlicePlan(InvalidOutput):
    def __init__(self, kind: SpecRejectionKind, detail: str):
        super().__init__(detail)
        self.reason: SpecRejection = {"kind": kind, "detail": detail}


class PullRequestSizeDecision(StrEnum):
    SPLIT = "split"
    OVERRIDE = "override"


_DELIVERY_STATE = TypeAdapter(DeliveryResult)


def review_count(state: DeliveryFields) -> int:
    return len(state.get("review_findings", []))


def read_delivery_state(raw: object) -> DeliveryResult:
    result = _DELIVERY_STATE.validate_python(raw, strict=True)
    for key in ("verify_failures", "proof_failures", "spec_failures"):
        result.setdefault(key, result.get("review_rounds", 0))
    result["review_rounds"] = review_count(result)
    return result


def _active_slice(state: DeliveryState) -> SliceRecord:
    slice = state.get("slice")
    if slice is None:
        raise InvalidOutput("implementation requires an active slice")
    return slice


def _last_stage(state: DeliveryState) -> str:
    return state["path"][-1]


def halted(reason: Halt, detail: str, path: str | None) -> DeliveryUpdate:
    if not detail.strip():
        raise ValueError(f"{reason} requires a nonempty halt detail")
    # Checkpoint codes remain strings after validation as Halt members.
    return {"halt": cast(Halt, reason.value), "halt_detail": detail, "watch": None, "path": [path] if path else []}


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
    admission: Remaining | Exhausted | CostUnknown | None,
) -> DeliveryUpdate | None:
    run_state = state["run"]
    assert run_state.token_cap is not None
    if run_tokens(attempts) >= run_state.token_cap:
        last = attempts[-1]
        detail = (f"Run used {run_tokens(attempts)} tokens; cap is {run_state.token_cap}. "
                  f"Last attempt: {last['stage']} {last['model']} used {run_tokens([last])}. "
                  "An in-flight attempt can overshoot the cap.")
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
    return (PACKAGE / "stages" / f"{name}.md").read_text().strip()


def clean_slices(
    raw: list[Slice] | None,
    workspace: Workspace | None,
) -> list[SliceRecord]:
    if not raw:
        raise InvalidOutput("slices: ready parent spec needs at least one slice")
    if len(raw) == 1:
        return [{**raw[0].record(), "depends_on": []}]
    slices = {item.title: item for item in raw}
    if len(slices) != len(raw):
        raise InvalidSlicePlan("duplicate_title", "Slice titles must be unique.")
    builders: dict[str, str] = {}
    for item in raw:
        for seam in item.builds:
            if seam.name in builders and builders[seam.name] != item.title:
                raise InvalidSlicePlan("duplicate_builder", f"{seam.name}: built by {builders[seam.name]} and {item.title}.")
            builders[seam.name] = item.title
    graph: dict[str, dict[str, None]] = {}
    for item in raw:
        dependencies = graph[item.title] = {}
        for seam in item.uses:
            if seam.path is not None and (workspace is None or not workspace.has_file(seam.path)):
                raise InvalidSlicePlan("dangling_seam", f"{item.title} uses {seam.name}, but {seam.path} is not a workspace file.")
            if builder := builders.get(seam.name):
                if seam.exists:
                    raise InvalidSlicePlan("existing_seam_builder", f"{seam.name}: marked existing but built by {builder}.")
                if builder != item.title:
                    dependencies[builder] = None
            elif seam.path is None:
                raise InvalidSlicePlan("dangling_seam", f"{item.title} uses {seam.name}, which has no builder or existing file.")
    try:
        ordered = list(TopologicalSorter(graph).static_order())
    except CycleError as exc:
        raise InvalidSlicePlan("dependency_cycle", "Dependency cycle: " + " -> ".join(exc.args[1])) from exc
    return [{**slices[title].record(), "depends_on": list(graph[title])} for title in ordered]


def load_slice(raw: object) -> DeliveryUpdate:
    stored = parse_output(StoredSlice, raw)
    state: DeliveryUpdate = {"slice": stored.record()}
    if stored.work_kind is not None:
        state["work_kind"] = stored.work_kind
    return state


def graph_check(state: DeliveryState) -> GraphEvidence | None:
    head, command, code = state.get("verified_head"), state.get("check"), state.get("check_exit_code")
    if head and command and code == 0 and state.get("verify") == "pass":
        return GraphEvidence(head=head, command=command, exit_code=code)
    return None


def carried_tool_facts(state: DeliveryFields) -> list[StoredToolFact]:
    gate = history({"rounds": state.get("review_findings", [])}).gate
    return gate.tool_facts if gate else []


def _check_tool_facts(
    output: ReviewOutput, workspace: Workspace, state: DeliveryState,
) -> GraphCheck | None:
    head = state.get("verified_head")
    manifest_path = state.get("fact_record", MANIFEST_PATH)
    facts = {fact.claim: fact for fact in carried_tool_facts(state)}
    for fact in output.tool_facts:
        facts.setdefault(fact.claim, StoredToolFact.from_fact(fact))
    proven = graph_check(state)
    outside = [fact for fact in facts.values() if not (
        proven is not None and fact.graph_check == proven and fact.claim == proven.claim
    )]
    recorded: dict[str, list[str]] = {}
    notes: list[str] = []
    if head and outside:
        try:
            recorded = recorded_evidence(workspace, head, manifest_path)
        except (TOMLDecodeError, ValidationError, WorktreeInspectionFailed) as exc:
            notes.append(f"Could not check {manifest_path} at {head}. {exc}")
    claim_ids = {claim_id for ids in recorded.values() for claim_id in ids}
    unrecorded = [fact for fact in outside if fact.fixture not in recorded and fact.claim_id not in claim_ids]
    if not outside:
        return None
    for fact in unrecorded:
        citation = f" (fixture {fact.fixture})" if fact.fixture is not None else ""
        notes.append(f"Unrecorded outside-tool fact {fact.claim_id}{citation}: {fact.claim}")
    return GraphCheck(failing_heads=[head or ""] if unrecorded else [],
                      detail="\n".join(notes), passed=not unrecorded, tool_facts=outside)


def failure_count(state: DeliveryState, key: Literal["verify_failures", "proof_failures", "spec_failures"]) -> int:
    # Older checkpoints combined failures and reviews. Retain that budget conservatively.
    return state.get(key, state.get("review_rounds", 0))


def stage_update(
    name: str, output: StageOutput, state: DeliveryState, model: str | None = None, *, workspace: Workspace | None = None, accepted_model: ModelSpec | None = None,
) -> DeliveryUpdate:
    slices = clean_slices(output.slices, workspace) if isinstance(output, SpecOutput) and state.get("issue_kind") == "parent" and output.spec_gate == "ready" else []
    context = UpdateContext(state.get("issue_kind"), state.get("work_kind"), bool(oversized_pr(state)), slices, accepted_model)
    update: DeliveryUpdate = {**output.update(context)}
    update["path"] = [name]
    if update.get("verify") == "fail":
        update["verify_failures"] = failure_count(state, "verify_failures") + 1
    if model:
        used = state.get("models_used", {})
        update["models_used"] = {**used, name: model}
        if name == "implement":
            latest = used.get("implement")
            history = state.get("implement_models", [latest] if latest else [])
            update["implement_models"] = [*history, model]
    return update


def review_submission(output: AcceptedReview, scope: ReviewScope) -> tuple[str, list[ReviewComment]]:
    body = [f"Graph verdict: {output.review}. Review event: COMMENT (Foreman authors this PR).", output.detail]
    provenance = output.history.rounds[-1].provenance if output.history.rounds else None
    if provenance is not None:
        reason = provenance.reason.replace("_", " ") if provenance.reason else "not needed"
        body.append(f"Reviewer {provenance.reviewer}; implemented by {', '.join(provenance.contributors)}; same-family fallback: {reason}.")
    comments: list[ReviewComment] = []
    for finding in output.findings:
        message = f"[{finding.severity}] {finding.summary}"
        if (finding.file, finding.line) in scope.inline:
            comments.append({"path": finding.file, "line": finding.line, "side": "RIGHT", "body": message})
        else:
            body.append(f"{finding.file}:{finding.line} {message}")
    return "\n\n".join(body), comments


def stop_reason(state: DeliveryState) -> Halt | Literal[
    "sliced", "pr_split_required", "needs_human", "answered", "verified", "stopped"
]:
    if halt := state.get("halt"):
        return halt
    if oversized_pr(state):
        split = state.get("pr_split")
        gate = split["gate"] if split else state.get("spec_gate")
        if gate == "ready":
            return "sliced" if state.get("issue_kind") == "parent" else "pr_split_required"
        if gate == "needs_human":
            return "needs_human"
    if state.get("triage") == "needs_human" or state.get("spec_gate") == "needs_human":
        return "needs_human"
    if state.get("spec_gate") == "ready" and state.get("path", [])[-1:] == ["spec"]:
        if state.get("issue_kind") == "parent":
            return "sliced"
    if state.get("triage") == "question":
        return "answered"
    if state.get("verify") == "fail":
        return Halt.verify_cap
    if state.get("review") == "changes":
        return Halt.review_cap
    if state.get("verify") == "pass":
        return "verified"
    return "stopped"


def failed_preflight(workspace: Workspace, command: str, *, log: CheckLog | None = None) -> str | None:
    if command and (exit_code := workspace.run(command, log=log)):
        return f"Preflight {command!r} exited with {exit_code}"
    return None


def oversized_pr(state: DeliveryState) -> PullRequestSize | None:
    size = state.get("pr_size")
    if size is not None and size["lines"] > PULL_REQUEST_LINES and size != state.get("pr_size_override"):
        return size
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
    prompts = {name: stage_text(name) for name in MODEL_STAGES}
    plan_workspace = run.workspace

    def bind(name: str) -> DeliveryNode:
        def node(state: DeliveryState, runtime: Runtime[LiveRun]) -> DeliveryUpdate:
            admission = dollar_admission_for(state["run"], state.get("attempts", []))
            if stop := admission_stop(state, state.get("attempts", []), name, admission):
                return stop
            current_run = runtime.context or run
            if name == "implement":
                probe = current_run.environment or EnvironmentProbe(
                    current_run.workspace, state.get("preflight", ""), current_run.check_log,
                )
                if detail := probe.error:
                    return halted(Halt.env_unavailable, detail, name)
                try:
                    before = current_run.workspace.inspect()
                    work = state.get("work", WorkState())
                    branch, parent = work.branch, work.attempt_head
                    if before.branch != branch or before.head != parent:
                        return halted(Halt.dirty_worktree, "Restore the attempt's branch and starting HEAD before resuming edits.", "implement")
                    partial = work.partial_edits
                    if not before.clean and partial and current_run.workspace.fingerprint() != partial:
                        return halted(Halt.dirty_worktree, "Edits changed after the quota stop; restore the run's partial edits.", "implement")
                except WorkspaceError as exc:
                    return halted(Halt.git_failed, str(exc), name)
            try:
                models = order_for(name, config, state.get("models_used") or {}, state.get("implement_models"))
            except ModelDependencyMissing as exc:
                return halted(Halt.model_dependency_missing, str(exc), name)
            except ConfigError as exc:
                return halted(Halt.model_family_exhausted, str(exc), name)
            instruction = prompts[name]
            methods_text, methods, read_roots = stage_methods(name, state, methods_root) if methods_root is not None else ("", [], [])
            if methods_text:
                instruction = f"{instruction}\n\nApply the methods listed below. Do not start subagents.\n\n{methods_text}"
            if name in {"implement", "review"}:
                instruction += f"\n\nFact record in this checkout: {state.get('fact_record', MANIFEST_PATH)}"
            gate = history({"rounds": state.get("review_findings", [])}).gate if state.get("review") == "changes" else None
            if name == "implement" and gate and not gate.passed:
                instruction += (
                    "\nResolve the tool-facts finding by recording evidence and creating the record and parent directories if absent."
                    " Fixture IDs are paths relative to the record directory. Write regular evidence files."
                    " Use TOML [fixtures.\"<evidence-file>\"] with tool and version, plus either"
                    " kind = \"captured_run\" and argv = [\"tool\", \"arg\"] for captured output, or"
                    " kind = \"quoted_source\" and sources = [\"https://source\"] for quoted source evidence."
                    ' Add claim_ids = ["<claim_id>"] to the evidence entry for each carried claim.'
                    " Support each carried tool fact; the graph rechecks the published verified head."
                )
                instruction += "\nCarried tool facts:\n" + json.dumps([fact.model_dump() for fact in carried_tool_facts(state)])
            if name == "review" and (proven := graph_check(state)) is not None:
                instruction += f"\nGraph fact: {proven.claim}\nGraph check evidence: {proven.model_dump_json()}"
            if name == "spec" and (size := oversized_pr(state)):
                instruction += (
                    f"\n\nThe pull request has {size['lines']} changed lines. The limit is {PULL_REQUEST_LINES}."
                    " Split the work into independently shippable pull requests."
                    " For a slice issue, describe the split in spec."
                )

            past = ReviewHistory()
            if name == "review":
                try:
                    past = history({"rounds": state.get("review_findings", []), "settled": state.get("settled_rulings", [])})
                except InvalidOutput as exc:
                    return halted(Halt.invalid_output, str(exc), name)
            review_context: tuple[Forge, PullRequest, ReviewScope] | None = None
            run_state = state["run"]
            repo = run_state.repo
            if name == "review":
                head, issue = state.get("verified_head"), run_state.issue
                try:
                    worktree = run.workspace.inspect()
                    if not (repo and issue is not None and head and worktree.clean and worktree.branch and worktree.head == head):
                        raise WorktreeInspectionFailed("Review requires a clean branch at the verified HEAD.")
                    target = run.forge.review_target(repo, issue, worktree.branch, head)
                    diff = run.workspace.review_scope(target.baseRefOid, head, past.rounds[-1].reviewed_head if past.rounds else None)
                except (ForgeError, WorktreeInspectionFailed) as exc:
                    return halted(Halt.review_unavailable, str(exc), name)
                review_context = (run.forge, target, diff)
                instruction = f"{instruction}\n\nRead the labeled review diffs with these commands in the checkout:\n{diff.command}"
            if name == "review":
                scope = "Full PR review." if not past.rounds else "Check unresolved findings and added interdiff lines also in the PR diff."
                instruction += "\n\n" + scope
                instruction += "\nUnresolved findings:\n" + json.dumps([f.model_dump() for f in past.unresolved()])
                instruction += "\nGraph checks, not yours to resolve:\n" + json.dumps(past.rounds[-1].graph_check.model_dump(mode="json") if past.rounds and past.rounds[-1].graph_check else None)
                instruction += "\nSettled, don't reopen:\n" + json.dumps([r.model_dump() for r in past.rulings()])
            return {"verify_failures": failure_count(state, "verify_failures"),
                    "proof_failures": failure_count(state, "proof_failures"), "spec_failures": failure_count(state, "spec_failures"),
                    "review_run_id": state.get("review_run_id") or uuid4().hex, "dispatch_plan": DispatchPlan(
                models=[model.name for model in models], instruction=instruction, methods=methods, read_roots=read_roots,
                budget_usd=dispatch_budget(admission),
                target=review_context[1] if review_context else None,
                scope=review_context[2].record() if review_context else None,
            ).record(), "pending_dispatch": PendingDispatch(stage=name, index=0, quota_model=None).record()}
        return node

    def call(name: str) -> DeliveryNode:
        def node(state: DeliveryState, runtime: Runtime[LiveRun]) -> DeliveryUpdate:
            assert "pending_dispatch" in state and "dispatch_plan" in state
            pending, plan = state["pending_dispatch"], state["dispatch_plan"]
            assert pending is not None
            models, methods = plan["models"], plan["methods"]
            worker_state: DeliveryState = state.copy()
            if name == "review":
                for key in ("review_findings", "settled_rulings", *(("findings", "detail", "implementation") if history({"rounds": state.get("review_findings", []), "settled": state.get("settled_rulings", [])}).rounds else ())):
                    worker_state.pop(key, None)
            citation_context = None
            if name == "review":
                try:
                    assert plan["target"] is not None
                    target = plan["target"]
                    citation_context = run.workspace.review_context(target["baseRefOid"], target["headRefOid"])
                except WorktreeInspectionFailed as exc:
                    return {"pending_dispatch": None, **halted(Halt.review_unavailable, str(exc), name)}
                if not (citation_context.base.changed or citation_context.head.changed):
                    return {"pending_dispatch": None, **halted(Halt.review_unavailable, "Review requires changed paths.", name)}
            accepted_model = ModelSpec.parse(models[pending["index"]]) if models else None
            outcome = clean_outcome(dispatcher(name, accepted_model, worker_state, plan["instruction"]))
            model = accepted_model.name if accepted_model else None
            attempts = [Attempt(
                stage=name, model=model, commit=config.pstack.commit, methods=methods, outcome=outcome.kind,
                usage=None if outcome.usage is None else AttemptUsage(
                    input=outcome.usage.input_tokens, output=outcome.usage.output_tokens,
                    cache_read=outcome.usage.cache_read_input_tokens or 0,
                    cache_creation=outcome.usage.cache_creation_input_tokens or 0,
                    cost_usd=outcome.usage.total_cost_usd,
                ),
            ).record()]
            stopped: DeliveryUpdate = {"path": [name], "attempts": attempts, "pending_dispatch": None}
            attempt_history = state.get("attempts", []) + attempts
            if outcome.kind == "budget_exhausted":
                return {**stopped, **halted(Halt.budget_exceeded, f"{name} {model} exhausted its dollar budget. {dollar_detail(state['run'], attempt_history)} {outcome.detail}".strip(), name)}
            if outcome.kind == "ran" and outcome.usage is None:
                return {**stopped, **halted(Halt.usage_unknown, unknown_usage(name, model), name)}
            admission = dollar_admission_for(state["run"], attempt_history)
            if stop := admission_stop(state, attempt_history, name, admission):
                return {**stopped, **stop}
            index = pending["index"] + 1
            unavailable = ""
            if name == "review" and outcome.kind in {"quota", "cli_missing"}:
                prior = state.get("halt_detail", "") + "; " if pending["index"] else ""
                unavailable = f"{prior}review {model}: {outcome.kind}. {outcome.detail}".strip()
            if outcome.kind in {"quota", "cli_missing"} and index < len(models):
                retry: DeliveryUpdate = {"attempts": attempts,
                    "dispatch_plan": {**plan, "budget_usd": dispatch_budget(admission)}, "pending_dispatch": PendingDispatch(
                    stage=name, index=index,
                    quota_model=model if outcome.kind == "quota" else pending["quota_model"],
                ).record()}
                if unavailable:
                    retry["halt_detail"] = unavailable
                return retry
            if unavailable:
                return {**stopped, **halted(Halt.reviewer_unavailable, unavailable, name)}
            if outcome.kind == "cli_missing" and pending["quota_model"]:
                outcome, model = Outcome("quota"), pending["quota_model"]
            if outcome.kind == "failed":
                return {**stopped, **halted(Halt.invalid_output, f"{name} output from {model or 'no model'}: {outcome.detail}", name)}
            if outcome.kind == "cli_missing":
                return {**stopped, **halted(Halt.cli_missing, outcome.detail, name)}
            if outcome.kind == "quota":
                return {**stopped, **halted(Halt.quota_exhausted, f"{name} quota exhausted: {model}", name)}
            try:
                output = parse_output(STAGE_OUTPUTS[name], outcome.output, context=citation_context)
                accepted: AcceptedReview | None = None
                if isinstance(output, ReviewOutput):
                    past = history({"rounds": state.get("review_findings", []), "settled": state.get("settled_rulings", [])})
                    head = state.get("verified_head") or state.get("work", WorkState()).head_sha or ""
                    accepted = accept(past, output, head, _check_tool_facts(output, run.workspace, state))
                if accepted is not None and accepted_model is not None:
                    contributors, families = contributors_for(name, config, state.get("models_used", {}), state.get("implement_models"))
                    same_family = accepted_model.family in families
                    other_configured = any(ModelSpec.parse(candidate).family not in families for candidate in models)
                    provenance = ReviewSelection(
                        reviewer=accepted_model.name, contributors=contributors, same_family=same_family,
                        reason=("preceding_candidates_unavailable" if other_configured else "no_other_family_configured") if same_family else None,
                    )
                    accepted.history.rounds[-1].provenance = provenance
                    attempts[0]["provenance"] = provenance.record()
                update = stage_update(name, output, state, model, workspace=plan_workspace, accepted_model=accepted_model)
                update.update(attempts=attempts, pending_dispatch=None)
                if accepted is not None:
                    update.update(review=accepted.review, detail=accepted.detail,
                                  review_findings=[r.model_dump(mode="json") for r in accepted.history.rounds],
                                  reviewed_head=accepted.history.rounds[-1].reviewed_head,
                                  findings=[f.record() for f in accepted.findings],
                                  review_change_kind=accepted.change_kind,
                                  review_receipt=None)
                    if "halt_detail" in state:
                        update["halt_detail"] = ""
                    if accepted.rethink:
                        update.update(halted(Halt.design_rethink, accepted.rethink, name))
                        update["human_question"] = accepted.rethink
            except InvalidSlicePlan as exc:
                rounds = failure_count(state, "spec_failures") + 1
                rejected: DeliveryUpdate = {**stopped, "spec_rejection": exc.reason, "spec_failures": rounds}
                if rounds >= config.max_review_rounds:
                    rejected.update(halted(Halt.spec_cap, str(exc), name))
                return rejected
            except InvalidOutput as exc:
                return {**stopped, **halted(Halt.invalid_output, f"{name} output from {model or 'no model'}: {exc}", name)}

            return update

        return node

    def post_review(state: DeliveryState) -> DeliveryUpdate:
        assert "dispatch_plan" in state and "review_findings" in state and "detail" in state and "review_run_id" in state
        plan = state["dispatch_plan"]
        assert plan["target"] is not None and plan["scope"] is not None
        past = history({"rounds": state["review_findings"], "settled": state.get("settled_rulings", [])})
        accepted = AcceptedReview(history=past, findings=past.unresolved(), detail=state["detail"])
        body, comments = review_submission(accepted, TypeAdapter(ReviewScope).validate_python(plan["scope"]))
        target = PullRequest.model_validate(plan["target"])
        marker = f"{state['review_run_id']}:{len(past.rounds)}:{target.headRefOid}"
        try:
            receipt = run.forge.post_review(state["run"].repo, target, body, comments, marker=marker)
        except ForgeError as exc:
            return halted(Halt.review_not_posted, str(exc), "review_post")
        return {"review_receipt": receipt}

    def prepare(state: DeliveryState) -> DeliveryUpdate:
        assert "slice" in state and "run" in state
        title = redact(str(state["slice"]["title"]))
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60].rstrip("-") or "change"
        work = state.get("work", WorkState())
        run_state = state["run"]
        branch = work.branch or f"{run_state.issue or '<issue>'}-{slug}"
        try:
            before = run.workspace.inspect()
            if not before.clean:
                return halted(Halt.dirty_worktree, "Unowned edits exist before this attempt; save them before rerunning.", "prepare")
            base = work.base or run.forge.default_branch(run_state.repo)
            run.workspace.create_branch(branch, base)
            if repair_head := work.repair_head:
                run.workspace.refresh(branch, repair_head)
            ready = run.workspace.inspect()
            return {"work": WorkState(branch=branch, base=base, attempt_head=ready.head)}
        except (WorkspaceError, ForgeError) as exc:
            return halted(Halt.git_failed, str(exc), "prepare")

    dispatch_implement = call("implement")

    def implement(state: DeliveryState, runtime: Runtime[LiveRun]) -> DeliveryUpdate:
        assert "work" in state and state["work"].branch and state["work"].attempt_head
        assert "pending_dispatch" in state and state["pending_dispatch"] is not None and "dispatch_plan" in state
        update: DeliveryUpdate = {}
        try:
            update = dispatch_implement(state, runtime)
            if update.get("pending_dispatch"):
                return update
            return {**update, "work": state["work"].model_copy(update={"partial_edits": run.workspace.fingerprint() if update.get("halt") == Halt.quota_exhausted else ""})}
        except WorkspaceError as exc:
            return {**update, **halted(Halt.git_failed, str(exc), "implement")}

    def reconcile(state: DeliveryState) -> DeliveryUpdate:
        assert "work" in state and state["work"].attempt_head and state["work"].branch and state["work"].base
        try:
            candidate = run.workspace.reconcile(state["work"].attempt_head, state["work"].branch,
                                                state["work"].base, state.get("fact_record", MANIFEST_PATH))
            if candidate is None:
                identifiers = [str(f.get("id")) for f in state.get("findings", [])]
                if (gate := history({"rounds": state.get("review_findings", [])}).gate) and not gate.passed:
                    identifiers.insert(0, "tool-facts")
                detail = "no changes" + ("; unresolved findings: " + ", ".join(identifiers) if identifiers else "")
                return halted(Halt.failed, detail, "reconcile")
            return {"work": state["work"].model_copy(update={"candidate": candidate})}
        except ImplementationRejected as exc:
            return halted(Halt.implementation_rejected, str(exc), "reconcile")
        except WorkspaceError as exc:
            return halted(Halt.git_failed, str(exc), "reconcile")

    def commit(state: DeliveryState) -> DeliveryUpdate:
        assert "slice" in state
        subject = f"{state['run'].issue or '<issue>'}: {' '.join(redact(str(state['slice']['title'])).split())}"
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
                if run.workspace.fingerprint() != candidate["fingerprint"]:
                    raise WorkspaceError("Implementation edits changed after reconciliation.")
                run.workspace.commit(message)
                observed = run.workspace.inspect()
                if not observed.clean:
                    raise WorkspaceError("The commit left uncommitted edits; restore the implementation tree.")
            return {"work": state["work"].model_copy(update={"head_sha": observed.head})}
        except WorkspaceError as exc:
            return halted(Halt.git_failed, str(exc), "commit")

    def publish(state: DeliveryState) -> DeliveryUpdate:
        assert "work" in state and state["work"].branch and state["work"].base and "run" in state and "slice" in state
        run_state = state["run"]
        branch, base, repo = state["work"].branch, state["work"].base, run_state.repo
        title = " ".join(redact(str(state["slice"]["title"])).split())
        body = f"Closes #{run_state.issue or '<issue>'}"
        try:
            observed = run.workspace.inspect()
            if not observed.clean or observed.branch != branch or observed.head != state["work"].head_sha:
                raise WorkspaceError("Restore the clean delivery branch at its implementation commit before publishing.")
            run.workspace.push(branch, repo)
            run.forge.open_pull_request(repo, branch, base, title, body)
        except (WorkspaceError, ForgeError) as exc:
            return halted(Halt.publish_failed, str(exc), "publish")
        return {"path": ["publish"]}

    def verify(state: DeliveryState, runtime: Runtime[LiveRun]) -> DeliveryUpdate:
        current = runtime.context or run
        workspace, check_log = current.workspace, current.check_log
        try:
            worktree = workspace.inspect()
        except WorktreeInspectionFailed as exc:
            return halted(Halt.dirty_worktree, str(exc), "verify")
        if not worktree.clean:
            return halted(Halt.dirty_worktree, "The worktree has changes.", "verify")
        command = state["check"]
        exit_code = workspace.run(command, log=check_log)
        detail = None
        if reason := CHECK_START_FAILURES.get(exit_code):
            detail = f"Check {command!r} exited with {exit_code} ({reason})"
        elif exit_code:
            detail = failed_preflight(workspace, state.get("preflight", ""), log=check_log)
        if detail:
            return {**halted(Halt.env_unavailable, detail, "verify"), "check_exit_code": exit_code}
        verdict = "pass" if exit_code == 0 else "fail"
        checked: DeliveryUpdate = {"verified_head": worktree.head} if exit_code == 0 else {}
        return {
            **stage_update("verify", VerifyOutput(verify=verdict), state),
            "check_exit_code": exit_code,
            "proof_labels": {},
            **checked,
        }

    def prove(state: DeliveryState) -> DeliveryUpdate:
        slice = _active_slice(state)
        labels = {
            "browser": browser_proof_label(slice, state.get("browser_proof")),
            "formal": formal_proof_label(slice, state.get("formal_proof")),
        }
        update: DeliveryUpdate = {"path": ["proof"], "proof_labels": {key: label for key, label in labels.items() if label}}
        if "insufficient_evidence" in labels.values():
            update.update(halted(Halt.insufficient_evidence, "Required proof is missing.", "proof"))
        elif "block" in labels.values():
            update["proof_failures"] = failure_count(state, "proof_failures") + 1
            if update["proof_failures"] >= config.max_review_rounds:
                detail = f"Proof failed after {update['proof_failures']} rounds: {update['proof_labels']}."
                update.update(halted(Halt.review_cap, detail, "proof"))
        return update

    def needs_human(state: DeliveryState) -> DeliveryUpdate:
        stage = state["path"][-1]
        split = state.get("pr_split") if stage == "spec" and oversized_pr(state) else None
        question = (split["question"] if split else state.get("human_question")) or "What should Foreman know to continue?"
        answer = interrupt(HumanPrompt.model_validate({"stage": stage, "question": question}).model_dump())
        update: DeliveryUpdate = {"human_answers": [*state.get("human_answers", []), answer]}
        if stage == "review":
            past = history({"rounds": state.get("review_findings", []), "settled": state.get("settled_rulings", [])})
            update.update(halt=None, halt_detail="", settled_rulings=[r.model_dump() for r in past.settled] +
                          [{"id": f"design:{len(past.rounds)}", "text": answer}])
            update.update(prepare(state))
        return update

    def quota_wait(state: DeliveryState) -> DeliveryUpdate:
        interrupt(QuotaPause(detail=state.get("halt_detail")).model_dump())
        return {"halt": None, "halt_detail": ""}

    def decide_pr_size(state: DeliveryState) -> DeliveryUpdate:
        size = oversized_pr(state)
        assert size is not None
        split = state.get("pr_split")
        plan = split["plan"] if split else state.get("spec", "")
        answer = PullRequestSizeDecision(interrupt(HumanPrompt(
            stage="pr_size",
            question=(
                f"The pull request has {size['lines']} changed lines. The limit is {PULL_REQUEST_LINES}.\n"
                f"{plan}\n"
                "Use --answer split to accept the split or --answer override to review this diff."
            ),
        ).model_dump()))
        if answer == PullRequestSizeDecision.OVERRIDE:
            return {"pr_size_override": size}
        return {}

    dispatch_review = bind("review")

    def moved_head_detail(actual: str, verified: str) -> str:
        return (
            f"HEAD is {actual}, verification checked {verified}. "
            "Reset the PR branch to the verified commit.\n"
            f"  git reset --keep {verified}\n"
            "Then rerun the same command. New commits need a new run."
        )

    def moved_before_review(actual: str, verified: str) -> DeliveryUpdate:
        return {**halted(Halt.merge_not_armed, moved_head_detail(actual, verified), "review"), "merge": MergeState(status="not_armed")}

    def review(state: DeliveryState, runtime: Runtime[LiveRun]) -> DeliveryUpdate:
        try:
            head = state.get("verified_head")
            if head is None:
                raise WorktreeInspectionFailed("The run did not record a verified HEAD.")
            worktree = run.workspace.inspect()
            if worktree.head != head:
                return moved_before_review(worktree.head, head)
            if worktree.branch is None:
                raise WorktreeInspectionFailed("HEAD is detached. Pull request sizing requires a checked-out branch.")
            repo = state["run"].repo
            base = run.forge.pull_request_base(repo, worktree.branch)
            size = run.workspace.pull_request_size(head, base)
            checked = run.workspace.inspect()
            if checked.head != head:
                return moved_before_review(checked.head, head)
            if not checked.clean or checked.branch != worktree.branch:
                raise WorktreeInspectionFailed("The worktree changed after verification.")
        except (ForgeError, WorktreeInspectionFailed) as exc:
            return halted(Halt.pr_size_unavailable, str(exc), "review")
        measured: DeliveryState = {**state, "pr_size": size}
        if oversized_pr(measured):
            return {"pr_size": size}
        return {**dispatch_review(measured, runtime), "pr_size": size}

    def after_triage(state: DeliveryState) -> str:
        if state.get("halt"):
            return "done"
        decision = state.get("triage")
        if decision == "needs_human":
            return "needs_human"
        if decision == "question":
            return "spec" if state.get("issue_kind") == "parent" else "research"
        return "prepare" if decision == "implement" else decision or "done"

    def after_spec(state: DeliveryState) -> str:
        if state.get("halt"):
            return "done"
        if state.get("spec_rejection"):
            return "spec"
        if oversized_pr(state):
            split = state.get("pr_split")
            if split and split["gate"] == "needs_human":
                return "needs_human"
            return "decide_pr_size"
        if state.get("spec_gate") == "needs_human":
            return "needs_human"
        if state.get("issue_kind") == "parent":
            return "done"
        return "prepare" if state.get("spec_gate") == "ready" else "done"

    def after_review(state: DeliveryState) -> str:
        if state.get("halt") == Halt.design_rethink:
            return "needs_human"
        if state.get("halt"):
            return "done"
        if oversized_pr(state):
            if state.get("pr_size_override"):
                return "decide_pr_size"
            return "spec"
        if state.get("review") == "approve":
            return "merge"
        if review_count(state) < config.max_review_rounds:
            return "prepare"
        return "done"

    def after_verify(state: DeliveryState) -> str:
        if state.get("halt"):
            return "done"
        if state.get("verify") == "pass":
            slice = _active_slice(state)
            if browser_required(slice) or formal_required(slice):
                return "proof"
            return "review"
        if failure_count(state, "verify_failures") < config.max_review_rounds:
            return "prepare"
        return "done"

    def after_proof(state: DeliveryState) -> str:
        if state.get("halt"):
            return "done"
        return "prepare" if state.get("proof_labels") else "review"

    def arm_merge(state: DeliveryState) -> DeliveryUpdate:
        run_state = state["run"]
        repo = run_state.repo
        head = state.get("verified_head")
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
        if head is None:
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
        receipt = state.get("review_receipt")
        past = history({"rounds": state.get("review_findings", [])})
        if (state.get("reviewed_head") != head or not receipt or receipt["commit_id"] != head
                or past.unresolved() or (past.gate and not past.gate.passed)):
            return merge_halted(Halt.merge_not_armed, "Renew review and its posted receipt at the verified head.")
        if previous := merge.receipt:
            saved = MergeReceipt.model_validate(previous)
            if saved.source_head == head:
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
        update: DeliveryUpdate = {"merge": merge.model_copy(update={
            "status": "armed", "receipt": merge_receipt, "observation": confirmed.model_dump(mode="json"),
        }), "path": ["merge"]}
        if confirmed.outcome != "ready":
            update.update(halted(READINESS_HALTS[confirmed.outcome], confirmed.detail(), "merge"))
        elif (confirmed.pr.number, confirmed.pr.head.sha, confirmed.pr.base.ref, confirmed.pr.state) != (observation.pr.number, observation.pr.head.sha, observation.pr.base.ref, observation.pr.state):
            update.update(halted(Halt.merge_not_armed, confirmed.detail(), "merge"))
        return update

    def finish(state: DeliveryState) -> DeliveryUpdate:
        reason = stop_reason(state)
        if not state.get("halt"):
            rounds = failure_count(state, "verify_failures") if reason == Halt.verify_cap else review_count(state)
            if reason == Halt.verify_cap:
                detail = f"Verification failed after {rounds} rounds. Check {state.get('check') or '(none)'} failed."
                return {**halted(Halt.verify_cap, detail, "done"), "stop": str(reason)}
            if reason == Halt.review_cap:
                detail = f"Review requested changes after {rounds} rounds. Last review: {state.get('detail', '')}"
                return {**halted(Halt.review_cap, detail, "done"), "stop": str(reason)}
            if reason == "needs_human":
                return {"stop": reason, "path": ["done"], "halt_detail":
                        state.get("human_question") or "What should Foreman know to continue?"}
        update: DeliveryUpdate = {"stop": str(reason), "path": ["done"]}
        split = state.get("pr_split")
        if reason == "sliced" and split:
            if "spec_candidate" in state:
                previous = SpecCandidate.from_record(state["spec_candidate"])
                model = state.get("models_used", {})["spec"]
                candidate = SpecCandidate(
                    slices=[CandidateSlice.model_validate(item) for item in split["slices"]],
                    spec=previous.spec, work_kind=previous.work_kind,
                    author=SpecAuthor(stage="spec", model=model, family=family(model)),
                )
                update = {**update, **candidate.update(single="slice" in state)}
            else:
                update["slices"] = split["slices"]
        if reason == "pr_split_required":
            update["halt_detail"] = split["plan"] if split else state.get("spec", "")
        return update

    builder = StateGraph[DeliveryState, LiveRun, DeliveryState, DeliveryResult](
        DeliveryState, input_schema=DeliveryState, output_schema=DeliveryResult, context_schema=LiveRun,
    )

    def after_halt(target: str) -> Callable[[DeliveryState], str]:
        def route(state: DeliveryState) -> str:
            return "done" if state.get("halt") else target
        return route

    def after_size(state: DeliveryState) -> str:
        return "done" if oversized_pr(state) else "verify"

    def add_agent_edges(stage: str, route: Callable[[DeliveryState], str], targets: dict[str, str]) -> None:
        def next_stage(state: DeliveryState) -> str:
            return "quota_wait" if state.get("halt") == Halt.quota_exhausted else route(state)

        def after_setup(state: DeliveryState) -> str:
            return "call" if state.get("pending_dispatch") and not state.get("halt") else next_stage(state)
        mapped: dict[str, str] = {**targets, "quota_wait": "quota_wait", "call": stage + "_call"}
        builder.add_conditional_edges(stage, after_setup, mapped)
        builder.add_conditional_edges(stage + "_call", after_setup, mapped)

    graph_nodes: dict[str, DeliveryNode] = {"implement": implement, "review": review}
    for name in MODEL_STAGES:
        builder.add_node(name, graph_nodes[name] if name in graph_nodes and name != "implement" else bind(name))
        builder.add_node(name + "_call", implement if name == "implement" else call(name))
    builder.add_node("verify", verify)
    builder.add_node("prepare", prepare)
    builder.add_node("reconcile", reconcile)
    builder.add_node("commit", commit)
    builder.add_node("publish", publish)
    builder.add_node("proof", prove)
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
        {"prepare": "prepare", "spec": "spec", "research": "research",
         "needs_human": "needs_human", "done": "done"},
    )
    add_agent_edges(
        "spec", after_spec,
        {"spec": "spec", "prepare": "prepare", "needs_human": "needs_human", "decide_pr_size": "decide_pr_size", "done": "done"},
    )
    builder.add_conditional_edges(
        "decide_pr_size", after_size, {"done": "done", "verify": "verify"},
    )
    builder.add_conditional_edges(
        "needs_human", lambda state: "done" if state.get("halt") else "implement" if _last_stage(state) == "review" else _last_stage(state),
        {"triage": "triage", "spec": "spec", "implement": "implement", "done": "done"},
    )
    builder.add_conditional_edges("prepare", after_halt("implement"), {"done": "done", "implement": "implement"})
    builder.add_conditional_edges("reconcile", after_halt("commit"), {"done": "done", "commit": "commit"})
    builder.add_conditional_edges("commit", after_halt("publish"), {"done": "done", "publish": "publish"})
    builder.add_conditional_edges("publish", after_halt("verify"), {"done": "done", "verify": "verify"})
    builder.add_conditional_edges(
        "quota_wait", _last_stage, {name: name for name in MODEL_STAGES},
    )
    add_agent_edges("research", lambda _: "done", {"done": "done"})
    add_agent_edges("implement", after_halt("reconcile"), {"reconcile": "reconcile", "done": "done"})
    add_agent_edges(
        "review",
        lambda state: after_review(state) if state.get("halt") not in (None, Halt.design_rethink) or oversized_pr(state) else "review_post",
        {"review_post": "review_post", "merge": "merge", "prepare": "prepare", "spec": "spec", "decide_pr_size": "decide_pr_size", "needs_human": "needs_human", "done": "done"},
    )
    builder.add_conditional_edges("review_post", after_review,
        {"merge": "merge", "prepare": "prepare", "spec": "spec", "decide_pr_size": "decide_pr_size", "needs_human": "needs_human", "done": "done"})
    builder.add_conditional_edges(
        "verify",
        after_verify,
        {"proof": "proof", "review": "review", "prepare": "prepare", "done": "done"},
    )
    builder.add_conditional_edges(
        "proof", after_proof, {"review": "review", "prepare": "prepare", "done": "done"},
    )
    builder.add_edge("merge", "done")
    builder.add_edge("done", END)
    # Each round visits at most all stages, each with one setup and every configured candidate,
    # plus prepare/commit/publish, proof, prompts, merge and done.
    steps = sum(1 + max(1, len(config.nodes[name].models)) for name in REQUIRED_NODES) + 12
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
    fact_record: str = MANIFEST_PATH,
    commands: list[list[str]] | None = None,
) -> DeliveryResult:
    dry = DryRun(commands=commands if commands is not None else [])
    state: DeliveryState = {
        "schema_version": SCHEMA_VERSION,
        "run": RunState(issue=dry.issue_placeholder, repo=repo, request=request, token_cap=max_tokens,
                        max_budget_usd=usd_text(max_budget_usd) if max_budget_usd is not None else None),
        "fact_record": fact_record_path(fact_record),
        "check": check,
        "issue_kind": "parent" if slice is None else "slice",
        "review_rounds": 0,
        "models_used": {},
        "path": [],
    }
    if slice is not None:
        state.update(load_slice(slice))
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
