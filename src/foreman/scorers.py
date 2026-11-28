"""Scorers: grade a finished run's recorded work outside the stages (target.yaml scorers, item 21).

Scorers read recorded work and never decide a route or change a run's result. They call workers through worker, not
dispatch, so their spend stays outside the run's budget ledger. Warp's equivalent: scorers/task-compliance and
scorers/review-quality (https://docs.warp.dev/factories/measure-and-improve/scorers/).
"""

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
import json
from typing import Literal, Mapping

from foreman.config import Config, ModelSpec, family
from foreman.state import DeliveryResult, ScoreRecord
from foreman.worker import Outcome

ScorerName = Literal["task_compliance", "review_quality"]
# One fresh read-only session for the model: the instruction and the output schema it answers. CliWorker.ask makes it.
Ask = Callable[[ModelSpec, str, str], Outcome]


@dataclass(frozen=True)
class Scorer:
    judges: str
    question: str
    labels: tuple[str, ...]


SCORERS: Mapping[ScorerName, Scorer] = {
    "task_compliance": Scorer("implement", "Did the merged PR deliver what the issue asked for, and only that? Judge scope from "
                                           "the issue text, not from the PR's description of itself.",
                              ("compliant", "partial", "non_compliant")),
    "review_quality": Scorer("review", "Did each blocking finding carry a failing demonstration or a cited file:line, and did "
                                       "the review miss a defect that later escaped? A review with no blocking finding and "
                                       "no later escape is clean.",
                             ("actionable", "vague", "empty_or_noise", "clean")),
}


def _schema(scorer: Scorer) -> str:
    return json.dumps({"title": "ScoreOutput", "type": "object", "additionalProperties": False, "required": ["label", "reason"],
                       "properties": {"label": {"type": "string", "enum": list(scorer.labels)}, "reason": {"type": "string"}}})


def _instruction(scorer: Scorer, result: DeliveryResult) -> str:
    """The scorer's drawn question and the run's recorded work it reads."""
    merge = result.get("merge")
    work = {
        "issue": result["run"].request,
        "slice": result["plan"].slice,
        "verified_head": result["checks"].verified_head,
        "merge_commit_sha": merge.merge_commit_sha if merge else "",
        "review_findings": result["review"].review_findings,
        "escapes": [escape.model_dump() for escape in merge.escapes] if merge else [],
        "result": result.get("halt") or result["stop"],
    }
    return (f"{scorer.question}\n\nAnswer with one label and the reason for it. The checkout is the target repository."
            f"\n\nRecorded work:\n{json.dumps(work, default=str)}")


def _spend(outcomes: list[Outcome]) -> str | None:
    """The dollars the scorer's calls reported, or None when a call that ran reported none."""
    costs = [outcome.usage.total_cost_usd if outcome.usage else None for outcome in outcomes if outcome.kind != "cli_missing"]
    return None if any(cost is None for cost in costs) else str(sum((Decimal(str(cost)) for cost in costs if cost is not None), Decimal(0)))


def _score(name: ScorerName, worked: str, result: DeliveryResult, config: Config, ask: Ask, escapes: int) -> ScoreRecord:
    """A fresh session on the next available qualified review model in models.toml order, preferring a family that did not do
    the work it judges. With none available, the score is unknown."""
    scorer = SCORERS[name]
    models = sorted(config.nodes["review"].models, key=lambda model: model.family == worked)
    outcomes: list[Outcome] = []
    for model in models:
        outcomes.append(outcome := ask(model, _instruction(scorer, result), _schema(scorer)))
        label, reason = outcome.output.get("label"), outcome.output.get("reason")
        if outcome.kind == "ran" and label in scorer.labels and isinstance(label, str) and isinstance(reason, str):
            return ScoreRecord(scorer=name, label=label, reason=reason, model=model.name, spend_usd=_spend(outcomes), escapes=escapes)
    return ScoreRecord(scorer=name, label=None, reason="No qualified review model was available.", model="",
                       spend_usd=_spend(outcomes), escapes=escapes)


def score_run(result: DeliveryResult, config: Config, ask: Ask) -> list[ScoreRecord]:
    """The scores the finished run is due: each scorer with work to judge scores it once, and review quality scores it again
    for each escape recorded since its last score. The run's result and routes are not read back from them."""
    scores, merge = result.get("scores", []), result.get("merge")
    escapes = len(merge.escapes) if merge else 0
    due: list[ScoreRecord] = []
    for name, scorer in SCORERS.items():
        worked = result.get("models_used", {}).get(scorer.judges)
        if not worked:
            continue
        done = [score for score in scores if score["scorer"] == name]
        for seen in ([0] if not done else range(done[-1]["escapes"] + 1, escapes + 1) if name == "review_quality" else []):
            due.append(_score(name, family(worked), result, config, ask, escapes if not done else seen))
    return due
