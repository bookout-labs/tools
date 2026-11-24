"""Offline skills, candidates, approvals and checker responses for tests and evals."""

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from tempfile import TemporaryDirectory

from foreman.stages import STAGES
from foreman.config import Config, SkillPin, load_config
from foreman.delivery import stage_text
from foreman.dry_run import CLEAR
from foreman.spec_check import SpecHistory, accept
from foreman.stage_output import PlanState, CandidateSlice, SliceProvenanceRecord, SpecCandidate, SpecAuthor, SpecTarget
from foreman.skill_rules import export_skills, git


AUTHOR = SpecAuthor(stage="spec", model="gpt-author", family="gpt")
PROVENANCE: SliceProvenanceRecord = {"author": AUTHOR.record(), "revision": "0" * 64}


def fixture_pins(root: Path) -> tuple[SkillPin, SkillPin]:
    """One synthetic repository that holds the pstack and the Matt Pocock skill directories."""
    root.mkdir()
    git(root, "init", "--quiet")
    directories = {"pstack": "pstack/skills", "matt_skills": "skills"}
    for rule in (rule for stage in STAGES.values() for rule in stage.skills):
        method = rule.skill
        if method.kind not in {"repository_skill", "target_skill"}:
            path = root / directories[method.repository] / method.path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("Synthetic method.\n" * (method.lines[1] if method.lines else 1), encoding="utf-8")
    git(root, "add", "--", *directories.values())
    git(
        root, "-c", "user.name=Foreman", "-c", "user.email=foreman@example.invalid",
        "-c", "commit.gpgSign=false", "commit", "--quiet", "-m", "Synthetic skill fixture",
    )
    commit = git(root, "rev-parse", "HEAD")
    return (SkillPin(repository=str(root.resolve()), commit=commit),
            SkillPin(repository=str(root.resolve()), commit=commit, directory="skills"))


def fixture_pin(root: Path) -> SkillPin:
    return fixture_pins(root)[0]


@contextmanager
def verify_checkout() -> Iterator[Path]:
    """A target checkout whose HEAD tracks one verify skill, so Verify drives the change."""
    with TemporaryDirectory(prefix="foreman-target-") as directory:
        root = Path(directory).resolve()
        git(root, "init", "--quiet")
        (root / ".claude/skills/verify-web").mkdir(parents=True)
        (root / ".claude/skills/verify-web/SKILL.md").write_text("# Verify the web app\n", encoding="utf-8")
        git(root, "add", "--", ".claude")
        git(root, "-c", "user.name=Foreman", "-c", "user.email=foreman@example.invalid",
            "-c", "commit.gpgSign=false", "commit", "--quiet", "-m", "Verify skill")
        yield root


@contextmanager
def skill_fixture(config: Config | None = None) -> Iterator[tuple[Config, Path]]:
    with TemporaryDirectory(prefix="foreman-skills-") as directory:
        scratch = Path(directory)
        pstack, matt_skills = fixture_pins(scratch / "fixture")
        yield replace(config if config is not None else load_config(), pstack=pstack, matt_skills=matt_skills), scratch


@dataclass
class FixtureSkills:
    pins: list[SkillPin] = field(default_factory=list)

    def __call__(self, pin: SkillPin, matt_skills: SkillPin, scratch: Path) -> Path:
        self.pins.append(pin)
        return export_skills(*fixture_pins(scratch / "fixture"), scratch)


fixture_skills = FixtureSkills()


def stored_slice(item: Mapping[str, object]) -> dict[str, object]:
    return {**item, "provenance": PROVENANCE}


def approved_spec(item: Mapping[str, object], target: SpecTarget, spec: str = "") -> PlanState:
    candidate = SpecCandidate(slices=[CandidateSlice.model_validate(item)], spec=spec, work_kind="feature",
                              author=AUTHOR, target=target)
    checked = accept(SpecHistory(), CLEAR, candidate)
    return candidate.update(PlanState(spec_gate="ready", spec_history={"rounds": checked.history.rounds}), single=True)


def checker_reply(args: Sequence[str]) -> dict[str, object] | None:
    return CLEAR.model_dump() if args[-1].startswith(stage_text("spec_check")) else None
