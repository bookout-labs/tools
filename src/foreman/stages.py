"""Canonical worker contracts and methods. Graph successors stay in delivery."""

from dataclasses import dataclass
from typing import Literal, TYPE_CHECKING

from foreman.config import Config, ModelSpec, order_for
from foreman.skill_rules import PstackMethod, SkillRule, principle
from foreman.stage_output import (
    StageOutput, TriageOutput, ResearchOutput, SpecOutput, SpecCheckOutput,
    ImplementOutput, ReviewOutput, VerifyOutput,
)

if TYPE_CHECKING:
    from foreman.state import DeliveryState


@dataclass(frozen=True)
class Stage:
    prompt: str | None
    output: type[StageOutput] | None
    skills: tuple[SkillRule, ...] = ()
    kind: Literal["model", "dormant", "deterministic"] = "model"
    input_fields: tuple[str, ...] = ()

    def models(self, name: str, state: "DeliveryState", config: Config) -> list[ModelSpec]:
        return order_for(name, config, state.get("models_used") or {}, state.get("implement_models"))


STAGES = {
    "triage": Stage("triage.md", TriageOutput, (
        SkillRule(skill=PstackMethod(path="poteto-mode/SKILL.md", kind="skill", lines=(110, 110))),
    ), input_fields=("run", "plan")),
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
    ), input_fields=("run", "plan", "pr_size")),
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
    ), input_fields=("run", "plan", "work", "checks", "review")),
    "review": Stage("review.md", ReviewOutput, (
        principle("boundary-discipline"),
        principle("model-the-domain"),
        principle("prove-it-works"),
        principle("test-behavior-not-implementation"),
        SkillRule(skill="review-quality"),
        SkillRule(skill="merge-risk"),
    ), input_fields=("run", "plan", "work", "checks", "review")),
    "verify": Stage(None, VerifyOutput, kind="deterministic", input_fields=("work", "checks")),
    "proof": Stage(None, None, kind="deterministic", input_fields=("plan", "checks", "review")),
}
