"""Worker methods selected by delivery state."""

from pathlib import Path
import subprocess
import tarfile
from typing import Literal, TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from foreman.config import PstackPin
from foreman.proof import browser_required, formal_required
from foreman.stage_output import WorkKind

if TYPE_CHECKING:
    from foreman.delivery import DeliveryState

AgentStage = Literal["triage", "research", "spec", "implement", "review"]
LocalSkill = Literal["browser-proof", "code-quality", "formal-proof", "merge-risk", "review-quality"]
Condition = WorkKind | Literal["stateful", "browser", "formal"]
PACKAGE_SKILLS = Path(__file__).resolve().with_name("skills")


class PstackMethod(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: str
    kind: Literal["principle", "skill", "leaf_prompt"]
    lines: tuple[int, int] | None = None


class SkillRule(BaseModel):
    model_config = ConfigDict(frozen=True)

    stage: AgentStage
    skill: PstackMethod | LocalSkill
    when: Condition | None = None

    def applies(self, state: "DeliveryState") -> bool:
        match self.when:
            case None:
                return True
            case "stateful":
                return state.get("issue_kind") == "slice" and state.get("slice", {}).get("stateful") is True
            case "browser":
                return bool(item := state.get("slice")) and browser_required(item)
            case "formal":
                return bool(item := state.get("slice")) and formal_required(item)
            case work_kind:
                return state.get("work_kind") == work_kind


def principle(stage: AgentStage, name: str) -> SkillRule:
    return SkillRule(stage=stage, skill=PstackMethod(path=f"principle-{name}/SKILL.md", kind="principle"))


SKILL_RULES = (
    SkillRule(stage="triage", skill=PstackMethod(path="poteto-mode/SKILL.md", kind="leaf_prompt", lines=(110, 110))),
    SkillRule(stage="research", skill=PstackMethod(path="how/SKILL.md", kind="leaf_prompt", lines=(17, 17))),
    SkillRule(stage="research", skill=PstackMethod(path="why/SKILL.md", kind="skill"), when="bug_fix"),
    principle("spec", "redesign-from-first-principles"),
    principle("spec", "subtract-before-you-add"),
    principle("spec", "model-the-domain"),
    principle("spec", "boundary-discipline"),
    principle("spec", "type-system-discipline"),
    SkillRule(stage="spec", skill=PstackMethod(path="statechart/SKILL.md", kind="skill"), when="stateful"),
    SkillRule(
        stage="implement", skill=PstackMethod(path="poteto-mode/playbooks/feature.md", kind="leaf_prompt", lines=(12, 13)),
        when="feature",
    ),
    principle("implement", "laziness-protocol"),
    principle("implement", "test-behavior-not-implementation"),
    principle("implement", "prove-it-works"),
    SkillRule(stage="implement", skill=PstackMethod(path="tdd/SKILL.md", kind="skill"), when="bug_fix"),
    SkillRule(stage="implement", skill=PstackMethod(path="benchmark-checklist/SKILL.md", kind="skill"), when="perf_issue"),
    SkillRule(stage="implement", skill="browser-proof", when="browser"),
    SkillRule(stage="implement", skill="formal-proof", when="formal"),
    principle("review", "boundary-discipline"),
    principle("review", "model-the-domain"),
    principle("review", "prove-it-works"),
    principle("review", "test-behavior-not-implementation"),
    SkillRule(stage="review", skill="review-quality"),
    SkillRule(stage="review", skill="merge-risk"),
)


class SkillMissing(ValueError):
    halt: Literal["skill_missing"] = "skill_missing"

    def __init__(self, method: str, pin: PstackPin):
        super().__init__(f"Required method {method} is unavailable at pstack commit {pin.commit}.")


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True,
    ).stdout.strip()


def export_skills(pin: PstackPin, scratch: Path) -> Path:
    checkout = scratch / "source"
    checkout.mkdir()
    archive = scratch / "skills.tar"
    root = (scratch / "skills").resolve()
    root.mkdir()
    try:
        git(checkout, "init", "--quiet")
        git(checkout, "fetch", "--quiet", "--no-tags", "--depth=1", "--", pin.repository, pin.commit)
        git(checkout, "archive", "--format=tar", f"--output={archive.resolve()}", "FETCH_HEAD:pstack/skills")
        with tarfile.open(archive) as bundle:
            bundle.extractall(root, filter="data")
    except (OSError, subprocess.CalledProcessError, tarfile.TarError) as exc:
        raise SkillMissing("poteto-mode/SKILL.md", pin) from exc

    for rule in SKILL_RULES:
        method = rule.skill
        path = root / method.path if isinstance(method, PstackMethod) else PACKAGE_SKILLS / f"{method}.md"
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            raise SkillMissing(method.path if isinstance(method, PstackMethod) else path.name, pin) from exc
        if not lines or isinstance(method, PstackMethod) and method.lines and len(lines) < method.lines[1]:
            raise SkillMissing(method.path if isinstance(method, PstackMethod) else path.name, pin)

    for path in sorted(root.rglob("*"), key=lambda entry: -len(entry.parts)):
        if not path.is_symlink():
            path.chmod(path.stat().st_mode & ~0o222)
    root.chmod(root.stat().st_mode & ~0o222)
    return root


def stage_methods(stage: str, state: "DeliveryState", root: Path) -> tuple[str, list[str]]:
    instructions: list[str] = []
    methods: list[str] = []
    for rule in SKILL_RULES:
        if rule.stage != stage or not rule.applies(state):
            continue
        method = rule.skill
        if isinstance(method, str):
            path = PACKAGE_SKILLS / f"{method}.md"
            methods.append(f"foreman/skills/{path.name}")
            instructions.append(f"Read {path} in full and follow it.")
        elif method.lines:
            first, last = method.lines
            methods.append(f"{method.path}#L{first}-L{last}")
            instructions.append(f"Use only lines {first} through {last} of {root / method.path} as the method.")
        else:
            methods.append(method.path)
            instructions.append(f"Read {root / method.path} in full and follow it.")
    return "\n\n".join(instructions), methods
