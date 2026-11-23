"""Model assignment. This is data. A node does not get to rewrite it."""

from dataclasses import dataclass
from pathlib import Path
import tomllib
from typing import TypeGuard

from foreman._raw import is_dict, is_list

from pydantic import BaseModel, ConfigDict, Field

DEFAULT_PATH = Path(__file__).with_name("models.toml")
ROLES = {"coordinator", "builder", "verifier", "check"}
FAMILIES = {"claude", "gpt", "grok"}
EFFORTS = {
    # Claude Code 2.1.329 --help, --effort.
    "claude": {"low", "medium", "high", "xhigh", "max"},
    # Grok Build 1.0.89 binary, "invalid reasoning effort" diagnostic.
    "grok": {"none", "minimal", "low", "medium", "high", "xhigh", "max"},
    # Codex 0.171.0, protocol/src/openai_models.rs, ReasoningEffort.
    "gpt": {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra", "persistent"},
}


class ConfigError(ValueError):
    pass


class ModelDependencyMissing(ConfigError):
    pass


class SkillPin(BaseModel):
    """One skills directory at an exact commit of its repository."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str = Field(min_length=1)
    commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    directory: str = Field(default="pstack/skills", min_length=1)


PSTACK_PIN = SkillPin(
    repository="https://github.com/cursor/plugins",
    commit="d42aff08aa58965d2735c4ead61a9af82b89d0d0",
)
MATT_SKILLS_PIN = SkillPin(
    repository="https://github.com/mattpocock/skills",
    commit="abeeaf95660ba9e8b7c753eb38f441ca707a8823",
    directory="skills",
)


@dataclass(frozen=True)
class ModelSpec:
    name: str
    family: str
    model_id: str
    effort: str | None

    @classmethod
    def parse(cls, model: str) -> "ModelSpec":
        provider = family(model)
        base, _, suffix = model.rpartition("-")
        if suffix in EFFORTS[provider]:
            return cls(model, provider, base, suffix)
        return cls(model, provider, model, None)


@dataclass(frozen=True)
class NodeConfig:
    role: str
    models: tuple[ModelSpec, ...]
    other_family_from: str | None = None


@dataclass(frozen=True)
class Config:
    max_review_rounds: int
    nodes: dict[str, NodeConfig]
    pstack: SkillPin = PSTACK_PIN
    matt_skills: SkillPin = MATT_SKILLS_PIN


def family(model: str) -> str:
    prefix = model.split("-", 1)[0]
    if prefix not in FAMILIES:
        raise ConfigError(f"unknown model family: {model}")
    return prefix


def _model_names(value: object) -> TypeGuard[list[str]]:
    return is_list(value) and all(isinstance(item, str) and item for item in value)


def parse_config(data: object) -> Config:
    if not is_dict(data):
        raise ConfigError("config must be a table")
    rounds = data.get("max_review_rounds")
    if not isinstance(rounds, int) or rounds < 1:
        raise ConfigError("max_review_rounds must be an integer of at least 1")
    raw_nodes = data.get("nodes")
    if not is_dict(raw_nodes) or not raw_nodes:
        raise ConfigError("nodes must be a non-empty table")
    nodes: dict[str, NodeConfig] = {}
    for name, raw in raw_nodes.items():
        if not isinstance(name, str) or not is_dict(raw):
            raise ConfigError(f"bad node entry: {name}")
        role = raw.get("role")
        models = raw.get("models")
        other = raw.get("other_family_from")
        if not isinstance(role, str) or role not in ROLES:
            raise ConfigError(f"{name} has an unknown role")
        if not _model_names(models):
            raise ConfigError(f"{name} models must be a list of names")
        if role == "check" and models:
            raise ConfigError(f"{name} is a check and takes no model")
        if role != "check" and not models:
            raise ConfigError(f"{name} needs at least one model")
        if other is not None and not isinstance(other, str):
            raise ConfigError(f"{name} other_family_from must be a node name")
        nodes[name] = NodeConfig(
            role=role, models=tuple(ModelSpec.parse(model) for model in models), other_family_from=other,
        )
    for name, node in nodes.items():
        if node.other_family_from and node.other_family_from not in nodes:
            raise ConfigError(f"{name} refers to a missing node {node.other_family_from}")
    pin = SkillPin.model_validate(data["pstack"]) if "pstack" in data else PSTACK_PIN
    return Config(max_review_rounds=rounds, nodes=nodes, pstack=pin)


def load_config(path: Path | None = None) -> Config:
    source = path or DEFAULT_PATH
    with source.open("rb") as handle:
        return parse_config(tomllib.load(handle))


def contributors_for(
    node: str, config: Config, models_used: dict[str, str], implement_models: list[str] | None = None,
) -> tuple[list[str], set[str]]:
    source = "implement" if node == "review" else config.nodes[node].other_family_from
    if not source:
        return [], set()
    latest = models_used.get(source)
    used = (implement_models if node == "review" else None) or ([latest] if latest else [])
    if not used:
        raise ModelDependencyMissing(f"{node} needs the {source} model")
    return list(dict.fromkeys(used)), {family(model) for model in used}


def order_for(
    node: str, config: Config, models_used: dict[str, str], implement_models: list[str] | None = None,
) -> list[ModelSpec]:
    _, contributing = contributors_for(node, config, models_used, implement_models)
    return sorted(config.nodes[node].models, key=lambda model: model.family in contributing)
