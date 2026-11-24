"""Canonical worker contracts, methods and callbacks. Graph successors stay in delivery."""

from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from graphlib import CycleError, TopologicalSorter
import json
from typing import Literal, TYPE_CHECKING

from foreman.config import Config, ModelSpec, order_for, contributors_for, family
from foreman.skill_rules import SkillMethod, SkillRule
from foreman.outcomes import Halt, stopped
from foreman.spec_check import SpecHistory, accept as accept_checked
from foreman.stage_output import (
    StageOutput, TriageOutput, ResearchOutput, SpecOutput, SpecCheckOutput, SpecCandidate, SpecRejection, PlanState,
    ImplementOutput, ReviewOutput, UpdateContext, Slice, SliceRecord,
    InvalidSlicePlan as InvalidSlicePlan, AttemptRecord, ReviewSelection, VerifyOutput, VerifyState,
)

from foreman.contracts import InvalidOutput
from foreman.review_round import history, accept
from foreman.workspace import Workspace, ReviewScope, WorktreeInspectionFailed
from foreman.github import Forge, PullRequest
from foreman.review_context import ReviewContext

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
    raw: Sequence[Slice] | None,
    workspace: Workspace | None,
    base: str = "",
    *,
    prerequisite_seams: Collection[str] = (),
) -> list[SliceRecord]:
    if not raw:
        raise InvalidOutput("slices: ready parent spec needs at least one slice")
    for item in raw:
        for seam in item.uses:
            if seam.path is not None and (workspace is None or not workspace.has_tracked_file(base, seam.path)):
                raise InvalidSlicePlan("dangling_seam", f"{item.title} uses {seam.name}, but {seam.path} is not a tracked regular file at {base}.")
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
            elif seam.path is None and seam.name not in prerequisite_seams:
                raise InvalidSlicePlan("dangling_seam", f"{item.title} uses {seam.name}, which has no builder or existing file.")
    try:
        ordered = list(TopologicalSorter(graph).static_order())
    except CycleError as exc:
        raise InvalidSlicePlan("dependency_cycle", "Dependency cycle: " + " -> ".join(exc.args[1])) from exc
    return [{**slices[title].record(), "depends_on": list(graph[title])} for title in ordered]


def no_preparation(state: "DeliveryState", workspace: Workspace, forge: Forge) -> tuple[str, PullRequest | None, ReviewScope | None]:
    return "", None, None


def pinned_instructions(state: "DeliveryState", workspace: Workspace, forge: Forge) -> tuple[str, PullRequest | None, ReviewScope | None]:
    run = state["run"]
    pinned = (f"\n\ntarget_repository: {run.repo}\ntarget_checkout: {workspace.root.resolve()}\npinned base: {run.base_sha}"
              f"\nRead the committed tree with git ls-tree {run.base_sha} and git show {run.base_sha}:<path>.")
    plan = state["plan"]
    if plan.spec_candidate and plan.slice and plan.slice.get("blocker_numbers"):
        pinned += "\nThe listed blockers merged into the pinned base. Confirm their pathless seams in that tree."
    return pinned, None, None


def accept_output(output: StageOutput, context: Acceptance) -> "DeliveryUpdate":
    state, model = context.state, context.model
    return {**output.update(UpdateContext(state["plan"], state["review"], [], model if isinstance(model, ModelSpec) else None))}


def accept_spec(output: StageOutput, context: Acceptance) -> "DeliveryUpdate":
    state, model, workspace = context.state, context.model, context.workspace
    assert isinstance(output, SpecOutput)
    if workspace is not None:
        for question in output.questions:
            for path in question.checked_paths:
                if not workspace.has_tracked_file(state["run"].base_sha, path):
                    raise InvalidOutput(f"checked_paths: {path} is not a tracked regular file at the pinned base")
    plan = state["plan"]
    slices = [item.record() for item in output.slices or []]
    if output.spec_gate == "ready" and not slices and plan.slice is not None:
        slices = [plan.slice]
    if output.spec_gate == "ready" and not slices:
        raise InvalidOutput("slices: ready spec needs at least one slice")
    return {**output.update(UpdateContext(plan, state["review"], slices, model if isinstance(model, ModelSpec) else None))}


