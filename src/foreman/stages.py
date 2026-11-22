"""Canonical worker contracts, methods and callbacks. Graph successors stay in delivery."""

from collections.abc import Callable
from dataclasses import dataclass
from graphlib import CycleError, TopologicalSorter
import json
from typing import Literal, TYPE_CHECKING

from foreman.config import Config, ModelSpec, order_for, contributors_for
from foreman.skill_rules import PstackMethod, SkillRule, principle
from foreman.stage_output import (
    StageOutput, TriageOutput, ResearchOutput, SpecOutput, SpecCheckOutput,
    ImplementOutput, ReviewOutput, VerifyOutput, UpdateContext, Slice, SliceRecord,
    SpecRejection, SpecRejectionKind, PULL_REQUEST_LINES, AttemptRecord, ReviewSelection,
)

from foreman.dispatch import InvalidOutput
from foreman.review_round import history, accept, graph_check, check_tool_facts
from foreman.workspace import WorkState, Workspace, PullRequestSize, ReviewScope, WorktreeInspectionFailed
from foreman.github import Forge, PullRequest
from foreman.review_context import ReviewContext
from foreman.adapters import Outcome
from foreman.outcomes import Halt, halted

if TYPE_CHECKING:
    from foreman.state import DeliveryState, DeliveryUpdate


@dataclass(frozen=True)
class Acceptance:
    state: "DeliveryState"
    model: ModelSpec | str | None = None
    workspace: Workspace | None = None
    config: Config | None = None
    attempt: AttemptRecord | None = None


class InvalidSlicePlan(InvalidOutput):
    def __init__(self, kind: SpecRejectionKind, detail: str):
        super().__init__(detail)
        self.reason: SpecRejection = {"kind": kind, "detail": detail}


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


def oversized_pr(state: "DeliveryState") -> PullRequestSize | None:
    size = state.get("pr_size")
    if size is not None and size["lines"] > PULL_REQUEST_LINES and size != state.get("pr_size_override"):
        return size
    return None


def no_preparation(state: "DeliveryState", workspace: Workspace, forge: Forge) -> tuple[str, PullRequest | None, ReviewScope | None]:
    return "", None, None


def implement_instructions(state: "DeliveryState", workspace: Workspace, forge: Forge) -> tuple[str, PullRequest | None, ReviewScope | None]:
    review_state = state["review"]
    record = review_state.fact_record
    instruction = (f"\n\nFact record in this checkout: {record}" if record is not None else
                   "\n\nNo fact record was declared.")
    gate = history({"rounds": review_state.review_findings}).gate if review_state.review == "changes" else None
    if gate and not gate.passed:
        if record is None:
            instruction += ("\nRecording evidence requires a new run with an explicit --fact-record and a new checkpoint."
                            " Remove the unsupported dependency if possible; carried claims remain subject to the gate.")
        else:
            instruction += (
                "\nResolve the tool-facts finding by recording evidence and creating the record and parent directories if absent."
                " Fixture IDs are paths relative to the record directory. Write regular evidence files."
                " Use TOML [fixtures.\"<evidence-file>\"] with tool and version, plus either"
                " kind = \"captured_run\" and argv = [\"tool\", \"arg\"] for captured output, or"
                " kind = \"quoted_source\" and sources = [\"https://source\"] for quoted source evidence."
                ' Add claim_ids = ["<claim_id>"] to the evidence entry for each carried claim.'
                " Support each carried tool fact; the graph rechecks the published verified head."
            )
        instruction += "\nCarried tool facts:\n" + json.dumps([fact.model_dump() for fact in gate.tool_facts])
    return instruction, None, None


def spec_instructions(state: "DeliveryState", workspace: Workspace, forge: Forge) -> tuple[str, PullRequest | None, ReviewScope | None]:
    if size := oversized_pr(state):
        return (
            f"\n\nThe pull request has {size['lines']} changed lines. The limit is {PULL_REQUEST_LINES}."
            " Split the work into independently shippable pull requests."
            " For a slice issue, describe the split in spec."
        ), None, None
    return "", None, None


def accept_output(output: StageOutput, context: Acceptance) -> "DeliveryUpdate":
    state, model = context.state, context.model
    return {**output.update(UpdateContext(state["plan"], state["review"], bool(oversized_pr(state)), [], model if isinstance(model, ModelSpec) else None))}


def accept_spec(output: StageOutput, context: Acceptance) -> "DeliveryUpdate":
    state, model, workspace = context.state, context.model, context.workspace
    assert isinstance(output, SpecOutput)
    plan = state["plan"]
    slices = clean_slices(output.slices, workspace) if plan.issue_kind == "parent" and output.spec_gate == "ready" else []
    return {**output.update(UpdateContext(plan, state["review"], bool(oversized_pr(state)), slices, model if isinstance(model, ModelSpec) else None))}


