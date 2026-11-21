"""The delivery graph. Edges live here. Models live in models.toml."""

import json
import operator
from uuid import uuid4
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from functools import cached_property
from graphlib import CycleError, TopologicalSorter
from pathlib import Path
from tomllib import TOMLDecodeError
from typing import Annotated, Literal, NotRequired, Protocol, get_args
import re
import textwrap

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from langgraph.types import interrupt
from pydantic import JsonValue, TypeAdapter, ValidationError
from typing_extensions import TypedDict

from foreman.adapters import Outcome
from foreman.config import Config, ConfigError, ModelSpec, family, load_config, order_for
from foreman.dispatch import FailoverDispatcher, InvalidOutput, QuotaExhausted
from foreman.fixture_manifest import MANIFEST_PATH, fact_record_path, recorded_evidence
from foreman.github import (
    MERGE_HOLD_VARIABLE, Forge, ForgeError, MergeHoldUnreadable, MergeNotArmed, PullRequest, ReviewComment, ReviewReceipt, merge_command, pull_request_command,
)
from foreman.proof import browser_proof_label, browser_required, formal_proof_label, formal_required
from foreman.skill_rules import SkillFailureReason, SkillMissing, SkillSource, export_skills, stage_methods
from foreman.redaction import redact
from foreman.stage_output import (
    CandidateSlice, SpecAuthor, SpecCandidate, SpecCandidateRecord, PendingDispatch, PendingDispatchRecord, DispatchPlan, DispatchPlanRecord, Attempt, AttemptRecord, AttemptUsage, Contract, UpdateContext, SpecOutput, PullRequestSplit, BrowserProofRecord, FormalProofRecord, SliceRecord, FindingRecord, MergeRiskRecord, StoredToolFact, GraphCheck as GraphEvidence, StageOutput, PULL_REQUEST_LINES, ReviewOutput, STAGE_OUTPUTS, Slice, StoredSlice, VerifyOutput, WorkKind, parse_output,
)
from foreman.readiness import Outcome as ReadinessOutcome
from foreman.review_round import AcceptedReview, GraphCheck, ReviewHistory, accept, history
from foreman.usd import BudgetUsd, usd_text
from foreman.workspace import ImplementationCandidate, ImplementationRejected, WorkspaceError, branch_command, commit_commands, push_command, CheckLog, PullRequestSize, ReviewScope, Workspace, WorktreeInspectionFailed, commit_sha, head_mismatch_detail

GRAPH_NAME = "delivery"
DEFAULT_TOKEN_CAP = 20_000_000
PACKAGE = Path(__file__).parent
REQUIRED_NODES = ("triage", "research", "spec", "implement", "verify", "review")
STAGE_ORDER = REQUIRED_NODES + ("merge",)
COAUTHOR_EMAILS = {"gpt": "noreply@openai.com", "claude": "noreply@anthropic.com", "grok": "noreply@x.ai"}
MergeHalt = Literal["merge_held", "merge_not_armed", "pr_conflict", "ci_pending", "ci_red", "readiness_unavailable"]
ResumableHalt = Literal[MergeHalt, "env_unavailable", "publish_failed", "git_failed", "implementation_rejected", "implement_quota", "quota_exhausted", "review_not_posted", "reviewer_unavailable"]
HaltReason = Literal[
    ResumableHalt, "failed", "no_dispatcher", "no_reviewer", "invalid_output", "cli_missing",
    "dirty_worktree", "insufficient_evidence", "review_cap", "verify_cap",
    "budget_exceeded", "usage_unknown", "design_rethink", "skill_missing", "missing_slice", "review_unavailable", "review_not_posted", "spec_cap", "pr_size_unavailable",
]
RESUMABLE_HALTS: frozenset[ResumableHalt] = frozenset(get_args(ResumableHalt))
READINESS_HALTS: dict[ReadinessOutcome, MergeHalt] = {
    "ready": "merge_not_armed", "conflict": "pr_conflict", "pending": "ci_pending", "red": "ci_red",
    "policy": "readiness_unavailable", "head_changed": "merge_not_armed", "closed": "merge_not_armed", "retargeted": "merge_not_armed",
}

CHECK_START_FAILURES = {126: "command is not executable", 127: "command not found"}


class HumanPrompt(Contract):
    stage: Literal["triage", "spec", "pr_size", "review"]
    question: str


class QuotaPause(Contract):
    reason: Literal["quota_exhausted"] = "quota_exhausted"
    detail: str | None


class WatchRecord(TypedDict):
    issue: int
    action: Literal["filed", "commented"]


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


