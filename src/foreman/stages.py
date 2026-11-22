"""Canonical worker contracts, methods and callbacks. Graph successors stay in delivery."""

from collections.abc import Callable
from dataclasses import dataclass
from graphlib import CycleError, TopologicalSorter
import json
from typing import Literal, TYPE_CHECKING

from foreman.config import Config, ModelSpec, order_for
from foreman.skill_rules import PstackMethod, SkillRule, principle
from foreman.stage_output import (
    StageOutput, TriageOutput, ResearchOutput, SpecOutput, SpecCheckOutput,
    ImplementOutput, ReviewOutput, VerifyOutput, UpdateContext, Slice, SliceRecord,
    SpecRejection, SpecRejectionKind, PULL_REQUEST_LINES,
)

from foreman.dispatch import InvalidOutput
from foreman.review_round import history
from foreman.workspace import Workspace, PullRequestSize

if TYPE_CHECKING:
    from foreman.state import DeliveryState, DeliveryUpdate


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


def no_instructions(state: "DeliveryState") -> str:
    return ""


def implement_instructions(state: "DeliveryState") -> str:
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
    return instruction


def spec_instructions(state: "DeliveryState") -> str:
    if size := oversized_pr(state):
        return (
            f"\n\nThe pull request has {size['lines']} changed lines. The limit is {PULL_REQUEST_LINES}."
            " Split the work into independently shippable pull requests."
            " For a slice issue, describe the split in spec."
        )
    return ""


def accept_output(
    output: StageOutput, state: "DeliveryState", model: str | None,
    workspace: Workspace | None, accepted_model: ModelSpec | None,
) -> "DeliveryUpdate":
    return {**output.update(UpdateContext(state["plan"], state["review"], bool(oversized_pr(state)), [], accepted_model))}


def accept_spec(
    output: StageOutput, state: "DeliveryState", model: str | None,
    workspace: Workspace | None, accepted_model: ModelSpec | None,
) -> "DeliveryUpdate":
    assert isinstance(output, SpecOutput)
    plan = state["plan"]
    slices = clean_slices(output.slices, workspace) if plan.issue_kind == "parent" and output.spec_gate == "ready" else []
    return {**output.update(UpdateContext(plan, state["review"], bool(oversized_pr(state)), slices, accepted_model))}


def accept_implement(
    output: StageOutput, state: "DeliveryState", model: str | None,
    workspace: Workspace | None, accepted_model: ModelSpec | None,
) -> "DeliveryUpdate":
    assert isinstance(output, ImplementOutput)
    update: DeliveryUpdate = {"implementation": output.implementation, "checks": state["checks"].model_copy(update={
        "browser_proof": output.browser_proof.record() if output.browser_proof else None,
        "formal_proof": output.formal_proof.record() if output.formal_proof else None,
    })}
    if model:
        latest = state.get("models_used", {}).get("implement")
        history = state.get("implement_models", [latest] if latest else [])
        update["implement_models"] = [*history, model]
    return update


@dataclass(frozen=True)
class Stage:
    prompt: str | None
    output: type[StageOutput] | None
    skills: tuple[SkillRule, ...] = ()
    kind: Literal["model", "dormant", "deterministic"] = "model"
    input_fields: tuple[str, ...] = ()
    prepare: Callable[["DeliveryState"], str] = no_instructions
    accept: Callable[[StageOutput, "DeliveryState", str | None, Workspace | None, ModelSpec | None], "DeliveryUpdate"] = accept_output

    def models(self, name: str, state: "DeliveryState", config: Config) -> list[ModelSpec]:
        return order_for(name, config, state.get("models_used") or {}, state.get("implement_models"))


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
    ), input_fields=("run", "plan", "work", "checks", "review", "implementation")),
    "verify": Stage(None, VerifyOutput, kind="deterministic", input_fields=("checks",)),
    "proof": Stage(None, None, kind="deterministic", input_fields=("plan", "checks", "review")),
}