def reject_candidate(plan: PlanState, revision: str, detail: str, max_rounds: int,
                     reason: SpecRejection | None = None) -> "DeliveryUpdate":
    """Count each rejected candidate once. The final allowed rejection or a repeated one stops planning."""
    repeated = revision in plan.rejected_revisions
    rejected = plan.rejected_revisions if repeated else [*plan.rejected_revisions, revision]
    planning = plan.model_copy(update={"rejected_revisions": rejected, "spec_rejection": reason})
    update: "DeliveryUpdate" = {"plan": planning, "human_question": ""}
    if planning.spec_failures >= max_rounds:
        update.update(stopped(Halt.spec_cap, detail, "spec"))
    elif repeated:
        update.update(stopped(Halt.invalid_output, f"Revision repeats a rejected plan. {detail}", "spec"))
    return update


def accept_spec_check(output: StageOutput, context: Acceptance) -> "DeliveryUpdate":
    assert isinstance(output, SpecCheckOutput) and context.config is not None
    if output.human_question:
        return {"human_question": output.human_question}
    plan = context.state["plan"]
    assert plan.spec_candidate is not None
    candidate = SpecCandidate.from_record(plan.spec_candidate)
    checked = accept_checked(SpecHistory.model_validate(plan.spec_history), output, candidate)
    planning = plan.model_copy(update={"spec_history": {"rounds": checked.history.rounds, "settled": checked.history.settled}})
    if checked.approval:
        return {"plan": planning, "human_question": ""}
    detail = "\n".join(finding.summary for finding in checked.findings)
    return reject_candidate(planning, candidate.revision, detail, context.config.max_review_rounds)


def accept_implement(output: StageOutput, context: Acceptance) -> "DeliveryUpdate":
    state, model = context.state, context.model
    assert isinstance(output, ImplementOutput)
    update: DeliveryUpdate = {"implementation": output.implementation, "human_question": output.human_question or ""}
    if model:
        latest = state.get("models_used", {}).get("implement")
        history = state.get("implement_models", [latest] if latest else [])
        update["implement_models"] = [*history, model.name if isinstance(model, ModelSpec) else model]
    return update


def accept_verify(output: StageOutput, context: Acceptance) -> "DeliveryUpdate":
    assert isinstance(output, VerifyOutput)
    assert "work" in context.state
    head = context.state["work"].published_head
    if not head or output.tested_head != head:
        raise InvalidOutput(f"tested_head {output.tested_head} is not the published head {head}.")
    return {"verify": VerifyState(outcome="driven", tested_head=head, criteria=[item.record() for item in output.criteria])}


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


def implement_preparation(state: "DeliveryState", workspace: Workspace, forge: Forge) -> tuple[str, PullRequest | None, ReviewScope | None]:
    """Render the drawn context's exact repository check. The check is granted as written, so a wrapped or split run is denied."""
    check = state["checks"].check
    if not check:
        return "", None, None
    return (f"\n\nRepository check, granted exactly as written. Run it unchanged, with no wrapper, prefix, suffix or chained command, "
            f"and never run one segment of it alone:\n{check}"), None, None


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


@dataclass(frozen=True)
class Stage:
    prompt: str | None
    output: type[StageOutput] | None
    skills: tuple[SkillRule, ...] = ()
    kind: Literal["model", "dormant"] = "model"
    input_fields: tuple[str, ...] = ()
    prepare: Callable[["DeliveryState", Workspace, Forge], tuple[str, PullRequest | None, ReviewScope | None]] = no_preparation
    accept: Callable[[StageOutput, Acceptance], "DeliveryUpdate"] = accept_output
    project: Callable[["DeliveryState", Workspace], tuple["DeliveryState", ReviewContext | None]] = lambda state, workspace: (state.copy(), None)

    models: Callable[[str, "DeliveryState", Config], list[ModelSpec]] = lambda name, state, config: order_for(
        name, config, state.get("models_used") or {}, state.get("implement_models"))


class TriageStage(Stage):
    step = "triage.route"


class ResearchStage(Stage):
    step = "research.answer"


class SpecStage(Stage):
    step = "spec.draft"


class SpecCheckStage(Stage):
    model_selector = "spec"
    model_dependency = "spec"
    worker = "checker"
    step = "spec.check"


class ImplementStage(Stage):
    step = "implement.edit"


class VerifyStage(Stage):
    step = "verify.drive"


class ReviewStage(Stage):
    step = "review.inspect"


