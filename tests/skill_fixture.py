"""Offline skill sources for graph tests and evals."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from tempfile import TemporaryDirectory

from foreman.config import Config, PstackPin, load_config
from foreman.skill_rules import PstackMethod, SKILL_RULES, export_skills, git


def fixture_pin(root: Path) -> PstackPin:
    root.mkdir()
    git(root, "init", "--quiet")
    for rule in SKILL_RULES:
        method = rule.skill
        if isinstance(method, PstackMethod):
            path = root / "pstack" / "skills" / method.path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("Synthetic method.\n" * (method.lines[1] if method.lines else 1), encoding="utf-8")
    git(root, "add", "--", "pstack")
    git(
        root, "-c", "user.name=Foreman", "-c", "user.email=foreman@example.invalid",
        "-c", "commit.gpgSign=false", "commit", "--quiet", "-m", "Synthetic skill fixture",
    )
    return PstackPin(repository=str(root.resolve()), commit=git(root, "rev-parse", "HEAD"))


@contextmanager
def skill_fixture(config: Config | None = None) -> Iterator[tuple[Config, Path]]:
    with TemporaryDirectory(prefix="foreman-skills-") as directory:
        scratch = Path(directory)
        yield replace(config if config is not None else load_config(), pstack=fixture_pin(scratch / "fixture")), scratch


@dataclass
class FixtureSkills:
    pins: list[PstackPin] = field(default_factory=list)

    def __call__(self, pin: PstackPin, scratch: Path) -> Path:
        self.pins.append(pin)
        return export_skills(fixture_pin(scratch / "fixture"), scratch)


fixture_skills = FixtureSkills()


def stored_slice(item):
    return {**item, "provenance": {"author": {"stage": "spec", "model": "gpt-author", "family": "gpt"}, "revision": "0" * 64}}


def approved_spec(item, spec=""):
    from foreman.stage_output import CandidateSlice, SpecCandidate, SpecAuthor, SpecCheckOutput
    from foreman.spec_check import SpecHistory, accept
    candidate = SpecCandidate(slices=[CandidateSlice.model_validate(item)], spec=spec, work_kind="feature",
                              author=SpecAuthor(stage="spec", model="gpt-author", family="gpt"))
    checked = accept(SpecHistory(), SpecCheckOutput(findings=[], notes=[], resolutions=[]), candidate, max_rounds=3)
    return {**candidate.update(single=True), "spec_history": checked.history.model_dump(mode="json"),
            "spec_approval": checked.approval.model_dump()}


def checker_reply(args):
    return {"findings": [], "notes": [], "resolutions": []} if args[-1].startswith("Check only the supplied candidate") else None
