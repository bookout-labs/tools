"""Model assignment. This is data. A node does not get to rewrite it."""

from dataclasses import dataclass
from pathlib import Path
import tomllib
from typing import cast

DEFAULT_PATH = Path(__file__).with_name("models.toml")
ROLES = {"coordinator", "builder", "verifier", "check"}
CLAUDE_EFFORTS = {"low", "medium", "high", "xhigh", "max"}
EFFORTS: dict[str, set[str]] = {"claude": CLAUDE_EFFORTS, "grok": CLAUDE_EFFORTS | {"none", "minimal"}, "gpt": set()}


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ModelSpec:
    name: str
    family: str
    model_id: str
    effort: str | None

    @classmethod
    def parse(cls, name: str) -> "ModelSpec":
        provider = family(name)
        base, _, suffix = name.rpartition("-")
        if suffix in EFFORTS[provider]:
            return cls(name, provider, base, suffix)
        return cls(name, provider, name, None)


@dataclass(frozen=True)
class NodeConfig:
    role: str
    models: tuple[ModelSpec, ...]
    other_family_from: str | None = None


@dataclass(frozen=True)
class Config:
    max_review_rounds: int
    nodes: dict[str, NodeConfig]


def family(model: str) -> str:
    prefix = model.split("-", 1)[0]
    if prefix not in EFFORTS:
        raise ConfigError(f"unknown model family: {model}")
    return prefix


def parse_config(data: object) -> Config:
    if not isinstance(data, dict):
        raise ConfigError("config must be a table")
    table = cast(dict[object, object], data)
    rounds = table.get("max_review_rounds")
    if not isinstance(rounds, int) or rounds < 1:
        raise ConfigError("max_review_rounds must be an integer of at least 1")
    raw_nodes = table.get("nodes")
    if not isinstance(raw_nodes, dict) or not raw_nodes:
        raise ConfigError("nodes must be a non-empty table")
    nodes: dict[str, NodeConfig] = {}
    for name, raw in cast(dict[object, object], raw_nodes).items():
        if not isinstance(name, str) or not isinstance(raw, dict):
            raise ConfigError(f"bad node entry: {name}")
        entry = cast(dict[object, object], raw)
        role = entry.get("role")
        models = entry.get("models")
        other = entry.get("other_family_from")
        if not isinstance(role, str) or role not in ROLES:
            raise ConfigError(f"{name} has an unknown role")
        if not isinstance(models, list) or not all(isinstance(item, str) and item for item in cast(list[object], models)):
            raise ConfigError(f"{name} models must be a list of names")
        if role == "check" and models:
            raise ConfigError(f"{name} is a check and takes no model")
        if role != "check" and not models:
            raise ConfigError(f"{name} needs at least one model")
        if other is not None and not isinstance(other, str):
            raise ConfigError(f"{name} other_family_from must be a node name")
        nodes[name] = NodeConfig(role, tuple(ModelSpec.parse(model) for model in cast(list[str], models)), other)
    for name, node in nodes.items():
        if node.other_family_from and node.other_family_from not in nodes:
            raise ConfigError(f"{name} refers to a missing node {node.other_family_from}")
    return Config(max_review_rounds=rounds, nodes=nodes)


def load_config(path: Path | None = None) -> Config:
    source = path or DEFAULT_PATH
    with source.open("rb") as handle:
        return parse_config(tomllib.load(handle))


def order_for(
    node: str, config: Config, models_used: dict[str, str], implement_models: list[str] | None = None,
) -> list[ModelSpec]:
    spec = config.nodes[node]
    models = list(spec.models)
    source = "implement" if node == "review" else spec.other_family_from
    if not source:
        return models
    latest = models_used.get(source)
    used = (implement_models if node == "review" else None) or ([latest] if latest else [])
    if not used:
        raise ConfigError(f"{node} needs the {source} model")
    excluded = {family(model) for model in used}
    others = [model for model in models if model.family not in excluded]
    if not others:
        names = ", ".join(sorted(excluded))
        group = "family" if len(excluded) == 1 else "families"
        raise ConfigError(f"no {node} model outside the {names} {group}")
    return others
