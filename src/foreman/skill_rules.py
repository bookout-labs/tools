"""Worker methods selected by delivery state."""

from collections.abc import Callable
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile
from typing import Literal, TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from foreman.config import SkillPin
from foreman.stage_output import WorkKind
from foreman.workspace import WorkspaceError

if TYPE_CHECKING:
    from foreman.state import DeliveryState

SkillSource = Callable[[SkillPin, SkillPin, Path], Path]

Condition = WorkKind | Literal["stateful", "parent"]
Repository = Literal["pstack", "matt_skills"]
SkillFailureReason = Literal["pin_unavailable", "method_missing"]
PACKAGE_SKILLS = Path(__file__).resolve().with_name("skills")
# pstack create-verification-skill writes the target repository's own skill under one of these.
TARGET_SKILL_DIRECTORIES = (".cursor/skills", ".claude/skills")


class SkillMethod(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: str
    kind: Literal["skill", "leaf_prompt", "repository_skill", "target_skill"]
    repository: Repository = "pstack"

    def source(self, root: Path) -> Path:
        if self.kind == "repository_skill":
            return PACKAGE_SKILLS / Path(self.path).name
        return root / self.path if self.repository == "pstack" else root / self.repository / self.path


class SkillRule(BaseModel):
    model_config = ConfigDict(frozen=True)

    skill: SkillMethod
    when: Condition | None = None

    def applies(self, state: "DeliveryState") -> bool:
        plan = state["plan"]
        match self.when:
            case None:
                return True
            case "stateful":
                return plan.issue_kind == "slice" and plan.slice is not None and plan.slice["stateful"] is True
            case "parent":
                return plan.issue_kind == "parent"
            case work_kind:
                return plan.work_kind == work_kind


class SkillMissing(ValueError):
    halt: Literal["skill_missing"] = "skill_missing"

    def __init__(self, pin: SkillPin, method: str | None = None, detail: str | None = None, *, source: Repository = "pstack"):
        self.reason: SkillFailureReason = "pin_unavailable" if method is None else "method_missing"
        reason = "pin unavailable" if method is None else f"method missing at pin ({method})"
        super().__init__(f"{source} {reason}, commit {pin.commit}" + (f": {detail}" if detail else ""))


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True,
    ).stdout.strip()


def export_skills(pin: SkillPin, matt_skills: SkillPin, scratch: Path) -> Path:
    """Export the pstack skills to the returned root and the Matt Pocock skills under its matt_skills directory."""
    from foreman.stages import STAGES

    root = (scratch / "skills").resolve()
    pins: dict[Repository, SkillPin] = {"pstack": pin, "matt_skills": matt_skills}
    for name, source in pins.items():
        checkout, archive = scratch / f"{name}-source", scratch / f"{name}.tar"
        destination = root if name == "pstack" else root / name
        checkout.mkdir()
        destination.mkdir()
        try:
            git(checkout, "init", "--quiet")
            git(checkout, "fetch", "--quiet", "--no-tags", "--depth=1", "--", source.repository, source.commit)
            git(checkout, "archive", "--format=tar", f"--output={archive.resolve()}", f"FETCH_HEAD:{source.directory}")
            with tarfile.open(archive) as bundle:
                bundle.extractall(destination, filter="data")
        except (OSError, subprocess.CalledProcessError, tarfile.TarError) as exc:
            raise SkillMissing(source, source=name) from exc

    for rule in (rule for stage in STAGES.values() for rule in stage.skills if rule.skill.kind != "target_skill"):
        method = rule.skill
        pin, name = pins[method.repository], method.path
        try:
            content = method.source(root).read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise SkillMissing(pin, name, source=method.repository) from exc
        if not content.strip():
            raise SkillMissing(pin, name, "method is empty", source=method.repository)

    for path in sorted(root.rglob("*"), key=lambda entry: -len(entry.parts)):
        if not path.is_symlink():
            path.chmod(path.stat().st_mode & ~0o222)
    root.chmod(root.stat().st_mode & ~0o222)
    return root


class TargetSkillUnreadable(WorkspaceError):
    """Git could not list the target checkout's skills at its HEAD."""


def target_skills(workspace: str, path: str) -> list[Path]:
    """The target repository's own skills matching a `<name>` path: Git-tracked regular files at the checkout's HEAD.

    A symlink or an untracked file never counts. The first skills directory wins a name both directories hold.
    A dry run has no checkout, so it finds none."""
    if not workspace:
        return []
    checkout, pattern = Path(workspace), re.sub(r"<[a-z]+>", "*", path)
    depth = len(PurePosixPath(pattern).parts)
    try:
        listed = subprocess.run(["git", "--no-replace-objects", "ls-tree", "-rz", "HEAD", "--", *TARGET_SKILL_DIRECTORIES],
                                cwd=checkout, capture_output=True, check=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise TargetSkillUnreadable(f"The target checkout's skills could not be listed: {exc}") from exc
    files = {entry.split(b"\t", 1)[1].decode(): entry.split(b" ", 1)[0] for entry in listed.split(b"\0") if entry}
    found: dict[str, Path] = {}
    for directory in TARGET_SKILL_DIRECTORIES:
        for name, mode in sorted(files.items()):
            relative = PurePosixPath(name).relative_to(directory) if name.startswith(f"{directory}/") else None
            # match() anchors only at the right, so the depth must agree too.
            if (relative is not None and mode in (b"100644", b"100755") and len(relative.parts) == depth
                    and relative.match(pattern)):
                found.setdefault(relative.parts[0], checkout / name)
    return list(found.values())


def stage_methods(stage: str, state: "DeliveryState", root: Path) -> tuple[str, list[str], list[str]]:
    from foreman.stages import STAGES

    instructions: list[str] = []
    methods: list[str] = []
    roots: list[str] = []
    for rule in STAGES[stage].skills:
        if not rule.applies(state):
            continue
        method = rule.skill
        if method.kind == "target_skill":
            workspace = state["run"].workspace
            for found in target_skills(workspace, method.path):
                methods.append(found.relative_to(workspace).as_posix())
                instructions.append(f"Read {found} in full and follow it to verify the change.")
            continue
        path = method.source(root)
        read_root = str((PACKAGE_SKILLS if method.kind == "repository_skill" else root).resolve())
        if read_root not in roots:
            roots.append(read_root)
        methods.append(method.path)
        instructions.append(f"Read {path} in full and follow it.")
    return "\n\n".join(instructions), methods, roots
