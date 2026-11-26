"""The run report the issue's status comment holds, written whole from the run's checkpoint (rules.reporting).

It holds only what Foreman's own code names or counts, so no text a CLI, a provider, a worker or a person wrote reaches it.
"""

from collections.abc import Callable, Sequence
from decimal import Decimal
from importlib.metadata import version
from typing import NamedTuple

from foreman.budget import claude_spend, work_tokens
from foreman.config import SkillPin
from foreman.review_round import history
from foreman.stage_output import AttemptRecord
from foreman.stages import STAGES
from foreman.state import DeliveryResult, DeliveryState

BLUEPRINT_VERSION = 3  # docs/graph/blueprint/target.yaml's blueprint_version, which tests hold it to.
COLUMNS = ("Stage", "Step", "Model", "Skills", "Outcome", "Tokens", "Dollars")


class Running(NamedTuple):
    """A call under way: the reservation dispatch saved, whose result it has not recorded, and the skills its plan names."""

    stage: str
    model: str
    methods: list[str]


# Writes the run report at a moment inside the run, from the state the run's checkpoint holds then.
Rewrite = Callable[[DeliveryState, Running | None], None]


def no_rewrite(state: DeliveryState, running: Running | None) -> None:
    """A run with no issue to report on, as in dry runs and tests of single steps."""


def _repository(pin: SkillPin) -> str:
    """The pin's repository by its URL's name. A local checkout's path is on the Foreman host, so it is not named."""
    return pin.repository.removeprefix("https://github.com/") if "://" in pin.repository else "a local repository"


def _skills(stage: str, methods: list[str], pins: dict[str, SkillPin], target: str) -> str:
    """Each skill by its ref and repository, and a target repository's skill by its path there. Never a host path."""
    known = {rule.skill.path: rule.skill for rule in STAGES[stage].skills}
    names: list[str] = []
    for path in methods:
        method = known.get(path)
        if method is None or method.kind == "target_skill":
            names.append(f"{path} ({target})")
        elif method.kind == "repository_skill":
            names.append(f"{path} (Foreman {version('bookout-tools')})")
        else:
            pin = pins[method.repository]
            names.append(f"{pin.directory}/{path}@{pin.commit[:12]} ({_repository(pin)})")
    return ", ".join(names) or "none"


def _dollars(attempt: AttemptRecord) -> str:
    """What dispatch charged, its worst case included. Dollars a CLI did not report and dispatch did not charge are unknown."""
    if "dollar_charge" in attempt:
        return f"${attempt['dollar_charge']}"
    if attempt["outcome"] == "cli_missing":
        return "$0"
    usage = attempt["usage"]
    return f"${Decimal(repr(usage['cost_usd']))}" if usage is not None and usage["cost_usd"] is not None else "unknown"


def _cell(value: int | None) -> str:
    return "unknown" if value is None else str(value)


def _row(cells: Sequence[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def run_report(state: DeliveryState | DeliveryResult, running: Running | None = None) -> str:
    """The report of the step under way, or of the step where the run stopped, which the run's steps end with."""
    run, plan, checks, steps = state["run"], state["plan"], state["checks"], state.get("steps", [])
    stage, _, step = (steps[-1] if steps else "").partition(".")
    pins = {name: SkillPin.model_validate(raw) for name, key in (("pstack", "pstack_pin"), ("matt_skills", "matt_skills_pin"))
            if (raw := state.get(key)) is not None}
    attempts = state.get("attempts", [])
    lines = [
        f"Repository: {run.repo}",
        f"Issue: #{run.issue}",
        f"Blueprint version: {BLUEPRINT_VERSION}",
        f"Stage: {stage}",
        f"Step: {step}",
        f"Steps taken: {' '.join(steps)}",
        f"Skill pins: {'; '.join(f'{_repository(pin)}@{pin.commit} {pin.directory}' for pin in pins.values())}",
        # Each count is the one its stage compares with max_review_rounds.
        f"Plan revisions: {plan.spec_failures}",
        f"Failed checks: {checks.verify_failures}",
        f"Blocking rounds: {history({'rounds': state['review'].review_findings}).blocking}",
    ]
    if (start := state.get("start")) is not None:
        spent = claude_spend(attempts)
        lines += [f"Tokens: {_cell(work_tokens(attempts))} of {start.max_tokens}",
                  f"Dollars: {'unknown' if spent is None else f'${spent}'} of ${start.max_budget_usd}"]
    table = [_row(COLUMNS), _row(["---"] * len(COLUMNS))]
    for attempt in attempts:
        drawn_stage, _, drawn_step = STAGES[attempt["stage"]].step.partition(".")
        table.append(_row([drawn_stage, drawn_step, attempt["model"] or "none",
                           _skills(attempt["stage"], attempt["methods"], pins, run.repo),
                           attempt["outcome"], _cell(work_tokens([attempt])), _dollars(attempt)]))
    if running is not None:
        drawn_stage, _, drawn_step = STAGES[running.stage].step.partition(".")
        table.append(_row([drawn_stage, drawn_step, running.model, _skills(running.stage, running.methods, pins, run.repo),
                           "running", "", ""]))
    return "\n".join(["Run report", "", *lines, "", *table])
