"""One policy row per delivery halt, independent of graph execution."""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum, auto
from typing import Final, Literal, TYPE_CHECKING, cast

if TYPE_CHECKING:
    from foreman.state import DeliveryUpdate


class Halt(StrEnum):
    invalid_output = auto()
    verify_cap = auto()
    review_cap = auto()
    spec_cap = auto()
    dirty_worktree = auto()
    missing_slice = auto()
    review_not_posted = auto()
    git_failed = auto()
    skill_missing = auto()
    merge_held = auto()
    merge_not_armed = auto()
    readiness_unavailable = auto()
    merged_unreviewed = auto()
    budget_exceeded = auto()
    env_unavailable = auto()
    publish_failed = auto()
    implementation_rejected = auto()
    publication_unreconciled = auto()
    model_dependency_missing = auto()
    publication_model_missing = auto()


def halted(reason: Halt, detail: str, path: str | None) -> "DeliveryUpdate":
    if not detail.strip():
        raise ValueError(f"{reason} requires a nonempty halt detail")
    # Checkpoint codes remain strings after validation as Halt members.
    return {"halt": cast(Halt, reason.value), "halt_detail": detail, "watch": None, "path": [path] if path else []}


def stopped(reason: Halt, detail: str, path: str) -> "DeliveryUpdate":
    """Halt and end the stage, so the stage result carries the named halt."""
    return {**halted(reason, detail, path), "stop": reason}


@dataclass(frozen=True)
class Watch:
    title: str
    cause: str
    file: str


@dataclass(frozen=True)
class Policy:
    reason: str
    recovery: Literal["terminal", "retry"]
    watch: Watch | None


HALTS: Final[Mapping[Halt, Policy]] = {
    Halt.invalid_output: Policy("The step's answer was not in the required format.", "terminal",
        Watch("Fix invalid output on {node}",
              "The {node} output did not satisfy its contract.", "src/foreman/stages/{node}.md")),
    Halt.verify_cap: Policy("Verification still fails at the attempt cap.", "terminal",
        Watch("Investigate repeated verify failures reaching cap",
              "Verification kept failing after implementation.", "src/foreman/stages/implement.md")),
    Halt.review_cap: Policy("Review still blocks at the attempt cap.", "terminal",
        Watch("Investigate repeated review blocks reaching cap",
              "Review remained blocked after implementation.", "src/foreman/stages/implement.md")),
    Halt.spec_cap: Policy("The slice plan still fails at the attempt cap.", "terminal",
        Watch("Investigate rejected slice plans reaching cap",
              "Slice plans kept failing the spec gate.", "src/foreman/stages/spec.md")),
    Halt.dirty_worktree: Policy("The workspace does not match the expected clean tree.", "terminal",
        Watch("Handle dirty worktree before verify",
              "Implementation left a dirty worktree before verification.", "src/foreman/stages/implement.md")),
    Halt.missing_slice: Policy("The issue has no loadable slice.", "terminal",
        Watch("Slice issues carry a loadable slice",
              "The issue did not carry a loadable slice.", "src/foreman/github.py")),
    Halt.review_not_posted: Policy("The accepted review could not be posted.", "retry",
        Watch("Reviews post to the pull request",
              "The accepted review could not be posted.", "src/foreman/github.py")),
    Halt.git_failed: Policy("A workspace git operation failed.", "retry",
        Watch("Git operations complete before publication",
              "A workspace git operation failed.", "src/foreman/workspace.py")),
    Halt.skill_missing: Policy("A required instruction file is unavailable at the selected version.", "terminal",
        Watch("Pinned skills are available before dispatch",
              "A required skill was unavailable at the pinned commit.", "src/foreman/skill_rules.py")),
    Halt.merge_held: Policy("Repository policy holds merge arming.", "retry", None),
    Halt.merge_not_armed: Policy("Auto-merge is not switched on for the reviewed version.", "retry", None),
    Halt.readiness_unavailable: Policy("Merge readiness cannot be established.", "retry", None),
    Halt.merged_unreviewed: Policy("GitHub merged the pull request at a head Foreman did not review.", "terminal",
        Watch("Investigate a merge at an unreviewed head",
              "GitHub merged the pull request at a head Foreman did not review, so the run earned no credit or dependent release.",
              "src/foreman/steps/merge.py")),
    Halt.budget_exceeded: Policy("The run reached its token or dollar budget.", "terminal", None),
    Halt.env_unavailable: Policy("The implementation environment is unavailable.", "retry", None),
    Halt.publish_failed: Policy("The branch or pull request could not be published.", "retry", None),
    Halt.implementation_rejected: Policy("The code changes did not match what the step reported.", "retry", None),
    Halt.publication_unreconciled: Policy(
        "A push or pull request write may have landed and GitHub could not confirm it. Rerun to reconcile.", "retry", None),
    Halt.model_dependency_missing: Policy(
        "This step needs a model from an earlier step that never ran. Fix models.toml.", "terminal", None),
    Halt.publication_model_missing: Policy(
        "No model was recorded for the build step, so the commit cannot credit it. Fix models.toml.", "terminal",
        None),
}

RESUMABLE_HALTS = frozenset(code for code, row in HALTS.items() if row.recovery != "terminal")


def exit_code(code: str | None) -> int:
    return 1 if code is None or HALTS[Halt(code)].recovery == "terminal" else 75
