"""Scorers and the self-improvement proposal. Neither one edits the model list."""

from dataclasses import dataclass
from pathlib import Path
import tomllib

from foreman._raw import is_dict

DEFAULT_PATH = Path(__file__).with_name("measure.toml")
LABELS = ("approve", "block", "insufficient_evidence")
PROPOSALS = {
    "redundant-tests": (
        "stages/implement.md",
        "Tell the builder to add a test only when it checks a behavior the change can break.",
    ),
    "code-quality": (
        "skills/code-quality.md",
        "Tighten the failing axis on the code-quality skill.",
    ),
}


class MeasureError(ValueError):
    pass


@dataclass(frozen=True)
class MeasureConfig:
    sampling_rate: int
    passing_score: float
    labels: dict[str, float]


@dataclass(frozen=True)
class Grade:
    label: str
    score: float
    passed: bool


def parse_measure(data: object) -> MeasureConfig:
    if not is_dict(data):
        raise MeasureError("measure config must be a table")
    rate = data.get("sampling_rate")
    bar = data.get("passing_score")
    labels = data.get("labels")
    if not isinstance(rate, int) or rate < 1:
        raise MeasureError("sampling_rate must be an integer of at least 1")
    if not isinstance(bar, (int, float)) or isinstance(bar, bool) or not 0 <= float(bar) <= 1:
        raise MeasureError("passing_score must be a number from 0 to 1")
    if not is_dict(labels):
        raise MeasureError("labels must be a table")
    parsed: dict[str, float] = {}
    for name in LABELS:
        score = labels.get(name)
        if not isinstance(score, (int, float)) or isinstance(score, bool) or not 0 <= float(score) <= 1:
            raise MeasureError(f"{name} needs a score from 0 to 1")
        parsed[name] = float(score)
    names: set[str] = set()
    for key in labels:
        if not isinstance(key, str):
            raise MeasureError("label names must be strings")
        names.add(key)
    extra = names - set(LABELS)
    if extra:
        raise MeasureError(f"unknown label {sorted(extra)[0]}")
    return MeasureConfig(sampling_rate=rate, passing_score=float(bar), labels=parsed)


def load_measure(path: Path | None = None) -> MeasureConfig:
    source = path or DEFAULT_PATH
    with source.open("rb") as handle:
        return parse_measure(tomllib.load(handle))


def grade(label: str, config: MeasureConfig | None = None) -> Grade:
    config = config or load_measure()
    if label not in config.labels:
        raise MeasureError(f"unknown label {label}")
    score = config.labels[label]
    passed = label != "insufficient_evidence" and score >= config.passing_score
    return Grade(label=label, score=score, passed=passed)


def selected(run_index: int, config: MeasureConfig | None = None) -> bool:
    config = config or load_measure()
    if isinstance(run_index, bool) or run_index < 0:
        raise MeasureError("run index must be zero or greater")
    return run_index % config.sampling_rate == 0


def propose(failure: str) -> str:
    if failure not in PROPOSALS:
        raise MeasureError(f"unknown failure {failure}")
    target, change = PROPOSALS[failure]
    if "models.toml" in target or target.startswith("models"):
        raise MeasureError("the model list is config")
    return (
        f"Proposed change to {target}.\n"
        f"{change}\n"
        "Leave the label scores in measure.toml.\n"
        "This proposal does not change models.toml or max_review_rounds.\n"
        "A person opens the pull request.\n"
    )