@dataclass(frozen=True)
class DryRun:
    skills: SkillSource = export_skills

    def record_merge(self, repo: str, head: str | None) -> list[str]:
        revision = (commit_sha(head) or redact(head)) if head else "<sha>"
        return merge_command(redact(repo), "<number>", revision)


def entry_node(name: str) -> str:
    return name


def unknown_usage(stage: str, model: str | None) -> str:
    return f"{model} ran {stage} without a usage report"


def run_tokens(attempts: list[AttemptRecord]) -> int:
    return sum(usage["input"] + usage["output"] + usage["cache_read"] + usage["cache_creation"]
               for attempt in attempts if (usage := attempt["usage"]) is not None)


SpecRejectionKind = Literal["duplicate_title", "duplicate_builder", "dangling_seam", "dependency_cycle", "existing_seam_builder"]


class SpecRejection(TypedDict):
    kind: SpecRejectionKind
    detail: str


class InvalidSlicePlan(InvalidOutput):
    def __init__(self, kind: SpecRejectionKind, detail: str):
        super().__init__(detail)
        self.reason: SpecRejection = {"kind": kind, "detail": detail}


class PullRequestSizeDecision(StrEnum):
    SPLIT = "split"
    OVERRIDE = "override"


class DeliveryFields(TypedDict, total=False):
    max_budget_usd: str | None
    pending_dispatch: PendingDispatchRecord | None
    dispatch_plan: DispatchPlanRecord
    request: str
    issue: int
    workspace: str
    preflight: str
    check_exit_code: int
    triage: str
    spec: str
    spec_gate: str
    spec_candidate: SpecCandidateRecord
    spec_rejection: SpecRejection | None
    slices: list[SliceRecord]
    child_issue_numbers: list[int]
    slice: SliceRecord
    issue_kind: str
    work_kind: WorkKind
    pstack_pin: dict[str, str]
    skill_reason: SkillFailureReason
    attempts: Annotated[list[AttemptRecord], operator.add]
    research: str
    implementation: str
    delivery_branch: str
    delivery_base: str
    attempt_head: str
    implementation_candidate: ImplementationCandidate | None
    partial_edits: str
    commands: Annotated[list[list[str]], operator.add]
    head_sha: str | None
    verified_head: str
    repair_head: str | None
    readiness_observation: dict[str, JsonValue]
    merge_observed_head: str
    merge: Literal["armed", "skipped", "not_armed"]
    merge_command: list[str]
    browser_proof: BrowserProofRecord | None
    formal_proof: FormalProofRecord | None
    proof_labels: dict[str, str]
    human_question: str
    human_answers: list[str]
    pr_size: PullRequestSize
    pr_size_override: PullRequestSize
    pr_split: PullRequestSplit
    review: str
    review_findings: list[dict[str, object]]
    reviewed_head: str
    settled_rulings: list[dict[str, object]]
    fact_record: str
    findings: list[FindingRecord]
    detail: str
    review_change_kind: Literal["blocking", "nits"] | None
    risk: MergeRiskRecord
    verify: str
    review_rounds: int
    verify_failures: int
    proof_failures: int
    spec_failures: int
    review_run_id: str
    review_receipt: ReviewReceipt | None
    models_used: dict[str, str]
    implement_models: list[str]
    halt: HaltReason | None
    halt_detail: str


class DeliveryUpdate(DeliveryFields, total=False):
    token_cap: int
    stop: str
    repo: str
    check: str
    path: Annotated[list[str], operator.add]


class DeliveryState(DeliveryFields):
    token_cap: int
    stop: NotRequired[str]
    repo: str
    check: str
    path: Annotated[list[str], operator.add]


class DeliveryResult(DeliveryFields):
    token_cap: NotRequired[int]
    repo: str
    check: str
    path: Annotated[list[str], operator.add]
    stop: str


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


def halted(reason: HaltReason, detail: str, path: str | None) -> DeliveryUpdate:
    if not detail.strip():
        raise ValueError(f"{reason} requires a nonempty halt detail")
    return {"halt": reason, "halt_detail": detail, "path": [path] if path else []}


def budget_halt(state: DeliveryState, attempts: list[AttemptRecord], stage: str) -> DeliveryUpdate:
    last = attempts[-1]
    detail = (f"Run used {run_tokens(attempts)} tokens; cap is {state['token_cap']}. "
              f"Last attempt: {last['stage']} {last['model']} used {run_tokens([last])}. "
              "An in-flight attempt can overshoot the cap.")
    return halted("budget_exceeded", detail, stage)


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
    output: ReviewOutput, run: LiveRun | DryRun, state: DeliveryState,
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
    if isinstance(run, LiveRun) and head and outside:
        try:
            recorded = recorded_evidence(run.workspace, head, manifest_path)
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
    comments: list[ReviewComment] = []
    for finding in output.findings:
        message = f"[{finding.severity}] {finding.summary}"
        if (finding.file, finding.line) in scope.inline:
            comments.append({"path": finding.file, "line": finding.line, "side": "RIGHT", "body": message})
        else:
            body.append(f"{finding.file}:{finding.line} {message}")
    return "\n\n".join(body), comments


