"""Canonical worker contracts, methods and callbacks. Graph successors stay in delivery."""

from collections.abc import Callable
from dataclasses import dataclass
from graphlib import CycleError, TopologicalSorter
import json
from typing import Literal, TYPE_CHECKING

from foreman.config import Config, ModelSpec, order_for, contributors_for
from foreman.skill_rules import SkillMethod, SkillRule, principle
from foreman.stage_output import (
    StageOutput, TriageOutput, ResearchOutput, SpecOutput, SpecCheckOutput,
    ImplementOutput, ReviewOutput, VerifyOutput, UpdateContext, Slice, SliceRecord,
    InvalidSlicePlan as InvalidSlicePlan, PULL_REQUEST_LINES, AttemptRecord, ReviewSelection,
)

from foreman.dispatch import InvalidOutput
from foreman.review_round import history, accept
from foreman.workspace import Workspace, PullRequestSize, ReviewScope, WorktreeInspectionFailed
from foreman.github import Forge, PullRequest
from foreman.review_context import ReviewContext
from foreman.adapters import Outcome
from foreman.outcomes import Halt

if TYPE_CHECKING:
    from foreman.state import DeliveryState, DeliveryUpdate


@dataclass(frozen=True)
class Acceptance:
    state: "DeliveryState"
    model: ModelSpec | str | None = None
    workspace: Workspace | None = None
    config: Config | None = None
    attempt: AttemptRecord | None = None


def clean_slices(
    raw: list[Slice] | None,
    workspace: Workspace | None,
) -> list[SliceRecord]:
    if not raw:
        raise InvalidOutput("slices: ready parent spec needs at least one slice")
    for item in raw:
        for seam in item.uses:
            if seam.path is not None and (workspace is None or not workspace.has_file(seam.path)):
                raise InvalidSlicePlan("dangling_seam", f"{item.title} uses {seam.name}, but {seam.path} is not a workspace file.")
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
    if workspace is not None:
        for question in output.questions:
            for path in question.checked_paths:
                if not workspace.has_file(path):
                    raise InvalidOutput(f"checked_paths: {path} is not a workspace file")
    plan = state["plan"]
    slices = clean_slices(output.slices, workspace) if plan.issue_kind == "parent" and output.spec_gate == "ready" else []
    return {**output.update(UpdateContext(plan, state["review"], bool(oversized_pr(state)), slices, model if isinstance(model, ModelSpec) else None))}


def accept_implement(output: StageOutput, context: Acceptance) -> "DeliveryUpdate":
    state, model = context.state, context.model
    assert isinstance(output, ImplementOutput)
    update: DeliveryUpdate = {"implementation": output.implementation}
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
    state, model, config = context.state, context.model, context.config
    assert isinstance(output, ReviewOutput) and "dispatch_plan" in state
    target = state["dispatch_plan"]["target"]
    assert target is not None
    review_state = state["review"]
    past = history({"rounds": review_state.review_findings, "settled": review_state.settled_rulings})
    accepted = accept(past, output, target["headRefOid"], target["baseRefOid"])
    if isinstance(model, ModelSpec) and config is not None:
        contributors, families = contributors_for("review", config, state.get("models_used", {}), state.get("implement_models"))
        same_family = model.family in families
        other_configured = any(ModelSpec.parse(candidate).family not in families for candidate in state["dispatch_plan"]["models"])
        provenance = ReviewSelection(
            reviewer=model.name, contributors=contributors, same_family=same_family,
            reason=("preceding_candidates_unavailable" if other_configured else "no_other_family_configured") if same_family else None,
        )
        accepted.provenance = provenance
    blockers = sorted(
        (finding for finding in accepted.findings if finding.blocking_severity),
        key=lambda finding: finding.severity,
    )
    update: DeliveryUpdate = {
        "review": review_state.model_copy(update={
            "review": "changes" if accepted.severity else "" if output.human_question else "approve",
            "detail": "\n".join(f"{finding.severity} {finding.summary}" for finding in blockers),
            "review_findings": [*review_state.review_findings, accepted.model_dump(mode="json")],
            "reviewed_head": accepted.reviewed_head,
            "findings": [finding.record() for finding in blockers],
            "review_receipt": None,
        }),
    }
    if output.human_question or "human_question" in state:
        update["human_question"] = output.human_question or ""
    if "halt_detail" in state:
        update["halt_detail"] = ""
    if context.attempt is not None and accepted.provenance is not None:
        update["attempts"] = [{**context.attempt, "provenance": accepted.provenance.record()}]
    return update