def spec_check_models(name: str, state: "DeliveryState", config: Config) -> list[ModelSpec]:
    models = order_for("spec", config, state.get("models_used", {}))
    candidate = state.get("plan")
    record = candidate.spec_candidate if candidate else None
    latest = state.get("models_used", {}).get("spec")
    author = record["author"]["family"] if record else family(latest) if latest else None
    return sorted(models, key=lambda model: model.family == author)


def spec_check_projection(state: "DeliveryState", workspace: Workspace) -> tuple["DeliveryState", ReviewContext | None]:
    plan = state["plan"]
    assert plan.spec_candidate is not None
    answers = [answer for answer in plan.human_qa if answer.get("candidate") == plan.spec_candidate["revision"]]
    settled = SpecHistory.model_validate(plan.spec_history).settled
    return {**state, "plan": plan.model_copy(update={"human_qa": answers, "spec_history": {"settled": settled}}),
            "human_answers": [answer["answer"] for answer in answers]}, None


# The target repository's own skill, written by pstack create-verification-skill.
VERIFY_SKILL = SkillMethod(path="verify-<app>/SKILL.md", kind="target_skill")
DIAGNOSING_BUGS = SkillMethod(path="engineering/diagnosing-bugs/SKILL.md", kind="skill", lines=(18, 37), repository="matt_skills")
STAGES = {
    "triage": TriageStage("triage.md", TriageOutput, prepare=pinned_instructions, input_fields=("run", "plan", "human_answers")),
    "research": ResearchStage("research.md", ResearchOutput, (
        SkillRule(skill=SkillMethod(path="how/SKILL.md", kind="leaf_prompt", lines=(17, 17))),
        SkillRule(skill=SkillMethod(path="why/SKILL.md", kind="skill", lines=(9, 9)), when="bug_fix"),
        SkillRule(skill=DIAGNOSING_BUGS, when="bug_fix"),
        SkillRule(skill=DIAGNOSING_BUGS, when="perf_issue"),
    ), prepare=pinned_instructions, input_fields=("run", "plan", "human_answers")),
    "spec": SpecStage("spec.md", SpecOutput, (
        SkillRule(skill=SkillMethod(path="architect/SKILL.md", kind="skill", lines=(9, 9))),
        SkillRule(skill=SkillMethod(path="statechart/SKILL.md", kind="skill", lines=(23, 27)), when="stateful"),
        SkillRule(skill=SkillMethod(path="engineering/to-tickets/SKILL.md", kind="skill", lines=(29, 40),
                                    repository="matt_skills"), when="parent"),
    ), prepare=pinned_instructions, accept=accept_spec, input_fields=("run", "plan", "human_answers")),
    "spec_check": SpecCheckStage("spec_check.md", SpecCheckOutput, (
        SkillRule(skill=SkillMethod(path="interrogate/SKILL.md", kind="leaf_prompt", lines=(19, 19))),
    ), prepare=pinned_instructions, accept=accept_spec_check, project=spec_check_projection,
        input_fields=("run", "plan", "human_answers"), models=spec_check_models),
    "implement": ImplementStage("implement.md", ImplementOutput, (
        SkillRule(skill=SkillMethod(path="tdd/SKILL.md", kind="skill", lines=(9, 31)), when="bug_fix"),
        SkillRule(skill=SkillMethod(path="benchmark-checklist/SKILL.md", kind="skill", lines=(13, 27)), when="perf_issue"),
        SkillRule(skill=SkillMethod(path="formal-<area>/SKILL.md", kind="target_skill")),
    ), prepare=implement_preparation, accept=accept_implement, input_fields=("run", "plan", "work", "checks", "review")),
    "verify": VerifyStage("verify.md", VerifyOutput, (SkillRule(skill=VERIFY_SKILL),), accept=accept_verify,
                          input_fields=("run", "plan", "work", "checks")),
    "review": ReviewStage("review.md", ReviewOutput, (
        SkillRule(skill=SkillMethod(path="src/foreman/skills/review-quality.md", kind="repository_skill", lines=(3, 7))),
        SkillRule(skill=SkillMethod(path="blast-radius/SKILL.md", kind="skill", lines=(31, 37))),
    ), prepare=review_preparation, accept=accept_review, project=review_projection,
        input_fields=("run", "plan", "work", "checks", "verify", "review")),
}