def stop_reason(state: DeliveryState) -> str:
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
        return "verify_cap"
    if state.get("review") == "changes":
        return "review_cap"
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
    dispatcher: FailoverDispatcher | None,
    config: Config,
    *,
    run: LiveRun | DryRun,
    scratch: Path | None = None,
    checkpointer: BaseCheckpointSaver[str] | None = None,
) -> CompiledStateGraph[DeliveryState, LiveRun, DeliveryState, DeliveryResult]:
    missing = [name for name in REQUIRED_NODES if name not in config.nodes]
    if missing:
        raise ValueError(f"delivery config is missing {', '.join(missing)}")
    methods_root = run.skills(config.pstack, scratch) if scratch is not None else None
    prompts = {name: stage_text(name) for name in REQUIRED_NODES}
    plan_workspace = run.workspace if isinstance(run, LiveRun) else None

    def bind(name: str) -> DeliveryNode:
        def node(state: DeliveryState, runtime: Runtime[LiveRun]) -> DeliveryUpdate:
            if run_tokens(state.get("attempts", [])) >= state["token_cap"]:
                return budget_halt(state, state.get("attempts", []), name)
            if dispatcher is None:
                return halted("no_dispatcher", "No dispatcher is configured.", name)
            current_run = run if isinstance(run, DryRun) else runtime.context or run
            if name == "implement" and isinstance(current_run, LiveRun):
                probe = current_run.environment or EnvironmentProbe(
                    current_run.workspace, state.get("preflight", ""), current_run.check_log,
                )
                if detail := probe.error:
                    return halted("env_unavailable", detail, name)
                try:
                    before = current_run.workspace.inspect()
                    branch, parent = state.get("delivery_branch"), state.get("attempt_head")
                    if before.branch != branch or before.head != parent:
                        return halted("dirty_worktree", "Restore the attempt's branch and starting HEAD before resuming edits.", "implement")
                    partial = state.get("partial_edits")
                    if not before.clean and partial and current_run.workspace.fingerprint() != partial:
                        return halted("dirty_worktree", "Edits changed after the quota stop; restore the run's partial edits.", "implement")
                except WorkspaceError as exc:
                    return halted("git_failed", str(exc), name)
            try:
                models = order_for(name, config, state.get("models_used") or {}, state.get("implement_models"))
            except ConfigError as exc:
                return halted("no_reviewer", str(exc), name)
            instruction = prompts[name]
            if isinstance(run, LiveRun) and methods_root is None:
                raise RuntimeError("Delivery execution requires a compiled skill export.")
            methods_text, methods = stage_methods(name, state, methods_root) if methods_root is not None else ("", [])
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
            if name == "verify":
                command = state.get("check") or "(none)"
                instruction = f"{instruction}\n\nCommand: {command}"
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
                    return halted("invalid_output", str(exc), name)
            review_context: tuple[Forge, PullRequest, ReviewScope] | None = None
            repo = state.get("repo", "")
            if name == "review" and isinstance(run, LiveRun):
                head, issue = state.get("verified_head"), state.get("issue")
                try:
                    worktree = run.workspace.inspect()
                    if not (repo and issue is not None and head and worktree.clean and worktree.branch and worktree.head == head):
                        raise WorktreeInspectionFailed("Review requires a clean branch at the verified HEAD.")
                    target = run.forge.review_target(repo, issue, worktree.branch, head)
                    diff = run.workspace.review_scope(target.baseRefOid, head, past.rounds[-1].reviewed_head if past.rounds else None)
                except (ForgeError, WorktreeInspectionFailed) as exc:
                    return halted("review_unavailable", str(exc), name)
                review_context = (run.forge, target, diff)
                instruction = f"{instruction}\n\nRead the reviewed diff with this command in the checkout:\n{diff.command}"
            if name == "review":
                scope = "Full PR review." if not past.rounds else "Check unresolved findings and added interdiff lines also in the PR diff."
                instruction += "\n\n" + scope
                instruction += "\nUnresolved findings:\n" + json.dumps([f.model_dump() for f in past.unresolved()])
                instruction += "\nGraph checks, not yours to resolve:\n" + json.dumps(past.rounds[-1].graph_check.model_dump(mode="json") if past.rounds and past.rounds[-1].graph_check else None)
                instruction += "\nSettled, don't reopen:\n" + json.dumps([r.model_dump() for r in past.rulings()])
            return {"verify_failures": failure_count(state, "verify_failures"),
                    "proof_failures": failure_count(state, "proof_failures"), "spec_failures": failure_count(state, "spec_failures"),
                    "review_run_id": state.get("review_run_id") or uuid4().hex, "dispatch_plan": DispatchPlan(
                models=[model.name for model in models], instruction=instruction, methods=methods,
                target=review_context[1] if review_context else None,
                scope=review_context[2].record() if review_context else None,
            ).record(), "pending_dispatch": PendingDispatch(stage=name, index=0, quota_model=None).record()}
        return node

    def call(name: str) -> DeliveryNode:
        def node(state: DeliveryState, runtime: Runtime[LiveRun]) -> DeliveryUpdate:
            assert dispatcher is not None
            assert "pending_dispatch" in state and "dispatch_plan" in state
            pending, plan = state["pending_dispatch"], state["dispatch_plan"]
            assert pending is not None
            models, methods = plan["models"], plan["methods"]
            attempts: list[AttemptRecord] = []
            def record_attempt(model: str | None, outcome: Outcome) -> None:
                attempts.append(Attempt(
                    stage=name, model=model, commit=config.pstack.commit, methods=methods, outcome=outcome.kind,
                    usage=None if outcome.usage is None else AttemptUsage(
                        input=outcome.usage.input_tokens, output=outcome.usage.output_tokens,
                        cache_read=outcome.usage.cache_read_input_tokens or 0,
                        cache_creation=outcome.usage.cache_creation_input_tokens or 0,
                        cost_usd=outcome.usage.total_cost_usd,
                    ),
                ).record())

            worker_state: DeliveryState = state.copy()
            if name == "review":
                for key in ("review_findings", "settled_rulings", *(("findings", "detail", "implementation") if history({"rounds": state.get("review_findings", []), "settled": state.get("settled_rulings", [])}).rounds else ())):
                    worker_state.pop(key, None)
            outcome, accepted_model = dispatcher.run(
                name, ModelSpec.parse(models[pending["index"]]) if models else None, worker_state, plan["instruction"], on_attempt=record_attempt,
            )
            model = accepted_model.name if accepted_model else None
            stopped: DeliveryUpdate = {"path": [name], "attempts": attempts, "pending_dispatch": None}
            if outcome.kind == "budget_exhausted":
                return {**stopped, **halted("budget_exceeded", f"{name} {model} exhausted its dollar budget. {outcome.detail}".strip(), name)}
            attempt_history = state.get("attempts", []) + attempts
            if outcome.kind == "ran" and outcome.usage is None:
                return {**stopped, **halted("usage_unknown", unknown_usage(name, model), name)}
            if run_tokens(attempt_history) >= state["token_cap"]:
                return {**stopped, **budget_halt(state, attempt_history, name)}
            index = pending["index"] + 1
            unavailable = ""
            if name == "review" and outcome.kind in {"quota", "cli_missing"}:
                prior = state.get("halt_detail", "") + "; " if pending["index"] else ""
                unavailable = f"{prior}review {model}: {outcome.kind}. {outcome.detail}".strip()
            if outcome.kind in {"quota", "cli_missing"} and index < len(models):
                retry: DeliveryUpdate = {"attempts": attempts, "pending_dispatch": PendingDispatch(
                    stage=name, index=index,
                    quota_model=model if outcome.kind == "quota" else pending["quota_model"],
                ).record()}
                if unavailable:
                    retry["halt_detail"] = unavailable
                return retry
            if unavailable:
                return {**stopped, **halted("reviewer_unavailable", unavailable, name)}
            if outcome.kind == "cli_missing" and pending["quota_model"]:
                outcome, model = Outcome("quota"), pending["quota_model"]
            if outcome.kind == "failed":
                return {**stopped, **halted("invalid_output", f"{name} {model}: {outcome.detail}", name)}
            if outcome.kind == "cli_missing":
                return {**stopped, **halted("cli_missing", outcome.detail, name)}
            if outcome.kind == "quota":
                if isinstance(run, DryRun):
                    raise QuotaExhausted(model)
                return {**stopped, **halted("quota_exhausted", f"{name} quota exhausted: {model}", name)}
            try:
                output = parse_output(STAGE_OUTPUTS[name], outcome.output)
                accepted: AcceptedReview | None = None
                if isinstance(output, ReviewOutput):
                    past = history({"rounds": state.get("review_findings", []), "settled": state.get("settled_rulings", [])})
                    head = state.get("verified_head") or state.get("head_sha") or ""
                    accepted = accept(past, output, head, _check_tool_facts(output, run, state))
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
                        update.update(halt="design_rethink", halt_detail=accepted.rethink, human_question=accepted.rethink)
            except InvalidSlicePlan as exc:
                rounds = failure_count(state, "spec_failures") + 1
                rejected: DeliveryUpdate = {**stopped, "spec_rejection": exc.reason, "spec_failures": rounds}
                if rounds >= config.max_review_rounds:
                    rejected.update(halted("spec_cap", str(exc), name))
                return rejected
            except InvalidOutput as exc:
                return {**stopped, **halted("invalid_output", f"{name} {model}: {exc}" if name == "review" else str(exc), name)}

            return update

        return node

    def post_review(state: DeliveryState) -> DeliveryUpdate:
        if not isinstance(run, LiveRun):
            return {}
        assert "dispatch_plan" in state and "review_findings" in state and "detail" in state and "review_run_id" in state
        plan = state["dispatch_plan"]
        assert plan["target"] is not None and plan["scope"] is not None
        past = history({"rounds": state["review_findings"], "settled": state.get("settled_rulings", [])})
        accepted = AcceptedReview(history=past, findings=past.unresolved(), detail=state["detail"])
        body, comments = review_submission(accepted, TypeAdapter(ReviewScope).validate_python(plan["scope"]))
        target = PullRequest.model_validate(plan["target"])
        marker = f"{state['review_run_id']}:{len(past.rounds)}:{target.headRefOid}"
        try:
            receipt = run.forge.post_review(state["repo"], target, body, comments, marker=marker)
        except ForgeError as exc:
            return halted("review_not_posted", str(exc), "review_post")
        return {"review_receipt": receipt}

    scripted_verify = bind("verify")

    def prepare(state: DeliveryState) -> DeliveryUpdate:
        assert "slice" in state and "repo" in state
        title = redact(str(state["slice"]["title"]))
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60].rstrip("-") or "change"
        branch = state.get("delivery_branch") or f"{state.get('issue', '<issue>')}-{slug}"
        if isinstance(run, DryRun):
            base = state.get("delivery_base", "<default-branch>")
            commands = [] if "delivery_branch" in state else [branch_command(branch, base)]
            return {"delivery_branch": branch, "delivery_base": base, "commands": commands}
        try:
            before = run.workspace.inspect()
            if not before.clean:
                return halted("dirty_worktree", "Unowned edits exist before this attempt; save them before rerunning.", "prepare")
            base = state.get("delivery_base") or run.forge.default_branch(state["repo"])
            run.workspace.create_branch(branch, base)
            if repair_head := state.get("repair_head"):
                run.workspace.refresh(branch, repair_head)
            ready = run.workspace.inspect()
            return {"delivery_branch": branch, "delivery_base": base, "attempt_head": ready.head, "repair_head": None, "partial_edits": "", "implementation_candidate": None}
        except (WorkspaceError, ForgeError) as exc:
            return halted("git_failed", str(exc), "prepare")

    dispatch_implement = call("implement")

    def implement(state: DeliveryState, runtime: Runtime[LiveRun]) -> DeliveryUpdate:
        if isinstance(run, DryRun):
            return dispatch_implement(state, runtime)
        assert "delivery_branch" in state and "attempt_head" in state
        assert "pending_dispatch" in state and state["pending_dispatch"] is not None and "dispatch_plan" in state
        update: DeliveryUpdate = {}
        try:
            try:
                update = dispatch_implement(state, runtime)
            except QuotaExhausted as exc:
                return {**halted("implement_quota", f"Implement quota exhausted for {exc.model}; rerun to continue partial edits.", "implement"),
                        "partial_edits": run.workspace.fingerprint(), "pending_dispatch": None,
                        "attempts": [Attempt(stage="implement", model=exc.model,
                                             commit=config.pstack.commit, methods=state["dispatch_plan"]["methods"],
                                             outcome="quota", usage=None).record()]}
            if update.get("pending_dispatch"):
                return update
            return {**update, "partial_edits": run.workspace.fingerprint() if update.get("halt") == "quota_exhausted" else ""}
        except WorkspaceError as exc:
            return {**update, **halted("git_failed", str(exc), "implement")}

    def reconcile(state: DeliveryState) -> DeliveryUpdate:
        if isinstance(run, DryRun):
            return {}
        assert "attempt_head" in state and "delivery_branch" in state
        try:
            candidate = run.workspace.reconcile(state["attempt_head"], state["delivery_branch"])
            if candidate is None:
                identifiers = [str(f.get("id")) for f in state.get("findings", [])]
                if (gate := history({"rounds": state.get("review_findings", [])}).gate) and not gate.passed:
                    identifiers.insert(0, "tool-facts")
                detail = "no changes" + ("; unresolved findings: " + ", ".join(identifiers) if identifiers else "")
                return halted("failed", detail, "reconcile")
            return {"implementation_candidate": candidate}
        except ImplementationRejected as exc:
            return halted("implementation_rejected", str(exc), "reconcile")
        except WorkspaceError as exc:
            return halted("git_failed", str(exc), "reconcile")

    def commit(state: DeliveryState) -> DeliveryUpdate:
        assert "slice" in state
        subject = f"{state.get('issue', '<issue>')}: {' '.join(redact(str(state['slice']['title'])).split())}"
        subject = textwrap.shorten(subject, width=72, placeholder="")
        model = state.get("models_used", {}).get("implement")
        if not model:
            return halted("no_reviewer", "Publication needs the implement model.", None)
        message = f"{subject}\n\nCo-Authored-By: {redact(model)} <{COAUTHOR_EMAILS[family(model)]}>"
        if isinstance(run, DryRun):
            return {"commands": commit_commands(message)}
        assert "delivery_branch" in state and "attempt_head" in state
        try:
            observed = run.workspace.inspect()
            if observed.branch != state["delivery_branch"]:
                raise WorkspaceError("Restore the delivery branch before committing.")
            candidate = state.get("implementation_candidate")
            assert candidate is not None
            if candidate["disposition"] == "adopted" and observed.head == candidate["head"]:
                if not observed.clean:
                    raise WorkspaceError("HEAD differs from the reconciled worker commit.")
                run.workspace.soft_reset(state["attempt_head"])
                observed = run.workspace.inspect()
            if observed.head != state["attempt_head"]:
                if not observed.clean or not run.workspace.committed(state["attempt_head"], message):
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
            return {"head_sha": observed.head}
        except WorkspaceError as exc:
            return halted("git_failed", str(exc), "commit")

    def publish(state: DeliveryState) -> DeliveryUpdate:
        assert "delivery_branch" in state and "delivery_base" in state and "repo" in state and "slice" in state
        branch, base, repo = state["delivery_branch"], state["delivery_base"], state["repo"]
        title = " ".join(redact(str(state["slice"]["title"])).split())
        body = f"Closes #{state.get('issue', '<issue>')}"
        if isinstance(run, DryRun):
            return {"path": ["publish"], "commands": [push_command("origin", branch), pull_request_command(repo, branch, base, title, body)]}
        try:
            observed = run.workspace.inspect()
            if not observed.clean or observed.branch != branch or observed.head != state.get("head_sha"):
                raise WorkspaceError("Restore the clean delivery branch at its implementation commit before publishing.")
            run.workspace.push(branch, repo)
            run.forge.open_pull_request(repo, branch, base, title, body)
        except (WorkspaceError, ForgeError) as exc:
            return halted("publish_failed", str(exc), "publish")
        return {"path": ["publish"]}

    def verify(state: DeliveryState, runtime: Runtime[LiveRun]) -> DeliveryUpdate:
        match run if isinstance(run, DryRun) else runtime.context or run:
            case DryRun():
                return scripted_verify(state, runtime)
            case LiveRun(workspace=workspace, check_log=check_log):
                try:
                    worktree = workspace.inspect()
                except WorktreeInspectionFailed as exc:
                    return halted("dirty_worktree", str(exc), "verify")
                if not worktree.clean:
                    return halted("dirty_worktree", "The worktree has changes.", "verify")
                command = state["check"]
                exit_code = workspace.run(command, log=check_log)
                detail = None
                if reason := CHECK_START_FAILURES.get(exit_code):
                    detail = f"Check {command!r} exited with {exit_code} ({reason})"
                elif exit_code:
                    detail = failed_preflight(workspace, state.get("preflight", ""), log=check_log)
                if detail:
                    return {**halted("env_unavailable", detail, "verify"), "check_exit_code": exit_code}
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
            update.update(halted("insufficient_evidence", "Required proof is missing.", "proof"))
        elif "block" in labels.values():
            update["proof_failures"] = failure_count(state, "proof_failures") + 1
            if update["proof_failures"] >= config.max_review_rounds:
                detail = f"Proof failed after {update['proof_failures']} rounds: {update['proof_labels']}."
                update.update(halted("review_cap", detail, "proof"))
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
        return {**halted("merge_not_armed", moved_head_detail(actual, verified), "review"), "merge": "not_armed"}

    def review(state: DeliveryState, runtime: Runtime[LiveRun]) -> DeliveryUpdate:
        if isinstance(run, DryRun):
            return dispatch_review(state, runtime)
        try:
            head = state.get("verified_head")
            if head is None:
                raise WorktreeInspectionFailed("The run did not record a verified HEAD.")
            worktree = run.workspace.inspect()
            if worktree.head != head:
                return moved_before_review(worktree.head, head)
            if worktree.branch is None:
                raise WorktreeInspectionFailed("HEAD is detached. Pull request sizing requires a checked-out branch.")
            repo = state.get("repo")
            assert repo is not None
            base = run.forge.pull_request_base(repo, worktree.branch)
            size = run.workspace.pull_request_size(head, base)
            checked = run.workspace.inspect()
            if checked.head != head:
                return moved_before_review(checked.head, head)
            if not checked.clean or checked.branch != worktree.branch:
                raise WorktreeInspectionFailed("The worktree changed after verification.")
        except (ForgeError, WorktreeInspectionFailed) as exc:
            return halted("pr_size_unavailable", str(exc), "review")
        measured: DeliveryState = {**state, "pr_size": size}
        if oversized_pr(measured):
            return {"pr_size": size}
        return {**dispatch_review(measured, runtime), "pr_size": size}

    def after_triage(state: DeliveryState) -> str:
        if state.get("halt"):
            return "done"
        decision = state.get("triage")
        if decision == "needs_human":
            return "needs_human" if checkpointer is not None else "done"
        if decision == "question":
            return "spec" if state.get("issue_kind") == "parent" else "research"
        return "prepare" if decision == "implement" else decision or "done"

    def after_spec(state: DeliveryState) -> str:
        if state.get("halt"):
            return "done"
        if state.get("spec_rejection"):
            return "spec"
        if oversized_pr(state):
            if checkpointer is None:
                return "done"
            split = state.get("pr_split")
            if split and split["gate"] == "needs_human":
                return "needs_human"
            return "decide_pr_size"
        if state.get("spec_gate") == "needs_human" and checkpointer is not None:
            return "needs_human"
        if state.get("issue_kind") == "parent":
            return "done"
        return "prepare" if state.get("spec_gate") == "ready" else "done"

    def after_review(state: DeliveryState) -> str:
        if state.get("halt") == "design_rethink" and checkpointer is not None:
            return "needs_human"
        if state.get("halt"):
            return "done"
        if oversized_pr(state):
            if state.get("pr_size_override"):
                return "decide_pr_size" if checkpointer is not None else "done"
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
        repo = state["repo"]
        head = state.get("verified_head")

        def merge_halted(halt: MergeHalt, detail: str) -> DeliveryUpdate:
            return {
                **halted(halt, detail, "merge"), "merge": "not_armed",
                "merge_command": merge_command(repo, "<number>", head or "<sha>"),
            }

        match run:
            case DryRun():
                return {
                    "merge": "skipped", "path": ["merge"],
                    "merge_command": run.record_merge(repo, state.get("head_sha")),
                }
            case LiveRun(forge=forge, workspace=workspace):
                if head is None:
                    return merge_halted("merge_not_armed", "The run did not record a verified HEAD.")
                try:
                    worktree = workspace.inspect()
                except WorktreeInspectionFailed as exc:
                    return merge_halted("merge_not_armed", str(exc))
                if not worktree.clean:
                    return merge_halted("merge_not_armed", "The worktree has changes or Git could not inspect it.")
                if worktree.head != head:
                    return {
                        **merge_halted("merge_not_armed", head_mismatch_detail(worktree.head, head)),
                        "merge_observed_head": worktree.head,
                    }
                if worktree.branch is None:
                    return merge_halted("merge_not_armed", "HEAD is detached. Merge requires a checked-out branch.")
                try:
                    hold = forge.merge_hold(repo)
                except MergeHoldUnreadable as exc:
                    return merge_halted("merge_held", f"could not read {MERGE_HOLD_VARIABLE}: {exc}")
                if hold:
                    return merge_halted("merge_held", f"{MERGE_HOLD_VARIABLE} is set" + (f": {hold}" if hold.strip() else "."))
                issue = state.get("issue")
                if issue is None:
                    return merge_halted("merge_not_armed", "Live merge requires an issue number.")
                receipt = state.get("review_receipt")
                past = history({"rounds": state.get("review_findings", [])})
                if (state.get("reviewed_head") != head or not receipt or receipt["commit_id"] != head
                        or past.unresolved() or (past.gate and not past.gate.passed)):
                    return merge_halted("merge_not_armed", "Renew review and its posted receipt at the verified head.")
                try:
                    observation = forge.readiness(repo, issue, worktree.branch, head)
                except ForgeError as exc:
                    return merge_halted("readiness_unavailable", f"Platform/policy observation unavailable: {exc}")
                if observation.outcome != "ready":
                    return {**merge_halted(READINESS_HALTS[observation.outcome], observation.detail()),
                            "readiness_observation": observation.model_dump(mode="json")}
                try:
                    forge.arm_auto_merge(repo, issue, worktree.branch, head)
                except MergeNotArmed as exc:
                    return merge_halted("merge_not_armed", str(exc))
                try:
                    confirmed = forge.readiness(repo, issue, worktree.branch, head)
                except ForgeError as exc:
                    return {**merge_halted("readiness_unavailable", str(exc)), "merge": "armed"}
                update: DeliveryUpdate = {"merge": "armed", "path": ["merge"], "readiness_observation": confirmed.model_dump(mode="json")}
                if confirmed.outcome != "ready":
                    update.update(halted(READINESS_HALTS[confirmed.outcome], confirmed.detail(), "merge"))
                elif (confirmed.pr.number, confirmed.pr.head.sha, confirmed.pr.base.ref, confirmed.pr.state) != (observation.pr.number, observation.pr.head.sha, observation.pr.base.ref, observation.pr.state):
                    update.update(halted("merge_not_armed", confirmed.detail(), "merge"))
                return update

    def finish(state: DeliveryState) -> DeliveryUpdate:
        reason = stop_reason(state)
        if not state.get("halt"):
            rounds = failure_count(state, "verify_failures") if reason == "verify_cap" else review_count(state)
            if reason == "verify_cap":
                detail = f"Verification failed after {rounds} rounds. Check {state.get('check') or '(none)'} failed."
                return {**halted("verify_cap", detail, "done"), "stop": reason}
            if reason == "review_cap":
                detail = f"Review requested changes after {rounds} rounds. Last review: {state.get('detail', '')}"
                return {**halted("review_cap", detail, "done"), "stop": reason}
            if reason == "needs_human":
                return {"stop": reason, "path": ["done"], "halt_detail":
                        state.get("human_question") or "What should Foreman know to continue?"}
        update: DeliveryUpdate = {"stop": reason, "path": ["done"]}
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
            return "quota_wait" if state.get("halt") == "quota_exhausted" else route(state)

        def after_setup(state: DeliveryState) -> str:
            return "call" if state.get("pending_dispatch") and not state.get("halt") else next_stage(state)
        mapped: dict[str, str] = {**targets, "quota_wait": "quota_wait", "call": stage + "_call"}
        builder.add_conditional_edges(stage, after_setup, mapped)
        builder.add_conditional_edges(stage + "_call", after_setup, mapped)

    graph_nodes: dict[str, DeliveryNode] = {"implement": implement, "verify": verify, "review": review}
    for name in REQUIRED_NODES:
        builder.add_node(name, graph_nodes[name] if name in graph_nodes and name != "implement" else bind(name))
        builder.add_node(name + "_call", implement if name == "implement" else call(name))
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
        "quota_wait", _last_stage, {name: name for name in REQUIRED_NODES if name != "verify"},
    )
    add_agent_edges("research", lambda _: "done", {"done": "done"})
    add_agent_edges("implement", after_halt("reconcile"), {"reconcile": "reconcile", "done": "done"})
    add_agent_edges(
        "review",
        lambda state: after_review(state) if state.get("halt") not in (None, "design_rethink") or oversized_pr(state) else "review_post",
        {"review_post": "review_post", "merge": "merge", "prepare": "prepare", "spec": "spec", "decide_pr_size": "decide_pr_size", "needs_human": "needs_human", "done": "done"},
    )
    builder.add_conditional_edges("review_post", after_review,
        {"merge": "merge", "prepare": "prepare", "spec": "spec", "decide_pr_size": "decide_pr_size", "needs_human": "needs_human", "done": "done"})
    add_agent_edges(
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
    dispatcher: FailoverDispatcher,
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
) -> DeliveryResult:
    state: DeliveryState = {
        "repo": repo,
        "token_cap": max_tokens,
        "max_budget_usd": usd_text(max_budget_usd) if max_budget_usd is not None else None,
        "fact_record": fact_record_path(fact_record),
        "request": request,
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
        graph = compile_delivery(dispatcher, config, run=DryRun(skills=skills), scratch=scratch)
    except SkillMissing as exc:
        return read_delivery_state({**state, **halted("skill_missing", str(exc), "done"), "stop": exc.halt,
                                    "skill_reason": exc.reason})
    return read_delivery_state(graph.invoke(state, version="v2").value)


def diagram(dispatcher: FailoverDispatcher | None = None, config: Config | None = None) -> str:
    if dispatcher is None:
        dispatcher = FailoverDispatcher(
            lambda node, model, state, instruction: Outcome(
                "ran", {"triage": "implement", "slice": None, "human_question": None},
            ),
        )
    graph = compile_delivery(dispatcher, config if config is not None else load_config(), run=DryRun())
    return graph.get_graph().draw_mermaid()