def review_preparation(state: "DeliveryState", workspace: Workspace, forge: Forge) -> tuple[str, PullRequest | None, ReviewScope | None]:
    review_state, checks = state["review"], state["checks"]
    run_state = state["run"]
    repo, head, issue = run_state.repo, checks.verified_head, run_state.issue
    worktree = workspace.inspect()
    if not (repo and issue is not None and head and worktree.clean and worktree.branch and worktree.head == head):
        raise WorktreeInspectionFailed("Review requires a clean branch at the verified HEAD.")
    target = forge.review_target(repo, issue, worktree.branch, head)
    diff = workspace.review_scope(target.baseRefOid, head)
    instruction = f"\n\nRead the full PR diff and committed files with these commands in the checkout:\n{diff.command}"
    instruction += "\n\nSettled rulings:\n" + json.dumps(review_state.settled_rulings)
    return instruction, target, diff


def review_projection(state: "DeliveryState", workspace: Workspace) -> tuple["DeliveryState", ReviewContext | None]:
    assert "dispatch_plan" in state
    review_state, plan = state["review"], state["dispatch_plan"]
    worker_state = state.copy()
    worker_state["review"] = review_state.model_copy(update={
        "review_findings": [], "settled_rulings": [], "findings": [], "detail": "",
        "review": "", "reviewed_head": "", "review_receipt": None,
    })
    assert plan["target"] is not None
    target = plan["target"]
    context = workspace.review_context(target["baseRefOid"], target["headRefOid"])
    if not (context.base.changed or context.head.changed):
        raise WorktreeInspectionFailed("Review requires changed paths.")
    return worker_state, context


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
    unavailable: Callable[["DeliveryState", Outcome, str | None], tuple[Halt, str] | None] = lambda state, outcome, model: None

    models: Callable[[str, "DeliveryState", Config], list[ModelSpec]] = lambda name, state, config: order_for(
        name, config, state.get("models_used") or {}, state.get("implement_models"))


STAGES = {
    "triage": Stage("triage.md", TriageOutput, (
        SkillRule(skill=SkillMethod(path="poteto-mode/SKILL.md", kind="skill", lines=(110, 110))),
    ), input_fields=("run", "plan", "human_question", "human_answers")),
    "research": Stage("research.md", ResearchOutput, (
        SkillRule(skill=SkillMethod(path="how/SKILL.md", kind="leaf_prompt", lines=(17, 17))),
        SkillRule(skill=SkillMethod(path="why/SKILL.md", kind="skill"), when="bug_fix"),
    ), input_fields=("run", "plan")),
    "spec": Stage("spec.md", SpecOutput, (
        principle("redesign-from-first-principles"),
        principle("subtract-before-you-add"),
        principle("model-the-domain"),
        principle("boundary-discipline"),
        principle("type-system-discipline"),
        SkillRule(skill=SkillMethod(path="statechart/SKILL.md", kind="skill"), when="stateful"),
    ), prepare=spec_instructions, accept=accept_spec, input_fields=("run", "plan", "pr_size", "pr_size_override", "human_question", "human_answers")),
    "spec_check": Stage("spec_check.md", SpecCheckOutput, (
        principle("model-the-domain"),
        principle("boundary-discipline"),
        principle("type-system-discipline"),
        principle("prove-it-works"),
    ), kind="dormant", input_fields=("run", "plan")),
    "implement": Stage("implement.md", ImplementOutput, (
        SkillRule(skill=SkillMethod(path="tdd/SKILL.md", kind="skill", lines=(9, 31)), when="bug_fix"),
        SkillRule(skill=SkillMethod(path="benchmark-checklist/SKILL.md", kind="skill", lines=(13, 27)), when="perf_issue"),
    ), accept=accept_implement, input_fields=("run", "plan", "work", "checks", "review")),
    "review": Stage("review.md", ReviewOutput, (
        SkillRule(skill=SkillMethod(path="src/foreman/skills/review-quality.md", kind="repository_skill", lines=(3, 7))),
    ), prepare=review_preparation, accept=accept_review, project=review_projection,
       unavailable=review_unavailable, input_fields=("run", "plan", "work", "checks", "review")),
    "verify": Stage(None, VerifyOutput, accept=accept_verify, kind="deterministic", input_fields=("checks",)),
}
