"""One policy row per delivery halt, independent of graph execution."""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum, auto
from typing import Final, Literal, TYPE_CHECKING, cast

if TYPE_CHECKING:
    from foreman.state import DeliveryUpdate


# A paused run waits for GitHub to merge its pull request. A later run restarts the waiting merge stage.
PAUSES = frozenset({"merge_pending"})
# The drawing's terminal outcomes other than halted.
RELEASED_STOPS = frozenset({"answered", "children_filed", "merged", "closed", "cancelled", "no_change"})


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
    worker_unavailable = auto()
    env_unavailable = auto()
    publish_failed = auto()
    implementation_rejected = auto()
    publication_unreconciled = auto()
    model_dependency_missing = auto()
    publication_model_missing = auto()
    not_ready = auto()
    base_check_failed = auto()
    base_protected_changed = auto()


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
    Halt.dirty_worktree: Policy("The workspace does not match the expected clean tree.", "retry",
        Watch("Handle dirty worktree before verify",
              "Implementation left a dirty worktree before verification.", "src/foreman/stages/implement.md")),
    Halt.missing_slice: Policy("The issue has no loadable slice.", "terminal",
        Watch("Slice issues carry a loadable slice",
              "The issue did not carry a loadable slice.", "src/foreman/github.py")),
    Halt.review_not_posted: Policy("The accepted review could not be posted.", "retry",
        Watch("Reviews post to the pull request",
              "The accepted review could not be posted.", "src/foreman/github.py")),
    Halt.git_failed: Policy("A Git or GitHub operation failed.", "retry",
        Watch("Git operations complete before publication",
              "A Git or GitHub operation failed.", "src/foreman/workspace.py")),
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
    Halt.budget_exceeded: Policy(
        "The run reached its token or dollar budget. Raise the FOREMAN_MAX_TOKENS or FOREMAN_MAX_BUDGET_USD variable before resuming.",
        "retry", None),
    Halt.worker_unavailable: Policy(
        "No qualified model for this call has quota or an installed CLI. Foreman does not wait for capacity. Adding Ready "
        "makes the interrupted call again once a provider has capacity.", "retry", None),
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
    Halt.not_ready: Policy("The target repository is not ready for Foreman.", "terminal", None),
    Halt.base_check_failed: Policy(
        "The repository's check fails at the base, so the candidate has nothing passing to be compared with. Fix the base branch.",
        "retry", None),
    Halt.base_protected_changed: Policy(
        "The base head changed the contract file, and a run keeps the contract it was admitted with. Adding Ready checks the base "
        "again without calling a worker, and halts again while the base still differs, so this run cannot take the base's change. "
        "To start over, post a foreman stop comment, which ends this run cancelled and closes its pull request. Once the issue "
        "shows the Done label, add Ready, which admits a new run with the base's current contract and its own budget. If the Done "
        "label does not come, post the stop again.", "retry", None),
}

RESUMABLE_HALTS = frozenset(code for code, row in HALTS.items() if row.recovery != "terminal")


def exit_code(code: str | None) -> int:
    return 1 if code is None or HALTS[Halt(code)].recovery == "terminal" else 75