def accept_implement(output: StageOutput, context: Acceptance) -> "DeliveryUpdate":
    state, model = context.state, context.model
    assert isinstance(output, ImplementOutput)
    update: DeliveryUpdate = {"implementation": output.implementation, "checks": state["checks"].model_copy(update={
        "browser_proof": output.browser_proof.record() if output.browser_proof else None,
        "formal_proof": output.formal_proof.record() if output.formal_proof else None,
    })}
    if model:
        latest = state.get("models_used", {}).get("implement")
        history = state.get("implement_models", [latest] if latest else [])
        update["implement_models"] = [*history, model.name if isinstance(model, ModelSpec) else model]
    return update


def accept_verify(output: StageOutput, context: Acceptance) -> "DeliveryUpdate":
    state = context.state
    assert isinstance(output, VerifyOutput)
    checks = state["checks"]
    return {"checks": checks.model_copy(update={"verify": output.verify,
        "verify_failures": checks.verify_failures + (output.verify == "fail")})}


def accept_review(output: StageOutput, context: Acceptance) -> "DeliveryUpdate":
    state, model, workspace, config = context.state, context.model, context.workspace, context.config
    assert isinstance(output, ReviewOutput) and workspace is not None
    review_state = state["review"]
    past = history({"rounds": review_state.review_findings, "settled": review_state.settled_rulings})
    head = state["checks"].verified_head or state.get("work", WorkState()).head_sha or ""
    accepted = accept(past, output, head, check_tool_facts(output, workspace, state))
    if isinstance(model, ModelSpec) and config is not None:
        assert "dispatch_plan" in state
        contributors, families = contributors_for("review", config, state.get("models_used", {}), state.get("implement_models"))
        same_family = model.family in families
        other_configured = any(ModelSpec.parse(candidate).family not in families for candidate in state["dispatch_plan"]["models"])
        provenance = ReviewSelection(
            reviewer=model.name, contributors=contributors, same_family=same_family,
            reason=("preceding_candidates_unavailable" if other_configured else "no_other_family_configured") if same_family else None,
        )
        accepted.history.rounds[-1].provenance = provenance
    update: DeliveryUpdate = {"review": review_state.model_copy(update={"risk": output.risk.record(), "review": accepted.review, "detail": accepted.detail,
                  "review_findings": [r.model_dump(mode="json") for r in accepted.history.rounds],
                  "reviewed_head": accepted.history.rounds[-1].reviewed_head,
                  "findings": [f.record() for f in accepted.findings],
                  "review_change_kind": accepted.change_kind, "review_receipt": None})}
    if context.attempt is not None and (selection := accepted.history.rounds[-1].provenance) is not None:
        update["attempts"] = [{**context.attempt, "provenance": selection.record()}]
    if "halt_detail" in state:
        update["halt_detail"] = ""
    if accepted.rethink:
        update.update(halted(Halt.design_rethink, accepted.rethink, "review"))
        update["human_question"] = accepted.rethink
    return update


def review_preparation(state: "DeliveryState", workspace: Workspace, forge: Forge) -> tuple[str, PullRequest | None, ReviewScope | None]:
    review_state, checks = state["review"], state["checks"]
    record = review_state.fact_record
    instruction = (f"\n\nFact record in this checkout: {record}" if record is not None else
                   "\n\nNo fact record was declared.")
    if record is None:
        instruction += " Use null fixture references for unsupported tool facts."
    if (proven := graph_check(state)) is not None:
        instruction += f"\nGraph fact: {proven.claim}\nGraph check evidence: {proven.model_dump_json()}"
    past = history({"rounds": review_state.review_findings, "settled": review_state.settled_rulings})
    run_state = state["run"]
    repo, head, issue = run_state.repo, checks.verified_head, run_state.issue
    worktree = workspace.inspect()
    if not (repo and issue is not None and head and worktree.clean and worktree.branch and worktree.head == head):
        raise WorktreeInspectionFailed("Review requires a clean branch at the verified HEAD.")
    target = forge.review_target(repo, issue, worktree.branch, head)
    diff = workspace.review_scope(target.baseRefOid, head, past.rounds[-1].reviewed_head if past.rounds else None)
    instruction += f"\n\nRead the labeled review diffs with these commands in the checkout:\n{diff.command}"
    scope = "Full PR review." if not past.rounds else "Check unresolved findings and added interdiff lines also in the PR diff."
    instruction += "\n\n" + scope
    instruction += "\nUnresolved findings:\n" + json.dumps([f.model_dump() for f in past.unresolved()])
    instruction += "\nGraph checks, not yours to resolve:\n" + json.dumps(past.rounds[-1].graph_check.model_dump(mode="json") if past.rounds and past.rounds[-1].graph_check else None)
    instruction += "\nSettled, don't reopen:\n" + json.dumps([r.model_dump() for r in past.rulings()])
    return instruction, target, diff


