"""One policy row per delivery halt, independent of graph execution."""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum, auto
from typing import Final, Literal, TYPE_CHECKING, cast

from foreman.readiness import Outcome as ReadinessOutcome


if TYPE_CHECKING:
    from foreman.state import DeliveryUpdate


class Halt(StrEnum):
    invalid_output = auto()
    verify_cap = auto()
    review_cap = auto()
    spec_cap = auto()
    dirty_worktree = auto()
    insufficient_evidence = auto()
    missing_slice = auto()
    review_unavailable = auto()
    review_not_posted = auto()
    pr_size_unavailable = auto()
    git_failed = auto()
    usage_unknown = auto()
    skill_missing = auto()
    reviewer_unavailable = auto()
    cli_missing = auto()
    merge_held = auto()
    merge_not_armed = auto()
    pr_conflict = auto()
    ci_pending = auto()
    queue_rejected = auto()
    ci_red = auto()
    readiness_unavailable = auto()
    budget_exceeded = auto()
    env_unavailable = auto()
    quota_exhausted = auto()
    publish_failed = auto()
    implementation_rejected = auto()
    failed = auto()
    design_rethink = auto()
    model_dependency_missing = auto()
    model_family_exhausted = auto()
    publication_model_missing = auto()


def halted(reason: Halt, detail: str, path: str | None) -> DeliveryUpdate:
    if not detail.strip():
        raise ValueError(f"{reason} requires a nonempty halt detail")
    # Checkpoint codes remain strings after validation as Halt members.
    return {"halt": cast(Halt, reason.value), "halt_detail": detail, "watch": None, "path": [path] if path else []}


@dataclass(frozen=True)
class Watch:
    title: str
    cause: str
    file: str


@dataclass(frozen=True)
class Policy:
    reason: str
    recovery: Literal["terminal", "retry", "recheck_merge"]
    watch: Watch | None
    readiness: tuple[ReadinessOutcome, ...] = ()


HALTS: Final[Mapping[Halt, Policy]] = {
    Halt.invalid_output: Policy("The step's answer was not in the required format.", "terminal",
        Watch("Fix invalid output on {node}",
              "The {node} output did not satisfy its contract.", "src/foreman/stages/{node}.md")),
    Halt.verify_cap: Policy("Verification still fails at the attempt cap.", "terminal",
        Watch("Investigate repeated verify failures reaching cap",
              "Verification kept failing after implementation.", "src/foreman/stages/implement.md")),
    Halt.review_cap: Policy("Review or proof still blocks at the attempt cap.", "terminal",
        Watch("Investigate repeated review or proof blocks reaching cap",
              "Review or proof remained blocked after implementation.", "src/foreman/stages/implement.md")),
    Halt.spec_cap: Policy("The slice plan still fails at the attempt cap.", "terminal",
        Watch("Investigate rejected slice plans reaching cap",
              "Slice plans kept failing the spec gate.", "src/foreman/stages/spec.md")),
    Halt.dirty_worktree: Policy("The workspace does not match the expected clean tree.", "terminal",
        Watch("Handle dirty worktree before verify",
              "Implementation left a dirty worktree before verification.", "src/foreman/stages/implement.md")),
    Halt.insufficient_evidence: Policy("Required proof is missing.", "terminal",
        Watch("Required proof is missing after implement",
              "Implementation did not supply the required proof.", "src/foreman/stages/implement.md")),
    Halt.missing_slice: Policy("The issue has no loadable slice.", "terminal",
        Watch("Slice issues carry a loadable slice",
              "The issue did not carry a loadable slice.", "src/foreman/github.py")),
    Halt.review_unavailable: Policy("The pull request cannot be loaded for review.", "terminal",
        Watch("Review finds its pull request",
              "The pull request could not be loaded for review.", "src/foreman/github.py")),
    Halt.review_not_posted: Policy("The accepted review could not be posted.", "retry",
        Watch("Reviews post to the pull request",
              "The accepted review could not be posted.", "src/foreman/github.py")),
    Halt.pr_size_unavailable: Policy("The pull request size cannot be measured.", "terminal",
        Watch("Pull request size is measurable before review",
              "The pull request size could not be measured.", "src/foreman/workspace.py")),
    Halt.git_failed: Policy("A workspace git operation failed.", "retry",
        Watch("Git operations complete before publication",
              "A workspace git operation failed.", "src/foreman/workspace.py")),
    Halt.usage_unknown: Policy("A model run did not report its token usage or dollar cost.", "terminal",
        Watch("{adapter} attempts report usage",
              "The {adapter} attempt did not report usage.", "src/foreman/adapters/{adapter}.py")),
    Halt.skill_missing: Policy("A required instruction file is unavailable at the selected version.", "terminal",
        Watch("Pinned skills are available before dispatch",
              "A required skill was unavailable at the pinned commit.", "src/foreman/skill_rules.py")),
    Halt.reviewer_unavailable: Policy("Independent reviewer models are temporarily unavailable.", "retry", None),
    Halt.cli_missing: Policy("The program that runs the model could not be found.", "terminal", None),
    Halt.merge_held: Policy("Repository policy holds merge arming.", "recheck_merge", None),
    Halt.merge_not_armed: Policy("Auto-merge is not switched on for the reviewed version.", "recheck_merge",
        None, readiness=("ready", "head_changed", "closed", "retargeted")),
    Halt.pr_conflict: Policy("The pull request conflicts with its base.", "recheck_merge",
        None, readiness=("conflict",)),
    Halt.ci_pending: Policy("Required checks are still pending.", "recheck_merge", None, readiness=("pending",)),
    Halt.queue_rejected: Policy("The merge queue rejected the pull request.", "recheck_merge",
        None, readiness=("queue_rejected",)),
    Halt.ci_red: Policy("Required checks failed.", "recheck_merge", None, readiness=("red",)),
    Halt.readiness_unavailable: Policy("Merge readiness cannot be established.", "recheck_merge",
        None, readiness=("policy",)),
    Halt.budget_exceeded: Policy("The run reached its token or dollar budget.", "terminal", None),
    Halt.env_unavailable: Policy("The implementation environment is unavailable.", "retry", None),
    Halt.quota_exhausted: Policy("Eligible models have no available quota.", "retry", None),
    Halt.publish_failed: Policy("The branch or pull request could not be published.", "retry", None),
    Halt.implementation_rejected: Policy("The code changes did not match what the step reported.", "retry", None),
    Halt.failed: Policy("The worker failed or produced no usable change.", "terminal", None),
    Halt.design_rethink: Policy("The design requires a human decision.", "terminal", None),
    Halt.model_dependency_missing: Policy(
        "This step needs a model from an earlier step that never ran. Fix models.toml.", "terminal", None),
    Halt.model_family_exhausted: Policy("No model from a different company is left to review.", "terminal", None),
    Halt.publication_model_missing: Policy(
        "No model was recorded for the build step, so the commit cannot credit it. Fix models.toml.", "terminal",
        None),
}

RESUMABLE_HALTS = frozenset(code for code, row in HALTS.items() if row.recovery != "terminal")
READINESS_HALTS = {outcome: code for code, row in HALTS.items() for outcome in row.readiness}


def exit_code(code: str | None) -> int:
    return 1 if code is None or HALTS[Halt(code)].recovery == "terminal" else 75