def review_projection(state: "DeliveryState", workspace: Workspace) -> tuple["DeliveryState", ReviewContext | None]:
    assert "dispatch_plan" in state
    review_state, plan = state["review"], state["dispatch_plan"]
    worker_state = state.copy()
    worker_state["review"] = review_state.model_copy(update={
        "review_findings": [], "settled_rulings": [],
        **({"findings": [], "detail": ""} if review_state.review_findings else {}),
    })
    if review_state.review_findings:
        worker_state.pop("implementation", None)
    assert plan["target"] is not None
    target = plan["target"]
    context = workspace.review_context(target["baseRefOid"], target["headRefOid"])
    if not (context.base.changed or context.head.changed):
        raise WorktreeInspectionFailed("Review requires changed paths.")
    return worker_state, context


def retry_denied_review(attempts: list[AttemptRecord]) -> bool:
    denied = 0
    for attempt in reversed(attempts):
        if attempt["stage"] != "review" or attempt["outcome"] == "ran":
            break
        denied += attempt["outcome"] == "review_denied"
    return denied == 1


def review_unavailable(state: "DeliveryState", outcome: Outcome, model: str | None) -> tuple[Halt, str] | None:
    if outcome.kind not in {"quota", "cli_missing"}:
        return None
    pending = state.get("pending_dispatch")
    assert pending is not None
    prior = state.get("halt_detail", "") + "; " if pending["index"] else ""
    return Halt.reviewer_unavailable, f"{prior}review {model}: {outcome.kind}. {outcome.detail}".strip()


@dataclass(frozen=True)
class Stage:
    prompt: str | None
    output: type[StageOutput] | None
    skills: tuple[SkillRule, ...] = ()
    kind: Literal["model", "dormant", "deterministic"] = "model"
    input_fields: tuple[str, ...] = ()
    prepare: Callable[["DeliveryState", Workspace, Forge], tuple[str, PullRequest | None, ReviewScope | None]] = no_preparation
    accept: Callable[[StageOutput, Acceptance], "DeliveryUpdate"] = accept_output
    project: Callable[["DeliveryState", Workspace], tuple["DeliveryState", ReviewContext | None]] = lambda state, workspace: (state.copy(), None)
    retry_denied: Callable[[list[AttemptRecord]], bool] = lambda attempts: False
    unavailable: Callable[["DeliveryState", Outcome, str | None], tuple[Halt, str] | None] = lambda state, outcome, model: None

    models: Callable[[str, "DeliveryState", Config], list[ModelSpec]] = lambda name, state, config: order_for(
        name, config, state.get("models_used") or {}, state.get("implement_models"))


STAGES = {
    "triage": Stage("triage.md", TriageOutput, (
        SkillRule(skill=PstackMethod(path="poteto-mode/SKILL.md", kind="skill", lines=(110, 110))),
    ), input_fields=("run", "plan", "human_question", "human_answers")),
    "research": Stage("research.md", ResearchOutput, (
        SkillRule(skill=PstackMethod(path="how/SKILL.md", kind="leaf_prompt", lines=(17, 17))),
        SkillRule(skill=PstackMethod(path="why/SKILL.md", kind="skill"), when="bug_fix"),
    ), input_fields=("run", "plan")),
    "spec": Stage("spec.md", SpecOutput, (
        principle("redesign-from-first-principles"),
        principle("subtract-before-you-add"),
        principle("model-the-domain"),
        principle("boundary-discipline"),
        principle("type-system-discipline"),
        SkillRule(skill=PstackMethod(path="statechart/SKILL.md", kind="skill"), when="stateful"),
    ), prepare=spec_instructions, accept=accept_spec, input_fields=("run", "plan", "pr_size", "pr_size_override", "human_question", "human_answers")),
    "spec_check": Stage("spec_check.md", SpecCheckOutput, (
        principle("model-the-domain"),
        principle("boundary-discipline"),
        principle("type-system-discipline"),
        principle("prove-it-works"),
    ), kind="dormant", input_fields=("run", "plan")),
    "implement": Stage("implement.md", ImplementOutput, (
        principle("laziness-protocol"),
        principle("test-behavior-not-implementation"),
        principle("prove-it-works"),
        SkillRule(skill=PstackMethod(path="tdd/SKILL.md", kind="skill"), when="bug_fix"),
        SkillRule(skill=PstackMethod(path="benchmark-checklist/SKILL.md", kind="skill"), when="perf_issue"),
        SkillRule(skill="browser-proof", when="browser"),
        SkillRule(skill="formal-proof", when="formal"),
    ), prepare=implement_instructions, accept=accept_implement, input_fields=("run", "plan", "work", "checks", "review")),
    "review": Stage("review.md", ReviewOutput, (
        principle("boundary-discipline"),
        principle("model-the-domain"),
        principle("prove-it-works"),
        principle("test-behavior-not-implementation"),
        SkillRule(skill="review-quality"),
        SkillRule(skill="merge-risk"),
    ), prepare=review_preparation, accept=accept_review, project=review_projection,
       retry_denied=retry_denied_review, unavailable=review_unavailable, input_fields=("run", "plan", "work", "checks", "review", "implementation")),
    "verify": Stage(None, VerifyOutput, accept=accept_verify, kind="deterministic", input_fields=("checks",)),
    "proof": Stage(None, None, kind="deterministic", input_fields=("plan", "checks", "review")),
}
